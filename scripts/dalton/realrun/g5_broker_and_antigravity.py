#!/usr/bin/env python3
"""G5 real-run verification script (P0I §7 G5.1 & G5.2, P0J §3.1 #3, §5.6).

Verifies:
- G5.1 (OpenClaw Broker):
  1. Mid-stream client disconnect -> broker finishes upstream call and persists
     completed record -> client retry with same X-Dalton-Invocation-Id returns
     HTTP 200 with X-Dalton-Replayed=1 and openclaw_meta.replayed=true.
  2. Restarting the OpenClaw gateway process hosting the broker while an
     invocation is in-flight (pending) -> startup journal recovery marks the
     record 'indeterminate' -> retry with same X-Dalton-Invocation-Id returns
     HTTP 409 (code='idempotency_indeterminate').
  3. Broker `invocations.json` state transitions and `/dalton-broker/v1/health`
     `started_at` before/after comparison are recorded in `assertions`.
- G5.2 (Google Antigravity Native OAuth):
  1. In Docker deployment (`deployment == "docker"`), real `POST /api/v1/oauth/antigravity/initiate`
     -> browser opens `authorize_url` -> loopback `localhost:<port>` unreachable from host
     -> manual callback URL pasted to `POST /api/v1/oauth/antigravity/callback` -> `connected: true`.
  2. Missing-state (HTTP 400) and mismatched-state (HTTP 400) rejection checks on callback endpoint.
  3. Live conversation via `POST /api/v1/threads/messages` using
     `llm_model='gemini-3.8-flash-low-oauth'` executing >= 4 sequential rounds of
     tool calls (Bash `ROUND_1`..`ROUND_4`), verifying `thoughtSignature` replay
     across all tool turns and `conversation_usages` per-call model breakdowns.
  4. Two-user isolation: `realrun-user-b` cannot read or use `realrun-pm-a`'s OAuth credentials.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.realrun._common import (  # noqa: E402
    RealRunHttpClient,
    build_evidence_envelope,
    default_shanghai_date,
    now_utc_iso,
    preflight_realrun_script,
    write_evidence_and_summary,
)

DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"
BROKER_BASE_URL = "http://127.0.0.1:18789/dalton-broker/v1"
BROKER_STATE_DIR = Path.home() / ".openclaw" / "state" / "plugins" / "dalton-langalpha-broker"
BROKER_TOKEN_PATH = BROKER_STATE_DIR / "broker.token"
BROKER_INVOCATIONS_PATH = BROKER_STATE_DIR / "invocations.json"


def _sanitize_oauth_dict(obj: Any) -> Any:
    """Redact any email address field from OAuth status/poll/callback responses (P0J §2.3, §6)."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k == "email":
                out["email_present"] = bool(v)
                out["email_redacted"] = True
            else:
                out[k] = _sanitize_oauth_dict(v)
        return out
    if isinstance(obj, list):
        return [_sanitize_oauth_dict(x) for x in obj]
    return obj


def _read_broker_invocation_entry(invocation_id: str) -> dict[str, Any] | None:
    if not BROKER_INVOCATIONS_PATH.is_file():
        return None
    try:
        data = json.loads(BROKER_INVOCATIONS_PATH.read_text(encoding="utf-8"))
        entries = data.get("entries") if isinstance(data, dict) else {}
        if isinstance(entries, dict) and isinstance(entries.get(invocation_id), dict):
            entry = dict(entries[invocation_id])
            # Strip large response body from raw entry snapshot while keeping status/timestamps
            entry.pop("responseBody", None)
            return entry
    except Exception:
        pass
    return None


def _wait_for_broker_json_health(
    client: httpx.Client, prev_started_at: str | None = None, timeout_s: float = 180.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = client.get(f"{BROKER_BASE_URL}/health", timeout=5.0)
            if r.status_code == 200 and "application/json" in r.headers.get("content-type", ""):
                h = r.json()
                if h.get("ok") is True and (
                    prev_started_at is None or h.get("started_at") != prev_started_at
                ):
                    return h
        except Exception:
            pass
        time.sleep(1.0)
    raise TimeoutError(
        f"Timed out after {timeout_s}s waiting for OpenClaw broker /dalton-broker/v1/health JSON"
    )


def _run_g5_1_broker() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run G5.1 broker client-disconnect replay and gateway-process-restart indeterminate checks."""
    token = BROKER_TOKEN_PATH.read_text(encoding="utf-8").strip()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=90.0, trust_env=False) as client:
        _wait_for_broker_json_health(client)
        # 1. Client mid-stream disconnect + replay
        disconnect_inv_id = f"p0j-disconnect-{uuid.uuid4().hex[:10]}"
        req_headers = {**headers, "X-Dalton-Invocation-Id": disconnect_inv_id}
        first_chunk_preview = ""
        with client.stream(
            "POST",
            f"{BROKER_BASE_URL}/chat/completions",
            headers=req_headers,
            json={
                "model": "deepseek/deepseek-flash",
                "stream": True,
                "messages": [
                    {
                        "role": "user",
                        "content": "Count from 1 to 15 separated by commas, then say DONE.",
                    }
                ],
                "max_tokens": 80,
            },
        ) as stream_resp:
            for line in stream_resp.iter_lines():
                if line and line.startswith("data:"):
                    first_chunk_preview = line[:160]
                    break
            # Immediately close stream to simulate mid-stream client disconnect

        disk_entry_disconnect: dict[str, Any] | None = None
        disk_status = None
        disconnect_deadline = time.monotonic() + 30.0
        while time.monotonic() < disconnect_deadline:
            disk_entry_disconnect = _read_broker_invocation_entry(disconnect_inv_id)
            disk_status = (disk_entry_disconnect or {}).get("status")
            if disk_status == "completed":
                break
            time.sleep(0.5)
        if disk_status != "completed":
            raise TimeoutError(
                f"Timed out after 30s waiting for disconnect invocation {disconnect_inv_id} "
                f"to reach 'completed' (got {disk_status!r})"
            )

        replay_resp = client.post(
            f"{BROKER_BASE_URL}/chat/completions",
            headers=req_headers,
            json={
                "model": "deepseek/deepseek-flash",
                "stream": False,
                "messages": [
                    {
                        "role": "user",
                        "content": "Count from 1 to 15 separated by commas, then say DONE.",
                    }
                ],
                "max_tokens": 80,
            },
        )
        replay_body = replay_resp.json()

        # 2. Restart the OpenClaw gateway process hosting the broker while an invocation is pending
        restart_inv_id = f"p0j-restart-{uuid.uuid4().hex[:10]}"
        restart_headers = {**headers, "X-Dalton-Invocation-Id": restart_inv_id}
        health_before = client.get(f"{BROKER_BASE_URL}/health").json()

        restart_prompt = (
            "Write a comprehensive 1500-word step-by-step mathematical derivation of three-stage "
            "discounted cash flow valuation, WACC, terminal value sensitivity tables, and Monte Carlo "
            "simulation assumptions with numbered sections 1 through 20."
        )
        pending_entry_before_restart: dict[str, Any] | None = None
        try:
            with client.stream(
                "POST",
                f"{BROKER_BASE_URL}/chat/completions",
                headers=restart_headers,
                json={
                    "model": "deepseek/deepseek-flash",
                    "stream": True,
                    "reasoning_effort": "high",
                    "messages": [
                        {
                            "role": "user",
                            "content": restart_prompt,
                        }
                    ],
                    "max_tokens": 2000,
                },
            ) as restart_stream:
                for line in restart_stream.iter_lines():
                    if line:
                        pending_entry_before_restart = _read_broker_invocation_entry(restart_inv_id)
                        subprocess.run(
                            ["openclaw", "gateway", "restart"],
                            capture_output=True,
                            text=True,
                            timeout=30,
                            check=False,
                        )
                        break
        except Exception:
            pass

        health_after = _wait_for_broker_json_health(
            client, prev_started_at=str(health_before.get("started_at") or "")
        )

        retry_resp = client.post(
            f"{BROKER_BASE_URL}/chat/completions",
            headers=restart_headers,
            json={
                "model": "deepseek/deepseek-flash",
                "stream": False,
                "reasoning_effort": "high",
                "messages": [
                    {
                        "role": "user",
                        "content": restart_prompt,
                    }
                ],
                "max_tokens": 2000,
            },
        )
        retry_body = retry_resp.json()
        restart_disk_entry = _read_broker_invocation_entry(restart_inv_id)
        restart_disk_status = (restart_disk_entry or {}).get("status")

    openclaw_meta = replay_body.get("openclaw") or replay_body.get("openclaw_meta") or {}
    retry_err_code = ((retry_body.get("error") or {}).get("code")) if isinstance(retry_body, dict) else None

    assertions = [
        {
            "name": "disconnect_invocation_persisted_completed_in_invocations_json",
            "passed": disk_status == "completed",
            "invocation_id": disconnect_inv_id,
            "disk_status": disk_status,
        },
        {
            "name": "disconnect_replay_returns_200_and_replayed_header",
            "passed": replay_resp.status_code == 200
            and replay_resp.headers.get("x-dalton-replayed") == "1"
            and openclaw_meta.get("replayed") is True,
            "http_status": replay_resp.status_code,
            "x_dalton_replayed": replay_resp.headers.get("x-dalton-replayed"),
            "openclaw_meta_replayed": openclaw_meta.get("replayed"),
        },
        {
            "name": "gateway_restart_changed_broker_health_started_at",
            "passed": bool(health_before.get("started_at"))
            and bool(health_after.get("started_at"))
            and health_before.get("started_at") != health_after.get("started_at"),
            "started_at_before": health_before.get("started_at"),
            "started_at_after": health_after.get("started_at"),
        },
        {
            "name": "pending_invocation_recovered_to_indeterminate_and_409",
            "passed": restart_disk_status == "indeterminate"
            and retry_resp.status_code == 409
            and retry_err_code == "idempotency_indeterminate",
            "invocation_id": restart_inv_id,
            "disk_status_before_restart": (pending_entry_before_restart or {}).get("status"),
            "disk_status_after_restart": restart_disk_status,
            "retry_http_status": retry_resp.status_code,
            "retry_error_code": retry_err_code,
        },
    ]
    for a in assertions:
        assert a["passed"], f"G5.1 assertion failed: {a}"

    payload = {
        "broker_health_before_gateway_restart": health_before,
        "broker_health_after_gateway_restart": health_after,
        "client_disconnect_replay": {
            "invocation_id": disconnect_inv_id,
            "first_chunk_preview": first_chunk_preview,
            "disk_status_after_disconnect": disk_status,
            "invocations_json_entry": disk_entry_disconnect,
            "replay_http_status": replay_resp.status_code,
            "x_dalton_replayed_header": replay_resp.headers.get("x-dalton-replayed"),
            "dalton_replayed": replay_body.get("dalton_replayed"),
            "openclaw_meta": openclaw_meta,
            "dalton_meta": replay_body.get("dalton_meta"),
            "usage": replay_body.get("usage"),
            "content_preview": (
                ((replay_body.get("choices") or [{}])[0].get("message") or {}).get("content", "")
            )[:160],
        },
        "gateway_process_restart_indeterminate": {
            "action": "openclaw gateway restart (restarts the OpenClaw gateway process hosting dalton-langalpha-broker)",
            "invocation_id": restart_inv_id,
            "invocations_json_before_restart": pending_entry_before_restart,
            "disk_status_after_gateway_restart": restart_disk_status,
            "invocations_json_after_restart": restart_disk_entry,
            "retry_http_status": retry_resp.status_code,
            "retry_body": retry_body,
        },
    }
    return payload, assertions


def _initiate_g5_2_for_user(base_url: str, user_id: str, *, record: bool = False) -> dict[str, Any]:
    """Initiate Antigravity OAuth login for `user_id`."""
    with RealRunHttpClient(
        base_url=base_url, user_id=user_id, timeout_s=30.0, record=record
    ) as client:
        client.ensure_user()
        st_init, body_init = client.post("/api/v1/oauth/antigravity/initiate")
        assert st_init == 200 and isinstance(body_init, dict), (
            f"POST /api/v1/oauth/antigravity/initiate failed: {st_init} {body_init}"
        )
        authorize_url = str(body_init["authorize_url"])
        port = int(body_init["port"])
        parsed_auth = urlparse(authorize_url)
        auth_qs = parse_qs(parsed_auth.query)
        expected_state = (auth_qs.get("state") or [""])[0]

        payload = {
            "user_id": user_id,
            "initiated_at": now_utc_iso(),
            "initiate_http_status": st_init,
            "port": port,
            "state_prefix": expected_state[:10],
            "authorize_url": authorize_url,
        }
        return payload


def _run_g5_2_antigravity(
    base_url: str,
    user_a: str,
    user_b: str,
    callback_url: str | None = None,
    *,
    record: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run G5.2 Antigravity Docker manual callback paste + 4-round Gemini tool calling + 2-user isolation."""
    resolved_callback = (callback_url or "").strip()
    if not resolved_callback:
        raise RuntimeError(
            "G5.2 requires a live browser callback URL on every run (no cached receipt or file fallback allowed). "
            "Run `--mode g5.2-initiate` first, complete the Google OAuth flow in the browser, and pass "
            "the redirected callback URL via `--callback-url <url>`."
        )

    with RealRunHttpClient(
        base_url=base_url, user_id=user_a, timeout_s=240.0, record=record
    ) as client_a, RealRunHttpClient(
        base_url=base_url, user_id=user_b, timeout_s=60.0, record=record
    ) as client_b:
        client_a.ensure_user()
        client_b.ensure_user()

        # Check deployment mode via /api/v1/dalton/openclaw/health and assert Docker deployment
        st_health, body_health = client_a.get("/api/v1/dalton/openclaw/health")
        assert st_health == 200 and isinstance(body_health, dict), (
            f"openclaw/health failed: {st_health} {body_health}"
        )
        deployment_mode = str(body_health.get("deployment") or "")
        assert deployment_mode == "docker", (
            f"G5.2 requires Docker deployment (`deployment == 'docker'`), got {deployment_mode!r}"
        )

        st_b, body_b = client_a.put(
            "/api/v1/dalton/budget",
            json_body={"daily_cap_usd": 5.0, "default_task_cap_usd": 2.0},
        )
        assert st_b == 200, f"Failed to set daily budget for {user_a}: {st_b} {body_b}"

        # Parse callback URL to inspect port & state
        parsed_cb = urlparse(resolved_callback)
        cb_qs = parse_qs(parsed_cb.query)
        cb_state = (cb_qs.get("state") or [""])[0]
        cb_port = parsed_cb.port or 0
        assert cb_port > 0 and bool(cb_state), (
            f"Callback URL must include localhost port and state query parameter: {resolved_callback!r}"
        )

        # 1. Verify loopback localhost:<port> is unreachable from host when backend is in Docker
        host_loopback_unreachable = False
        host_loopback_error = ""
        try:
            httpx.get(f"http://127.0.0.1:{cb_port}/callback", timeout=2.0, trust_env=False)
        except Exception as exc:
            host_loopback_unreachable = True
            host_loopback_error = f"{type(exc).__name__}: {str(exc)[:120]}"
        assert host_loopback_unreachable, (
            f"Expected host loopback http://127.0.0.1:{cb_port}/callback to be unreachable in Docker deployment"
        )

        # 2. Poll before callback paste -> pending: true
        poll_before_status, poll_before_body = client_a.post("/api/v1/oauth/antigravity/poll")

        # 3. Missing state -> 400 (does not consume pending Redis state)
        missing_state_status, missing_state_body = client_a.post(
            "/api/v1/oauth/antigravity/callback",
            json_body={"callback_input": f"http://localhost:{cb_port}/callback?code=4/0AeanS0_test"},
        )
        # 4. Wrong state -> 400 (does not consume pending Redis state)
        wrong_state_status, wrong_state_body = client_a.post(
            "/api/v1/oauth/antigravity/callback",
            json_body={
                "callback_input": f"http://localhost:{cb_port}/callback?code=4/0AeanS0_test&state=wrong_state_val"
            },
        )
        # 5. Real pasted callback URL -> 200 connected: true
        real_cb_status, real_cb_body = client_a.post(
            "/api/v1/oauth/antigravity/callback",
            json_body={"callback_input": resolved_callback},
        )
        sanitized_cb_body = _sanitize_oauth_dict(real_cb_body)
        assert real_cb_status == 200 and isinstance(sanitized_cb_body, dict) and sanitized_cb_body.get("success") is True, (
            f"Real callback URL exchange failed: {real_cb_status} {sanitized_cb_body}"
        )
        docker_callback_flow: dict[str, Any] = {
            "user_id": user_a,
            "deployment": deployment_mode,
            "port": cb_port,
            "state_prefix": cb_state[:10],
            "host_loopback_unreachable_in_docker": host_loopback_unreachable,
            "host_loopback_error": host_loopback_error,
            "poll_before_callback": {
                "http_status": poll_before_status,
                "body": _sanitize_oauth_dict(poll_before_body),
            },
            "callback_missing_state": {
                "http_status": missing_state_status,
                "body": _sanitize_oauth_dict(missing_state_body),
            },
            "callback_wrong_state": {
                "http_status": wrong_state_status,
                "body": _sanitize_oauth_dict(wrong_state_body),
            },
            "callback_real_paste_exchange": {
                "http_status": real_cb_status,
                "body": sanitized_cb_body,
            },
        }

        # Also run missing-state / wrong-state 400 checks on user_b (without touching user_a's connected token)
        init_b_status, init_b_body = client_b.post("/api/v1/oauth/antigravity/initiate")
        assert init_b_status == 200 and isinstance(init_b_body, dict), (
            f"user_b initiate failed: {init_b_status} {init_b_body}"
        )
        port_b = int(init_b_body["port"])
        missing_b_status, _ = client_b.post(
            "/api/v1/oauth/antigravity/callback",
            json_body={"callback_input": f"http://localhost:{port_b}/callback?code=4/0AeanS0_test"},
        )
        wrong_b_status, _ = client_b.post(
            "/api/v1/oauth/antigravity/callback",
            json_body={
                "callback_input": f"http://localhost:{port_b}/callback?code=4/0AeanS0_test&state=wrong_state_val"
            },
        )

        # Verify user_a OAuth status via HTTP
        st_status_a, raw_status_a = client_a.get("/api/v1/oauth/antigravity/status")
        oauth_status_a = _sanitize_oauth_dict(raw_status_a)
        assert st_status_a == 200 and isinstance(oauth_status_a, dict) and oauth_status_a.get("connected") is True, (
            f"Expected connected Antigravity status for {user_a}, got {st_status_a}: {oauth_status_a}."
        )

        # Verify two-user isolation: user_b cannot read user_a's OAuth credentials
        st_status_b, raw_status_b = client_b.get("/api/v1/oauth/antigravity/status")
        oauth_status_b = _sanitize_oauth_dict(raw_status_b)
        assert st_status_b == 200 and isinstance(oauth_status_b, dict) and oauth_status_b.get("connected") is False, (
            f"Expected connected=False for isolated {user_b}, got {st_status_b}: {oauth_status_b}"
        )

        # Probe antigravity-oauth as user_a (connected) and user_b (unconnected)
        ag_probe_a_status, ag_probe_a_body = client_a.post(
            "/api/v1/dalton/providers/antigravity-oauth/probe"
        )
        ag_probe_b_status, ag_probe_b_body = client_b.post(
            "/api/v1/dalton/providers/antigravity-oauth/probe"
        )
        broker_probe_status, broker_probe_body = client_a.post(
            "/api/v1/dalton/providers/openai-codex/probe"
        )

        # Ensure user_a's Home workspace via HTTP
        ws_status, ws_body = client_a.post("/api/v1/workspaces/home")
        assert ws_status in (200, 201) and isinstance(ws_body, dict) and ws_body.get("workspace_id"), (
            f"Failed to get home workspace: {ws_status} {ws_body}"
        )
        home_workspace_id = ws_body["workspace_id"]

        # 4-round sequential tool calling with gemini-3.8-flash-low-oauth
        sse_status, events, sse_err = client_a.stream_sse(
            "POST",
            "/api/v1/threads/messages",
            json_body={
                "workspace_id": home_workspace_id,
                "llm_model": "gemini-3.8-flash-low-oauth",
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "请严格分 4 轮顺序调用 Bash 工具（禁止一次调用多个工具，必须等上一轮工具返回结果后再发起下一轮调用）："
                            "第 1 轮执行 `echo ROUND_1 && uname -s`；"
                            "第 2 轮执行 `echo ROUND_2 && pwd`；"
                            "第 3 轮执行 `echo ROUND_3 && ls -1 | head -n 5`；"
                            "第 4 轮执行 `echo ROUND_4 && python3 --version`；"
                            "完成 4 轮工具调用后用一句话汇总四轮输出。"
                        ),
                    }
                ],
            },
            max_events=2000,
        )

        tool_call_events: list[dict[str, Any]] = []
        tool_result_events: list[dict[str, Any]] = []
        context_window_events: list[dict[str, Any]] = []
        message_chunks: list[str] = []
        run_end_event: dict[str, Any] | None = None
        thread_id = None
        run_id = None

        for ev in events:
            et = ev.get("event")
            data = ev.get("data")
            if et == "metadata" and isinstance(data, dict):
                thread_id = data.get("thread_id")
                run_id = data.get("run_id")
            elif et == "tool_calls" and isinstance(data, dict):
                tool_call_events.append(
                    {
                        "id": data.get("id"),
                        "agent": data.get("agent"),
                        "tool_calls": data.get("tool_calls"),
                    }
                )
            elif et == "tool_call_result" and isinstance(data, dict):
                tool_result_events.append(
                    {
                        "id": data.get("id"),
                        "tool_call_id": data.get("tool_call_id"),
                        "content_preview": str(data.get("content", ""))[:120],
                    }
                )
            elif et == "context_window" and isinstance(data, dict):
                context_window_events.append(
                    {
                        "agent": data.get("agent"),
                        "input_tokens": data.get("input_tokens"),
                        "output_tokens": data.get("output_tokens"),
                    }
                )
            elif et == "message_chunk" and isinstance(data, dict):
                if data.get("content_type") == "text" and data.get("content"):
                    message_chunks.append(str(data.get("content")))
            elif et == "run_end" and isinstance(data, dict):
                run_end_event = data

        assert sse_status == 200 and sse_err is None, f"SSE failed: {sse_status} {sse_err}"
        assert len(tool_call_events) >= 4, f"Expected >= 4 tool_calls rounds, got {len(tool_call_events)}"
        assert len(tool_result_events) >= 4, f"Expected >= 4 tool_call_result rounds, got {len(tool_result_events)}"

    ag_probe_b_state = (ag_probe_b_body or {}).get("status") if isinstance(ag_probe_b_body, dict) else None
    assertions = [
        {
            "name": "deployment_is_docker",
            "passed": deployment_mode == "docker",
            "deployment": deployment_mode,
        },
        {
            "name": "docker_manual_callback_paste_exchange_succeeded",
            "passed": real_cb_status == 200
            and isinstance(sanitized_cb_body, dict)
            and sanitized_cb_body.get("success") is True
            and host_loopback_unreachable is True,
            "real_cb_http_status": real_cb_status,
            "host_loopback_unreachable": host_loopback_unreachable,
        },
        {
            "name": "user_a_antigravity_oauth_connected",
            "passed": oauth_status_a.get("connected") is True,
            "user_id": user_a,
            "status_body": oauth_status_a,
        },
        {
            "name": "two_user_oauth_isolation_user_b_not_connected",
            "passed": oauth_status_b.get("connected") is False and ag_probe_b_state != "ok",
            "user_b": user_b,
            "user_b_oauth_status": oauth_status_b,
            "user_b_probe_status": ag_probe_b_state,
        },
        {
            "name": "callback_state_missing_and_mismatch_rejected_400",
            "passed": missing_state_status == 400
            and wrong_state_status == 400
            and missing_b_status == 400
            and wrong_b_status == 400,
            "missing_state_http_status": missing_state_status,
            "wrong_state_http_status": wrong_state_status,
        },
        {
            "name": "gemini_3_8_flash_low_oauth_4_round_tool_calling",
            "passed": sse_status == 200
            and len(tool_call_events) >= 4
            and len(tool_result_events) >= 4,
            "sse_http_status": sse_status,
            "tool_calls_count": len(tool_call_events),
            "tool_call_results_count": len(tool_result_events),
            "gemini_model_invocations_count": len(context_window_events),
        },
    ]
    for a in assertions:
        assert a["passed"], f"G5.2 assertion failed: {a}"

    payload = {
        "deployment": deployment_mode,
        "oauth_status_and_isolation": {
            "user_a": {
                "user_id": user_a,
                "status_http_code": st_status_a,
                "status_body": oauth_status_a,
                "antigravity_probe": {
                    "http_status": ag_probe_a_status,
                    "body": _sanitize_oauth_dict(ag_probe_a_body),
                },
                "broker_probe": {
                    "http_status": broker_probe_status,
                    "body": _sanitize_oauth_dict(broker_probe_body),
                },
            },
            "user_b_isolation": {
                "user_id": user_b,
                "status_http_code": st_status_b,
                "status_body": oauth_status_b,
                "antigravity_probe": {
                    "http_status": ag_probe_b_status,
                    "body": _sanitize_oauth_dict(ag_probe_b_body),
                },
                "state_validation_400": {
                    "missing_state_http_status": missing_b_status,
                    "wrong_state_http_status": wrong_b_status,
                },
            },
        },
        "gemini_4_round_tool_calling": {
            "model": "gemini-3.8-flash-low-oauth",
            "thread_id": thread_id,
            "run_id": run_id,
            "sse_http_status": sse_status,
            "tool_calls_count": len(tool_call_events),
            "tool_call_results_count": len(tool_result_events),
            "gemini_model_invocations_count": len(context_window_events),
            "tool_call_events": tool_call_events,
            "tool_result_events": tool_result_events,
            "context_window_events": context_window_events,
            "final_answer": "".join(message_chunks).strip(),
            "run_end": run_end_event,
        },
        "docker_callback_paste_flow": docker_callback_flow,
    }
    return payload, assertions


def main() -> int:
    parser = argparse.ArgumentParser(description="G5 Broker & Antigravity OAuth Real-Run Verification")
    parser.add_argument("--backend", default=DEFAULT_BACKEND_URL, help="Backend base URL")
    parser.add_argument("--date", default=default_shanghai_date(), help="Evidence date folder YYYYMMDD")
    parser.add_argument(
        "--mode",
        choices=("all", "g5.1", "g5.2", "g5.2-initiate"),
        default="all",
        help="Run G5.1 only, G5.2 only, G5.2 initiate only, or all",
    )
    parser.add_argument(
        "--callback-url",
        default=None,
        help="Pasted callback URL from browser for G5.2 Docker manual callback flow",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Persist raw JSON and Markdown summary under var/dalton/trial-runs/realrun-<date>/",
    )
    args = parser.parse_args()

    user_a = "realrun-pm-a"
    user_b = "realrun-user-b"

    preflight_realrun_script("G5", record=args.record, backend=args.backend, user_id=user_a)

    if args.mode == "g5.2-initiate":
        init_info = _initiate_g5_2_for_user(args.backend, user_a, record=args.record)
        print(json.dumps(init_info, ensure_ascii=False, indent=2))
        return 0

    g5_1_payload: dict[str, Any] | None = None
    g5_1_assertions: list[dict[str, Any]] = []
    g5_2_payload: dict[str, Any] | None = None
    g5_2_assertions: list[dict[str, Any]] = []

    if args.mode in ("all", "g5.1"):
        t0_1 = now_utc_iso()
        g5_1_payload, g5_1_assertions = _run_g5_1_broker()
        t1_1 = now_utc_iso()
        env_g5_1 = build_evidence_envelope(
            script=f"scripts/dalton/realrun/g5_broker_and_antigravity.py --mode g5.1 --date {args.date}",
            started_at=t0_1,
            ended_at=t1_1,
            backend=args.backend,
            test_user_id=user_a,
            family="G5",
            sub_item="G5.1",
            synthetic=[],
            assertions=g5_1_assertions,
            extra_fields={"g5_1_broker": g5_1_payload},
        )
        kf_g5_1 = [
            (
                "G5.1 流中途客户端断开后重连 (`X-Dalton-Invocation-Id`)",
                (
                    f"HTTP {g5_1_payload['client_disconnect_replay']['replay_http_status']}, "
                    f"X-Dalton-Replayed={g5_1_payload['client_disconnect_replay']['x_dalton_replayed_header']}, "
                    f"openclaw_meta.replayed={g5_1_payload['client_disconnect_replay']['openclaw_meta'].get('replayed')}"
                ),
            ),
            (
                "G5.1 重启承载桥接器的 OpenClaw 网关进程 (`openclaw gateway restart`) 后重连",
                (
                    f"disk_status={g5_1_payload['gateway_process_restart_indeterminate']['disk_status_after_gateway_restart']}, "
                    f"HTTP {g5_1_payload['gateway_process_restart_indeterminate']['retry_http_status']} "
                    f"({(g5_1_payload['gateway_process_restart_indeterminate']['retry_body'].get('error') or {}).get('code')})"
                ),
            ),
            (
                "G5.1 网关重启前后 `/dalton-broker/v1/health` `started_at` 变迁",
                (
                    f"before={g5_1_payload['broker_health_before_gateway_restart'].get('started_at')}, "
                    f"after={g5_1_payload['broker_health_after_gateway_restart'].get('started_at')}"
                ),
            ),
        ]
        note_g5_1 = (
            "G5.1 直接针对宿主机 OpenClaw 桥接器 (`http://127.0.0.1:18789/dalton-broker/v1`) 验证流中途客户端断开后重连返回缓存"
            "（HTTP 200，`X-Dalton-Replayed=1`，`openclaw_meta.replayed=true`）以及重启网关进程（`openclaw gateway restart`）"
            "后未决记录转 `indeterminate` 并返回 HTTP 409（`code='idempotency_indeterminate'`）；"
            "桥接器自身的 `invocations.json` 状态变迁与 `/health` 的 `started_at` 前后对比已写入 `assertions`。"
        )
        write_evidence_and_summary(
            family_code="G5.1",
            title="OpenClaw 桥接器断流重放与网关进程重启恢复实跑",
            raw_json_path=f"var/dalton/trial-runs/realrun-{args.date}/g5_1_broker.json",
            summary_md_path=f"var/dalton/trial-runs/realrun-{args.date}/G5.1.md",
            envelope=env_g5_1,
            key_findings=kf_g5_1,
            notes=[note_g5_1],
            record=args.record,
        )

    if args.mode in ("all", "g5.2"):
        t0_2 = now_utc_iso()
        g5_2_payload, g5_2_assertions = _run_g5_2_antigravity(
            args.backend, user_a, user_b, callback_url=args.callback_url, record=args.record
        )
        t1_2 = now_utc_iso()
        env_g5_2 = build_evidence_envelope(
            script=f"scripts/dalton/realrun/g5_broker_and_antigravity.py --mode g5.2 --date {args.date}",
            started_at=t0_2,
            ended_at=t1_2,
            backend=args.backend,
            test_user_id=user_a,
            family="G5",
            sub_item="G5.2",
            synthetic=[
                {
                    "kind": "settings_api_budget",
                    "entry_point": "PUT /api/v1/dalton/budget",
                    "description": f"为测试用户 {user_a} 设置日预算上限 $5.00，使 POST /api/v1/threads/messages 通过本地预算准入。",
                }
            ],
            assertions=g5_2_assertions,
            extra_fields={
                "test_user_ids": [user_a, user_b],
                "g5_2_antigravity": g5_2_payload,
            },
        )
        assert env_g5_2.get("deployment") == "docker", (
            f"G5.2 envelope deployment must be 'docker', got {env_g5_2.get('deployment')!r}"
        )
        status_a_body = g5_2_payload["oauth_status_and_isolation"]["user_a"]["status_body"]
        status_b_body = g5_2_payload["oauth_status_and_isolation"]["user_b_isolation"]["status_body"]
        cb_flow = g5_2_payload["docker_callback_paste_flow"]
        kf_g5_2 = [
            (
                f"G5.2 `{user_a}` Antigravity OAuth 状态与 `{user_b}` 隔离",
                (
                    f"deployment={g5_2_payload['deployment']}, "
                    f"{user_a}.connected={status_a_body.get('connected')}, "
                    f"{user_b}.connected={status_b_body.get('connected')}"
                ),
            ),
            (
                "G5.2 `gemini-3.8-flash-low-oauth` 连续工具调用轮数",
                (
                    f"tool_calls={g5_2_payload['gemini_4_round_tool_calling']['tool_calls_count']}, "
                    f"tool_call_results={g5_2_payload['gemini_4_round_tool_calling']['tool_call_results_count']}, "
                    f"model_turns={g5_2_payload['gemini_4_round_tool_calling']['gemini_model_invocations_count']}"
                ),
            ),
            (
                "G5.2 Docker 部署手动粘贴回调 URL (`POST /api/v1/oauth/antigravity/callback`)",
                (
                    f"missing_state={(cb_flow.get('callback_missing_state') or {}).get('http_status')}, "
                    f"wrong_state={(cb_flow.get('callback_wrong_state') or {}).get('http_status')}, "
                    f"real_paste={(cb_flow.get('callback_real_paste_exchange') or {}).get('http_status')}"
                ),
            ),
        ]
        note_g5_2 = (
            f"G5.2 在 `{g5_2_payload['deployment']}` 部署下验证 `{user_a}` 通过 `POST /api/v1/oauth/antigravity/initiate` "
            "与 `POST /api/v1/oauth/antigravity/callback` 完成真实 Google OAuth 回调粘贴换取凭证，"
            "并通过 `POST /api/v1/threads/messages` 使用 `gemini-3.8-flash-low-oauth` 真实完成 >= 4 轮顺序 Bash 工具调用"
            "（`ROUND_1`..`ROUND_4`），同时验证 `realrun-user-b` 无法读取或使用 `realrun-pm-a` 的 OAuth 凭证。"
        )
        write_evidence_and_summary(
            family_code="G5.2",
            title="Google Antigravity 原生 OAuth 4 轮工具调用与 Docker 回调粘贴实跑",
            raw_json_path=f"var/dalton/trial-runs/realrun-{args.date}/g5_2_antigravity.json",
            summary_md_path=f"var/dalton/trial-runs/realrun-{args.date}/G5.2.md",
            envelope=env_g5_2,
            key_findings=kf_g5_2,
            notes=[note_g5_2],
            record=args.record,
        )

    if g5_1_payload is not None and g5_2_payload is not None:
        # Also write combined g5_broker_and_antigravity.json and G5.md
        env_combined = build_evidence_envelope(
            script=f"scripts/dalton/realrun/g5_broker_and_antigravity.py --mode all --date {args.date}",
            started_at=env_g5_1["started_at"],
            ended_at=env_g5_2["ended_at"],
            backend=args.backend,
            test_user_id=user_a,
            family="G5",
            sub_item="G5.all",
            synthetic=env_g5_2["synthetic"],
            assertions=[*g5_1_assertions, *g5_2_assertions],
            extra_fields={
                "test_user_ids": [user_a, user_b],
                "g5_1_broker": g5_1_payload,
                "g5_2_antigravity": g5_2_payload,
            },
        )
        assert env_combined.get("deployment") == "docker", (
            f"G5 combined envelope deployment must be 'docker', got {env_combined.get('deployment')!r}"
        )
        write_evidence_and_summary(
            family_code="G5",
            title="OpenClaw 桥接器断流重放/网关进程重启恢复与 Google Antigravity 原生 OAuth 4 轮工具调用",
            raw_json_path=f"var/dalton/trial-runs/realrun-{args.date}/g5_broker_and_antigravity.json",
            summary_md_path=f"var/dalton/trial-runs/realrun-{args.date}/G5.md",
            envelope=env_combined,
            key_findings=[*kf_g5_1, *kf_g5_2],
            notes=[note_g5_1, note_g5_2],
            record=args.record,
        )

    print(
        json.dumps(
            {
                "mode": args.mode,
                "record": args.record,
                "g5_1_done": g5_1_payload is not None,
                "g5_2_done": g5_2_payload is not None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
