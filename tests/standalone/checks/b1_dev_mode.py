#!/usr/bin/env python3
# auth: oss
"""Batch 1 standalone core E2E acceptance suite (auth: oss).

Verifies design §5.1 Batch 1 end-to-end against a clean core stack:
1. Both Alembic chains (upstream + Dalton) reached current ``head`` (dynamically
   resolved, never hardcoded) and re-running ``migrate`` is idempotent.
2. Layer-by-layer scan of backend image ``layer.tar`` confirms excluded
   proprietary host bridge, channel gateway, and ops scripts are absent.
3. Internal header stripping:
   - Seeds ``local-dev-user`` and ``probe-edge-user``.
   - Direct call to backend with ``X-Service-Token`` + ``X-User-Id: probe-edge-user``
     returns ``probe-edge-user`` (HTTP 200).
   - Edge call to ``/__probe/headers`` with both headers shows neither header
     reached ``header-echo``.
   - Edge call to ``/api/v1/users/me`` with both headers resolves to
     ``local-dev-user`` (not ``probe-edge-user``).
4. Fake OpenAI Responses API model (four core assertions + usage accounting):
   - Streaming greeting.
   - ``ExecuteCode`` tool call in real Docker sandbox with verbatim stdout echoed.
   - Mid-stream disconnect + reconnect via ``reconnect_to_stream`` (``last_event_id``).
   - Post-completion history replay via ``replay_thread_messages``.
   - Usage accounting persisted in ``conversation_usages``.
5. Chat multipart file upload -> ``sandbox-net`` relay download + SHA-256 check
   + LiveFS readable + network isolation blocking ``postgres``/``redis``/``minio``.
6. Host loopback workspace preview URL reachable on the runner.
7. Without search API keys, bound agent tools include ``WebFetch`` and exclude
   ``WebSearch``.
8. Exports a restorable PostgreSQL baseline snapshot as a CI artifact.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import urlsplit

# Allow invoking directly as `python3 tests/standalone/checks/b1_dev_mode.py`
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402

LOCAL_DEV_USER = "local-dev-user"
PROBE_EDGE_USER = "probe-edge-user"
FAKE_PROVIDER_NAME = "fake-openai"
FAKE_MODEL_NAME = "fake-gpt-6-luna"
FAKE_MODEL_ID = os.environ.get("FAKE_LLM_MODEL", "gpt-6-luna")
FAKE_BASE_URL = "http://fake-llm:8099/v1"
FAKE_API_KEY = "sk-fake-standalone-ci-0001"

BATCH1_FORBIDDEN_IMAGE_PATHS = (
    "scripts/dalton/researchdata_bridge.py",
    "scripts/dalton/research_runtime/",
    "scripts/dalton/ops/",
    "integrations/dalton-channel-gateway/",
    "agent_config.dalton.yaml",
    "config.dalton.yaml",
    "web/.env.dalton",
)


def log(msg: str) -> None:
    print(f"[b1_dev_mode] {msg}", flush=True)


def check_migrations_and_health(ctx: _lib.ComposeContext) -> None:
    log("1/8 Checking service health and dual Alembic migration heads...")
    _lib.wait_for_http_ready(f"{ctx.edge_url}/__probe/health", timeout=120.0)
    # Wait for backend via edge /api/v1/auth/cli (public endpoint)
    _lib.wait_for_http_ready(f"{ctx.edge_url}/api/v1/auth/cli", timeout=120.0)

    expected_up, expected_dalton = ctx.get_alembic_heads()
    actual_up, actual_dalton = ctx.get_db_alembic_versions()
    log(
        f"Alembic versions: upstream={actual_up} (head={expected_up}), "
        f"dalton={actual_dalton} (head={expected_dalton})"
    )
    assert actual_up == expected_up, (
        f"Upstream migration mismatch: db={actual_up!r} != head={expected_up!r}"
    )
    assert actual_dalton == expected_dalton, (
        f"Dalton migration mismatch: db={actual_dalton!r} != head={expected_dalton!r}"
    )

    log("Re-running `migrate` service to verify idempotency after interruption...")
    ctx.compose("run", "--rm", "migrate")
    recheck_up, recheck_dalton = ctx.get_db_alembic_versions()
    assert recheck_up == expected_up and recheck_dalton == expected_dalton, (
        f"Post-rerun migration mismatch: {(recheck_up, recheck_dalton)!r}"
    )


def check_backend_image_layers(ctx: _lib.ComposeContext) -> None:
    log("2/8 Scanning backend image layer.tar archives for excluded Batch 1 paths...")
    hits = ctx.scan_service_image_layers("backend", BATCH1_FORBIDDEN_IMAGE_PATHS)
    assert not hits, f"Forbidden paths found in backend image layers:\n" + "\n".join(hits)
    log("Backend image layers clean (0 forbidden Batch 1 paths).")


def check_users_and_edge_header_stripping(ctx: _lib.ComposeContext) -> None:
    log("3/8 Seeding test users and verifying direct service-header auth vs edge stripping...")
    ctx.psql_exec(
        f"""
        INSERT INTO users (user_id, email, name)
        VALUES
            ('{LOCAL_DEV_USER}', 'local-dev@localhost.test', 'Local Dev User'),
            ('{PROBE_EDGE_USER}', 'probe-edge@localhost.test', 'Probe Edge User')
        ON CONFLICT (user_id) DO NOTHING;
        """
    )

    token = ctx.internal_service_token
    probe_script = (
        "import json, urllib.request; "
        "req = urllib.request.Request('http://127.0.0.1:8000/api/v1/users/me', "
        f"headers={{'X-Service-Token': {token!r}, 'X-User-Id': {PROBE_EDGE_USER!r}}}); "
        "resp = urllib.request.urlopen(req, timeout=15); "
        "print(resp.status); print(resp.read().decode('utf-8'))"
    )
    direct_out = (
        ctx.compose("exec", "-T", "backend", "uv", "run", "python", "-c", probe_script)
        .stdout.decode("utf-8")
        .strip()
        .splitlines()
    )
    assert direct_out and direct_out[0] == "200", f"Direct backend call failed: {direct_out}"
    direct_payload = json.loads("\n".join(direct_out[1:]))
    assert direct_payload["user"]["user_id"] == PROBE_EDGE_USER, (
        f"Expected direct backend /users/me to return {PROBE_EDGE_USER!r}, got {direct_payload!r}"
    )
    log(f"Direct backend call with internal headers resolved user={PROBE_EDGE_USER!r}.")

    # Probe via Caddy edge -> header-echo
    probe_resp = _lib.http_request(
        "GET",
        f"{ctx.edge_url}/__probe/headers",
        headers={
            "X-Service-Token": token,
            "X-User-Id": PROBE_EDGE_USER,
            "X-Probe-Sentinel": "keep-me",
        },
    )
    assert probe_resp.status == 200, (
        f"Edge /__probe/headers returned {probe_resp.status}: {probe_resp.text}"
    )
    echo = probe_resp.json()
    echo_headers = {k.lower(): v for k, v in (echo.get("headers") or {}).items()}
    assert echo_headers.get("x-probe-sentinel") == "keep-me", (
        f"Probe sentinel missing from echo response: {echo}"
    )
    assert "x-service-token" not in echo_headers, (
        f"Edge leaked X-Service-Token to upstream: {echo_headers}"
    )
    assert "x-user-id" not in echo_headers, (
        f"Edge leaked X-User-Id to upstream: {echo_headers}"
    )
    log("Edge /__probe/headers confirmed X-Service-Token and X-User-Id were stripped.")

    # Also verify real /api/* route on edge strips both headers (resolves to local-dev-user)
    edge_me = _lib.http_request(
        "GET",
        f"{ctx.edge_url}/api/v1/users/me",
        headers={
            "X-Service-Token": token,
            "X-User-Id": PROBE_EDGE_USER,
        },
    )
    assert edge_me.status == 200, f"Edge /api/v1/users/me failed: {edge_me.status} {edge_me.text}"
    edge_user_id = edge_me.json()["user"]["user_id"]
    assert edge_user_id == LOCAL_DEV_USER, (
        f"Edge /api/v1/users/me honored forged headers! Expected {LOCAL_DEV_USER!r}, got {edge_user_id!r}"
    )
    log(f"Edge /api/v1/users/me also stripped headers and resolved to {edge_user_id!r}.")


def configure_fake_model_and_workspace(ctx: _lib.ComposeContext) -> str:
    log("4/8 Configuring fake Responses API provider/model and creating workspace...")
    pref_resp = _lib.http_request(
        "PUT",
        f"{ctx.edge_url}/api/v1/users/me/preferences",
        json_body={
            "model_preference": {
                "custom_providers": [
                    {
                        "name": FAKE_PROVIDER_NAME,
                        "parent_provider": "openai",
                        "use_response_api": True,
                    }
                ],
                "custom_models": [
                    {
                        "name": FAKE_MODEL_NAME,
                        "model_id": FAKE_MODEL_ID,
                        "provider": FAKE_PROVIDER_NAME,
                        "input_modalities": ["text"],
                    }
                ],
                "preferred_model": FAKE_MODEL_NAME,
            }
        },
    )
    assert pref_resp.status == 200, (
        f"Failed to save fake provider/model preferences: {pref_resp.status} {pref_resp.text}"
    )

    key_resp = _lib.http_request(
        "PUT",
        f"{ctx.edge_url}/api/v1/users/me/api-keys",
        json_body={
            "byok_enabled": True,
            "api_keys": {FAKE_PROVIDER_NAME: FAKE_API_KEY},
            "base_urls": {FAKE_PROVIDER_NAME: FAKE_BASE_URL},
        },
    )
    assert key_resp.status == 200, (
        f"Failed to save fake provider API key: {key_resp.status} {key_resp.text}"
    )

    test_key_resp = _lib.http_request(
        "POST",
        f"{ctx.edge_url}/api/v1/keys/test",
        json_body={
            "provider": FAKE_PROVIDER_NAME,
            "api_key": FAKE_API_KEY,
            "base_url": FAKE_BASE_URL,
        },
    )
    assert test_key_resp.status == 200, (
        f"POST /api/v1/keys/test against fake-llm failed: "
        f"{test_key_resp.status} {test_key_resp.text}"
    )

    ws_resp = _lib.http_request(
        "POST",
        f"{ctx.edge_url}/api/v1/workspaces",
        json_body={"name": "b1-ci-workspace"},
    )
    assert ws_resp.status == 201, f"Failed to create workspace: {ws_resp.status} {ws_resp.text}"
    workspace_id = ws_resp.json()["workspace_id"]
    log(f"Created workspace_id={workspace_id}")
    return workspace_id


def _send_turn_and_collect(
    ctx: _lib.ComposeContext,
    *,
    workspace_id: str,
    content: str,
    thread_id: str | None = None,
    additional_context: list[dict[str, Any]] | None = None,
) -> tuple[str, list[_lib.SSEEvent], str]:
    url = (
        f"{ctx.edge_url}/api/v1/threads/{thread_id}/messages"
        if thread_id
        else f"{ctx.edge_url}/api/v1/threads/messages"
    )
    body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "llm_model": FAKE_MODEL_NAME,
        "messages": [{"role": "user", "content": content}],
    }
    if additional_context:
        body["additional_context"] = additional_context

    events: list[_lib.SSEEvent] = []
    with _lib.open_sse_stream("POST", url, json_body=body, timeout=180.0) as (
        status,
        _headers,
        stream,
    ):
        assert status == 200, f"Chat POST {url} returned HTTP {status}"
        for ev in stream:
            events.append(ev)
            if ev.event == "error":
                raise AssertionError(f"Chat stream emitted error event: {ev.raw_data}")

    resolved_tid = thread_id or _lib.extract_thread_id(events)
    assert resolved_tid, f"No thread_id in SSE events: {[e.event for e in events]}"
    text = _lib.extract_assistant_text(events)
    return resolved_tid, events, text


def check_fake_model_four_flows_and_usage(
    ctx: _lib.ComposeContext, workspace_id: str
) -> str:
    log("4a. Fake model streaming greeting...")
    thread_id, greeting_events, greeting_text = _send_turn_and_collect(
        ctx,
        workspace_id=workspace_id,
        content="Hello Dalton standalone core!",
    )
    assert "Responses API" in greeting_text, (
        f"Unexpected greeting text: {greeting_text!r} (events={[e.event for e in greeting_events]})"
    )
    log(f"4a OK (thread_id={thread_id}, len={len(greeting_text)}).")

    log("4b. Fake model ExecuteCode tool call in real Docker sandbox...")
    _, code_events, code_text = _send_turn_and_collect(
        ctx,
        workspace_id=workspace_id,
        thread_id=thread_id,
        content="RUN_CODE please verify sandbox execution",
    )
    event_names = [e.event for e in code_events]
    assert "FAKE_SANDBOX_OK:6x7=42" in code_text, (
        f"Expected sandbox stdout marker in final reply, got: {code_text!r} "
        f"(events={event_names})"
    )
    log("4b OK (ExecuteCode ran in real sandbox and output echoed verbatim).")

    log("4c. Mid-stream disconnect + reconnect_to_stream(last_event_id)...")
    partial_events: list[_lib.SSEEvent] = []
    last_numeric_id: int | None = None
    run_id: str | None = None
    with _lib.open_sse_stream(
        "POST",
        f"{ctx.edge_url}/api/v1/threads/{thread_id}/messages",
        json_body={
            "workspace_id": workspace_id,
            "llm_model": FAKE_MODEL_NAME,
            "messages": [{"role": "user", "content": "SLOW_STREAM reconnect check"}],
        },
        timeout=180.0,
    ) as (status, _headers, stream):
        assert status == 200, f"SLOW_STREAM POST returned HTTP {status}"
        chunk_count = 0
        for ev in stream:
            partial_events.append(ev)
            if ev.event_id and ev.event_id.isdigit():
                last_numeric_id = int(ev.event_id)
            if run_id is None and isinstance(ev.data, dict) and ev.data.get("run_id"):
                run_id = str(ev.data["run_id"])
            if ev.event == "message_chunk":
                chunk_count += 1
            if chunk_count >= 2 and last_numeric_id is not None:
                # Simulate abrupt client disconnect mid-stream
                break

    assert last_numeric_id is not None, (
        f"Did not capture numeric SSE event id before disconnect: {partial_events}"
    )

    reconnect_url = (
        f"{ctx.edge_url}/api/v1/threads/{thread_id}/messages/stream"
        f"?last_event_id={last_numeric_id}"
    )
    if run_id:
        reconnect_url += f"&run_id={run_id}"

    reconnected_events: list[_lib.SSEEvent] = []
    with _lib.open_sse_stream("GET", reconnect_url, timeout=180.0) as (
        rstatus,
        _rheaders,
        rstream,
    ):
        assert rstatus == 200, f"Reconnect GET {reconnect_url} returned HTTP {rstatus}"
        for ev in rstream:
            reconnected_events.append(ev)
            if ev.event == "error":
                raise AssertionError(f"Reconnect stream emitted error: {ev.raw_data}")

    assert reconnected_events, "Reconnect stream yielded 0 events after disconnect"
    for ev in reconnected_events:
        if ev.event_id and ev.event_id.isdigit():
            assert int(ev.event_id) > last_numeric_id, (
                f"Reconnect re-delivered old event id {ev.event_id} <= {last_numeric_id}"
            )
    combined_text = _lib.extract_assistant_text(partial_events) + _lib.extract_assistant_text(
        reconnected_events
    )
    assert "SLOW_STREAM_COMPLETE" in combined_text, (
        f"Reconnected stream did not reach completion marker; combined={combined_text!r}"
    )
    log(
        f"4c OK (disconnected at id={last_numeric_id}, received "
        f"{len(reconnected_events)} resumed events)."
    )

    log("4d. Post-completion history replay via replay_thread_messages...")
    replay_events: list[_lib.SSEEvent] = []
    with _lib.open_sse_stream(
        "GET",
        f"{ctx.edge_url}/api/v1/threads/{thread_id}/messages/replay",
        timeout=60.0,
    ) as (pstatus, _pheaders, pstream):
        assert pstatus == 200, f"Replay GET returned HTTP {pstatus}"
        for ev in pstream:
            replay_events.append(ev)

    replay_types = [e.event for e in replay_events]
    assert "replay_done" in replay_types, f"Replay missing replay_done: {replay_types}"
    replay_text = _lib.extract_assistant_text(replay_events)
    assert "FAKE_SANDBOX_OK:6x7=42" in replay_text, (
        f"Replay missing ExecuteCode turn text: {replay_text[:400]!r}"
    )
    assert "SLOW_STREAM_COMPLETE" in replay_text, (
        f"Replay missing SLOW_STREAM turn text: {replay_text[:400]!r}"
    )
    log(f"4d OK (replay returned {len(replay_events)} events ending with replay_done).")

    log("4e. Checking usage accounting records in PostgreSQL...")
    usage_row = ctx.psql_query(
        f"""
        SELECT COUNT(*), COALESCE(SUM(total_credits), 0)
        FROM conversation_usages
        WHERE conversation_thread_id = '{thread_id}'
          AND user_id = '{LOCAL_DEV_USER}';
        """
    )
    count_str, credits_str = (usage_row.split("|") + ["0"])[:2]
    assert int(count_str) >= 3, (
        f"Expected >= 3 conversation_usages rows for thread {thread_id}, got {usage_row!r}"
    )
    log(f"4e OK (conversation_usages rows={count_str}, total_credits={credits_str}).")
    return thread_id


def check_upload_relay_livefs_and_network_isolation(
    ctx: _lib.ComposeContext, workspace_id: str, thread_id: str
) -> None:
    log("5/8 Testing chat multipart upload -> sandbox-net relay + LiveFS + network isolation...")
    payload = b"DALTON_STANDALONE_B1_UPLOAD_LINE\n" * 512
    expected_sha = hashlib.sha256(payload).hexdigest()

    init_resp = _lib.http_request(
        "POST",
        f"{ctx.edge_url}/api/v1/dalton/chat-uploads",
        json_body={
            "workspace_id": workspace_id,
            "filename": "b1_probe.txt",
            "size_bytes": len(payload),
            "mime_type": "text/plain",
            "sha256": expected_sha,
        },
    )
    assert init_resp.status == 200, (
        f"Initiate chat upload failed: {init_resp.status} {init_resp.text}"
    )
    upload_id = init_resp.json()["upload_id"]

    part_resp = _lib.http_request(
        "PUT",
        f"{ctx.edge_url}/api/v1/dalton/chat-uploads/{upload_id}/parts/1",
        headers={
            "Content-Type": "application/octet-stream",
            "X-Part-Sha256": expected_sha,
        },
        data=payload,
    )
    assert part_resp.status == 200, (
        f"Upload part 1 failed: {part_resp.status} {part_resp.text}"
    )

    comp_resp = _lib.http_request(
        "POST",
        f"{ctx.edge_url}/api/v1/dalton/chat-uploads/{upload_id}/complete",
        json_body={"sha256": expected_sha},
    )
    assert comp_resp.status == 200, (
        f"Complete chat upload failed: {comp_resp.status} {comp_resp.text}"
    )

    verify_code = f"""
import glob, hashlib, os, socket

# 1. Verify uploaded file materialized via sandbox-net relay with matching SHA-256
matches = glob.glob("work/uploads/*b1_probe*")
assert matches, f"uploaded file missing in work/uploads: {{os.listdir('work/uploads') if os.path.isdir('work/uploads') else 'no dir'}}"
digest = hashlib.sha256(open(matches[0], "rb").read()).hexdigest()
assert digest == {expected_sha!r}, f"sha mismatch: {{digest}}"
print("UPLOAD_SHA_OK=" + digest[:16])

# 2. Verify LiveFS mount is readable inside sandbox
livefs_ok = False
for candidate in ("/mnt/livefs", ".agents/memory", ".agents/memos"):
    if os.path.exists(candidate):
        try:
            os.listdir(candidate)
            livefs_ok = True
            break
        except OSError:
            pass
assert livefs_ok, "LiveFS mount path not readable inside sandbox"
print("LIVEFS_OK=true")

# 3. Verify sandbox-net isolation: postgres, redis, minio must be unreachable
for host, port in (("postgres", 5432), ("redis", 6379), ("minio", 9000)):
    try:
        s = socket.create_connection((host, port), timeout=2.0)
        s.close()
        raise AssertionError(f"Network isolation violated: connected to {{host}}:{{port}}")
    except OSError:
        pass
print("NET_ISOLATION_OK=true")
""".strip()

    encoded = base64.urlsafe_b64encode(verify_code.encode("utf-8")).decode("ascii")
    _, _, verify_text = _send_turn_and_collect(
        ctx,
        workspace_id=workspace_id,
        thread_id=thread_id,
        content=f"RUN_CODE RUN_CODE_B64:{encoded}",
        additional_context=[
            {
                "type": "chat_upload",
                "upload_id": upload_id,
                "name": "b1_probe.txt",
                "mime_type": "text/plain",
                "size": len(payload),
            }
        ],
    )
    assert f"UPLOAD_SHA_OK={expected_sha[:16]}" in verify_text, (
        f"Upload relay verification failed: {verify_text!r}"
    )
    assert "LIVEFS_OK=true" in verify_text, f"LiveFS read check failed: {verify_text!r}"
    assert "NET_ISOLATION_OK=true" in verify_text, (
        f"sandbox-net isolation check failed: {verify_text!r}"
    )
    log("5/8 OK (chat upload relay SHA-256, LiveFS read, and sandbox-net isolation verified).")


def check_loopback_preview(ctx: _lib.ComposeContext, workspace_id: str) -> None:
    log("6/8 Verifying host loopback workspace preview URL...")
    preview_resp = _lib.http_request(
        "POST",
        f"{ctx.edge_url}/api/v1/workspaces/{workspace_id}/sandbox/preview-url",
        json_body={
            "port": 8765,
            "command": "python3 -m http.server 8765 --bind 0.0.0.0",
        },
        timeout=60.0,
    )
    assert preview_resp.status == 200, (
        f"POST preview-url failed: {preview_resp.status} {preview_resp.text}"
    )
    preview_url = preview_resp.json()["url"]
    parsed = urlsplit(preview_url)
    assert parsed.hostname in ("localhost", "127.0.0.1"), (
        f"Expected loopback preview URL in Batch 1, got {preview_url!r}"
    )
    # Wait briefly for the sandbox http.server to accept connections
    page_resp = _lib.wait_for_http_ready(preview_url, timeout=30.0, interval=1.0)
    assert page_resp.status == 200, f"Preview URL {preview_url} returned {page_resp.status}"
    log(f"6/8 OK (loopback preview reachable at {preview_url}).")


def check_web_tools_without_search_keys(
    ctx: _lib.ComposeContext, workspace_id: str, thread_id: str
) -> None:
    log("7/8 Verifying WebFetch present and WebSearch absent when no search keys are set...")
    _, _, report_text = _send_turn_and_collect(
        ctx,
        workspace_id=workspace_id,
        thread_id=thread_id,
        content="REPORT_TOOLS",
    )
    match = re.search(r"BOUND_TOOLS=(\[[^\n]*\])", report_text)
    assert match, f"Fake model did not emit BOUND_TOOLS list: {report_text!r}"
    bound_tools = json.loads(match.group(1))
    assert "WebFetch" in bound_tools, f"Expected WebFetch in bound tools: {bound_tools}"
    assert "WebSearch" not in bound_tools, (
        f"WebSearch should be omitted without search keys, got: {bound_tools}"
    )
    log(f"7/8 OK (bound tools={len(bound_tools)}; WebFetch=present, WebSearch=absent).")


def export_baseline_db_state(ctx: _lib.ComposeContext) -> None:
    log("8/8 Exporting restorable PostgreSQL baseline dump...")
    dump_path = ctx.artifacts_dir / "baseline-b1-oss.dump"
    ctx.dump_database(dump_path)
    size = dump_path.stat().st_size
    assert size > 0, f"Database dump {dump_path} is empty"
    log(f"8/8 OK (exported {dump_path} ({size} bytes), verified with pg_restore --list).")


def main() -> int:
    t0 = time.monotonic()
    ctx = _lib.ComposeContext.from_environment()
    check_migrations_and_health(ctx)
    check_backend_image_layers(ctx)
    check_users_and_edge_header_stripping(ctx)
    workspace_id = configure_fake_model_and_workspace(ctx)
    thread_id = check_fake_model_four_flows_and_usage(ctx, workspace_id)
    check_upload_relay_livefs_and_network_isolation(ctx, workspace_id, thread_id)
    check_loopback_preview(ctx, workspace_id)
    check_web_tools_without_search_keys(ctx, workspace_id, thread_id)
    export_baseline_db_state(ctx)
    elapsed = time.monotonic() - t0
    log(f"ALL BATCH 1 CHECKS PASSED in {elapsed:.1f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
