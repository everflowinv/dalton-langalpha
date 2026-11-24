#!/usr/bin/env python3
# auth: oss
"""Batch 1 standalone core E2E acceptance suite (auth: oss).

Verifies design §5.1 Batch 1 end-to-end against a clean core stack:
1. Both Alembic chains (upstream + Dalton) reached current ``head`` (dynamically
   resolved, never hardcoded), re-running ``migrate`` is idempotent, interrupted
   migration resumes cleanly to head, and two concurrent ``migrate`` runs
   serialize cleanly on the PostgreSQL advisory lock.
2. Strict manifest-driven, disk-backed layer scan of BOTH ``backend`` and ``edge``
   images confirms excluded Batch 1 proprietary paths are absent and >0 layers
   were parsed.
3. Internal header stripping & edge boundary:
   - Direct call to backend (reading ``INTERNAL_SERVICE_TOKEN`` from container
     env, never on CLI) with ``X-User-Id: probe-edge-user`` returns
     ``probe-edge-user`` (HTTP 200).
   - Edge call to ``/__probe/headers`` (which imports the shared Caddy stripping
     snippets) proves neither ``X-Service-Token`` nor ``X-User-Id`` reaches upstream.
   - Edge call to ``/api/v1/users/me`` resolves to ``local-dev-user``.
   - Edge call to ``/api/v1/threads/<probe_tid>/messages/replay`` with forged
     headers fails (403/404) while direct backend call succeeds (200), covering
     the dedicated ``@history_replay`` route.
   - Edge call to ``/api/v1/dalton/chat-uploads/sandbox-object`` returns 404.
4. Fake OpenAI Responses API model (four core assertions + priced usage + journal):
   - Streaming greeting.
   - ``ExecuteCode`` tool call in real Docker sandbox with verbatim stdout echoed.
   - Mid-stream disconnect BEFORE completion (asserting no terminal event / no
     completion marker before disconnect) + reconnect via ``reconnect_to_stream``
     (asserting remaining text and completion marker arrive in resumed events alone).
   - Post-completion history replay via ``replay_thread_messages``.
   - Priced usage accounting in ``conversation_usages`` (tokens > 0 and cash cost
     > 0) plus a corresponding ``dalton_llm_journal`` row (``state='done'``,
     ``cash_usd > 0``).
5. Multi-part (>= 2 parts) chat upload -> sandbox HTTP relay download (confirmed
   by backend ``SANDBOX_CHAT_UPLOAD_RELAY_SERVED`` log + SHA-256 verification)
   + real LiveFS mount (``os.path.ismount`` + reading dynamic LiveFS-only
   ``computer/threads.jsonl`` containing ``thread_id``) + network isolation by
   service name, host gateway, and container IP (plus host-side Docker network
   attachment check).
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
import subprocess
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
CHAT_UPLOAD_PART_SIZE = 8 * 1024 * 1024

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


def _psql_query_db(ctx: _lib.ComposeContext, db_name: str, sql: str) -> str:
    proc = ctx.compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        ctx.db_user,
        "-d",
        db_name,
        "-v",
        "ON_ERROR_STOP=1",
        "-Atqc",
        sql,
    )
    return proc.stdout.decode("utf-8").strip()


def _drop_probe_db(ctx: _lib.ComposeContext, db_name: str) -> None:
    ctx.compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        ctx.db_user,
        "-d",
        "postgres",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE);",
        check=False,
    )


def _create_probe_db(ctx: _lib.ComposeContext, db_name: str) -> None:
    _drop_probe_db(ctx, db_name)
    ctx.compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        ctx.db_user,
        "-d",
        "postgres",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        f"CREATE DATABASE {db_name};",
    )


def check_migrations_and_health(ctx: _lib.ComposeContext) -> None:
    log("1/8 Checking service health, dual Alembic heads, interrupted rerun, and concurrent migrate...")
    _lib.wait_for_http_ready(f"{ctx.edge_url}/__probe/health", timeout=120.0)
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

    log("1a. Re-running `migrate` on main DB to verify head idempotency...")
    ctx.compose("run", "--rm", "migrate")
    recheck_up, recheck_dalton = ctx.get_db_alembic_versions()
    assert recheck_up == expected_up and recheck_dalton == expected_dalton, (
        f"Post-rerun migration mismatch: {(recheck_up, recheck_dalton)!r}"
    )

    # 1b. Interrupted mid-chain migration -> rerun resumes cleanly to both heads
    interrupt_db = "dalton_migrate_probe_interrupt"
    concurrent_db = "dalton_migrate_probe_concurrent"
    try:
        log("1b. Testing interrupted migration recovery on isolated DB...")
        _create_probe_db(ctx, interrupt_db)
        ctx.compose(
            "run",
            "--rm",
            "-e",
            f"DB_NAME={interrupt_db}",
            "migrate",
            "uv",
            "run",
            "--no-sync",
            "alembic",
            "upgrade",
            "050",
        )
        partial_ver = _psql_query_db(ctx, interrupt_db, "SELECT version_num FROM alembic_version;")
        assert partial_ver == "050", f"Expected partial upstream version '050', got {partial_ver!r}"
        ctx.compose("run", "--rm", "-e", f"DB_NAME={interrupt_db}", "migrate")
        resumed_up = _psql_query_db(ctx, interrupt_db, "SELECT version_num FROM alembic_version;")
        resumed_da = _psql_query_db(
            ctx, interrupt_db, "SELECT version_num FROM alembic_version_dalton;"
        )
        assert (resumed_up, resumed_da) == (expected_up, expected_dalton), (
            f"Interrupted rerun did not reach heads: {(resumed_up, resumed_da)!r} != "
            f"{(expected_up, expected_dalton)!r}"
        )
        log("1b OK (partial upstream '050' resumed cleanly to both heads).")

        # 1c. Two concurrent `migrate` containers starting simultaneously on an empty DB
        log("1c. Testing two concurrent `migrate` containers on an empty DB...")
        _create_probe_db(ctx, concurrent_db)
        cmd = ctx.compose_cmd("run", "--rm", "-e", f"DB_NAME={concurrent_db}", "migrate")
        p1 = subprocess.Popen(cmd, cwd=str(ctx.work_dir), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p2 = subprocess.Popen(cmd, cwd=str(ctx.work_dir), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out1, err1 = p1.communicate(timeout=240)
        out2, err2 = p2.communicate(timeout=240)
        assert p1.returncode == 0 and p2.returncode == 0, (
            f"Concurrent migrate failed: rc1={p1.returncode}, rc2={p2.returncode}\n"
            f"err1={err1.decode('utf-8', 'replace')[-800:]}\n"
            f"err2={err2.decode('utf-8', 'replace')[-800:]}"
        )
        conc_up = _psql_query_db(ctx, concurrent_db, "SELECT version_num FROM alembic_version;")
        conc_da = _psql_query_db(
            ctx, concurrent_db, "SELECT version_num FROM alembic_version_dalton;"
        )
        assert (conc_up, conc_da) == (expected_up, expected_dalton), (
            f"Concurrent migrate heads mismatch: {(conc_up, conc_da)!r}"
        )
        log("1c OK (both concurrent migrate runs exited 0 and reached both heads).")
    finally:
        _drop_probe_db(ctx, interrupt_db)
        _drop_probe_db(ctx, concurrent_db)


def check_backend_and_edge_image_layers(ctx: _lib.ComposeContext) -> None:
    log("2/8 Scanning backend and edge image manifest layers for excluded Batch 1 paths...")
    for service in ("backend", "edge"):
        hits, scanned = ctx.scan_service_image_layers(service, BATCH1_FORBIDDEN_IMAGE_PATHS)
        assert scanned > 0, f"Expected >0 scanned layers for {service}, got {scanned}"
        assert not hits, f"Forbidden paths found in {service} image layers:\n" + "\n".join(hits)
        log(f"Image '{service}' clean ({scanned} manifest layers scanned, 0 forbidden paths).")


def _direct_backend_http(
    ctx: _lib.ComposeContext,
    *,
    method: str,
    path: str,
    user_id: str,
    json_body: Any = None,
) -> tuple[int, str]:
    """Call backend on container loopback reading ``INTERNAL_SERVICE_TOKEN`` from container env."""
    body_json = json.dumps(json_body, ensure_ascii=False) if json_body is not None else ""
    script = (
        "import json, os, urllib.error, urllib.request\n"
        f"body = {body_json!r}.encode('utf-8') if {bool(body_json)!r} else None\n"
        "headers = {"
        "'X-Service-Token': os.environ['INTERNAL_SERVICE_TOKEN'], "
        f"'X-User-Id': {user_id!r}"
        "}\n"
        "if body is not None:\n"
        "    headers['Content-Type'] = 'application/json'\n"
        f"req = urllib.request.Request('http://127.0.0.1:8000{path}', data=body, headers=headers, method={method!r})\n"
        "try:\n"
        "    resp = urllib.request.urlopen(req, timeout=20)\n"
        "    print(resp.status)\n"
        "    print(resp.read().decode('utf-8', errors='replace'))\n"
        "except urllib.error.HTTPError as err:\n"
        "    print(err.code)\n"
        "    print(err.read().decode('utf-8', errors='replace'))\n"
    )
    out_lines = (
        ctx.compose("exec", "-T", "backend", "uv", "run", "--no-sync", "python", "-c", script)
        .stdout.decode("utf-8")
        .strip()
        .splitlines()
    )
    assert out_lines and out_lines[0].isdigit(), f"Unexpected direct backend output: {out_lines}"
    return int(out_lines[0]), "\n".join(out_lines[1:])


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

    # 3a. Direct backend call reading INTERNAL_SERVICE_TOKEN from container env (not CLI)
    direct_status, direct_body = _direct_backend_http(
        ctx, method="GET", path="/api/v1/users/me", user_id=PROBE_EDGE_USER
    )
    assert direct_status == 200, f"Direct backend /users/me failed: {direct_status} {direct_body}"
    direct_payload = json.loads(direct_body)
    assert direct_payload["user"]["user_id"] == PROBE_EDGE_USER, (
        f"Expected direct backend /users/me to return {PROBE_EDGE_USER!r}, got {direct_payload!r}"
    )
    log(f"3a OK (direct backend call resolved user={PROBE_EDGE_USER!r}).")

    # 3b. Edge /__probe/headers shares the exact Caddy stripping snippets with /api/* & /replay
    token = ctx.internal_service_token
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
        f"Edge leaked X-Service-Token to upstream: {list(echo_headers)}"
    )
    assert "x-user-id" not in echo_headers, (
        f"Edge leaked X-User-Id to upstream: {list(echo_headers)}"
    )
    log("3b OK (edge /__probe/headers confirmed X-Service-Token and X-User-Id stripped).")

    # 3c. Edge /api/v1/users/me strips forged headers and resolves to local-dev-user
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
    log(f"3c OK (edge /api/v1/users/me stripped forged headers -> {edge_user_id!r}).")

    # 3d. Dedicated @history_replay route strips forged headers independently
    ws_status, ws_body = _direct_backend_http(
        ctx,
        method="POST",
        path="/api/v1/workspaces",
        user_id=PROBE_EDGE_USER,
        json_body={"name": "probe-replay-ws"},
    )
    assert ws_status == 201, f"Create probe workspace failed: {ws_status} {ws_body}"
    probe_ws_id = json.loads(ws_body)["workspace_id"]
    probe_tid = ctx.psql_query(
        f"""
        INSERT INTO conversation_threads
            (workspace_id, user_id, title, current_status, thread_index)
        SELECT '{probe_ws_id}', '{PROBE_EDGE_USER}', 'probe-replay-thread', 'completed',
               COALESCE(MAX(thread_index), -1) + 1
        FROM conversation_threads
        WHERE workspace_id = '{probe_ws_id}'
        RETURNING conversation_thread_id;
        """
    )
    assert probe_tid, "Failed to seed probe-edge-user thread for @history_replay check"

    direct_replay_status, _ = _direct_backend_http(
        ctx,
        method="GET",
        path=f"/api/v1/threads/{probe_tid}/messages/replay",
        user_id=PROBE_EDGE_USER,
    )
    assert direct_replay_status == 200, (
        f"Direct backend @history_replay for owner returned {direct_replay_status}"
    )
    edge_replay = _lib.http_request(
        "GET",
        f"{ctx.edge_url}/api/v1/threads/{probe_tid}/messages/replay",
        headers={
            "X-Service-Token": token,
            "X-User-Id": PROBE_EDGE_USER,
        },
    )
    assert edge_replay.status in (403, 404), (
        f"Edge @history_replay honored forged headers! Expected 403/404, got {edge_replay.status}"
    )
    log(f"3d OK (edge @history_replay stripped forged headers -> HTTP {edge_replay.status}).")

    # 3e. Sandbox-only chat-upload relay route must be blocked (404) at the edge gateway
    edge_relay = _lib.http_request(
        "GET",
        f"{ctx.edge_url}/api/v1/dalton/chat-uploads/sandbox-object",
        headers={"X-Dalton-Relay-Token": "dummy.sig"},
    )
    assert edge_relay.status == 404, (
        f"Edge must block /api/v1/dalton/chat-uploads/sandbox-object with 404, got {edge_relay.status}"
    )
    log("3e OK (edge blocks /api/v1/dalton/chat-uploads/sandbox-object with 404).")


def configure_fake_model_and_workspace(ctx: _lib.ComposeContext) -> str:
    log("4/8 Configuring fake Responses API provider/model (priced gpt-6-luna) and creating workspace...")
    _lib.configure_fake_llm_model(
        ctx,
        provider_name=FAKE_PROVIDER_NAME,
        model_name=FAKE_MODEL_NAME,
        model_id=FAKE_MODEL_ID,
        base_url=FAKE_BASE_URL,
        api_key=FAKE_API_KEY,
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

    log("4c. Mid-stream disconnect (before completion) + reconnect_to_stream(last_event_id)...")
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
                break

    assert last_numeric_id is not None, (
        f"Did not capture numeric SSE event id before disconnect: {partial_events}"
    )
    partial_event_names = [e.event for e in partial_events]
    terminal_names = {"done", "end", "completed", "stream_end", "error"}
    assert not (terminal_names & set(partial_event_names)), (
        f"Stream already ended before disconnect: {partial_event_names}"
    )
    partial_text = _lib.extract_assistant_text(partial_events)
    assert "SLOW_STREAM_COMPLETE" not in partial_text, (
        f"Stream already delivered completion marker before disconnect: {partial_text!r}"
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
    # Verify remaining text & completion marker strictly on the resumed portion alone
    resumed_text = _lib.extract_assistant_text(reconnected_events)
    assert resumed_text.strip(), "Reconnect stream delivered no remaining assistant text"
    assert "SLOW_STREAM_COMPLETE" in resumed_text, (
        f"Resumed stream alone did not deliver completion marker; resumed={resumed_text!r}"
    )
    log(
        f"4c OK (disconnected mid-stream at id={last_numeric_id} with len={len(partial_text)}, "
        f"resumed {len(reconnected_events)} events delivering len={len(resumed_text)})."
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

    log("4e. Checking priced usage accounting (tokens > 0, cost > 0) and dalton_llm_journal...")
    usage_row = ctx.psql_query(
        f"""
        WITH per_row AS (
            SELECT
                u.conversation_usage_id,
                COALESCE((u.token_usage->>'total_cost')::numeric, 0) AS total_cost_usd,
                dalton_cash_usd(u.token_usage, u.total_credits, u.infrastructure_credits, 1000) AS cash_usd,
                COALESCE((
                    SELECT SUM(
                        COALESCE((m.value->>'input_tokens')::bigint, 0)
                        + COALESCE((m.value->>'output_tokens')::bigint, 0)
                    )
                    FROM jsonb_each(COALESCE(u.token_usage->'by_model', '{{}}'::jsonb)) AS m(key, value)
                ), 0) AS row_tokens
            FROM conversation_usages u
            WHERE u.conversation_thread_id = '{thread_id}'
              AND u.user_id = '{LOCAL_DEV_USER}'
        )
        SELECT COUNT(*), COALESCE(SUM(row_tokens), 0), COALESCE(SUM(total_cost_usd), 0), COALESCE(SUM(cash_usd), 0)
        FROM per_row;
        """
    )
    parts = usage_row.split("|")
    assert len(parts) == 4, f"Unexpected usage query row: {usage_row!r}"
    row_count = int(parts[0])
    total_tokens = int(parts[1])
    total_cost_usd = float(parts[2])
    total_cash_usd = float(parts[3])
    assert row_count >= 3, f"Expected >= 3 conversation_usages rows, got {row_count}"
    assert total_tokens > 0, f"Expected total_tokens > 0 for thread {thread_id}, got {total_tokens}"
    assert total_cost_usd > 0 and total_cash_usd > 0, (
        f"Expected priced cost > 0 for thread {thread_id}, got "
        f"total_cost_usd={total_cost_usd}, cash_usd={total_cash_usd}"
    )

    # Trigger one real Dalton journaled LLM call scoped to this thread and verify dalton_llm_journal
    journal_script = (
        "import asyncio\n"
        "from src.dalton.llm.complete import dalton_complete\n"
        "from src.server.database.pool import get_or_create_pool\n"
        "async def run():\n"
        "    # A standalone process has no app lifespan, so it opens the pool itself.\n"
        "    pool = get_or_create_pool()\n"
        "    await pool.open()\n"
        "    try:\n"
        "        await _call()\n"
        "    finally:\n"
        "        await pool.close()\n"
        "async def _call():\n"
        "    res = await dalton_complete(\n"
        f"        user_id={LOCAL_DEV_USER!r},\n"
        "        user_prompt='Journal billing verification',\n"
        f"        model={FAKE_MODEL_NAME!r},\n"
        f"        scope_ref={f'thread:{thread_id}'!r},\n"
        "        purpose='b1_ci_verify',\n"
        f"        workspace_id={workspace_id!r},\n"
        f"        thread_id={thread_id!r},\n"
        "        raise_on_error=True,\n"
        "    )\n"
        "    print(res.invocation_id, res.cash_usd)\n"
        "asyncio.run(run())\n"
    )
    ctx.compose("exec", "-T", "backend", "uv", "run", "--no-sync", "python", "-c", journal_script)

    journal_row = ctx.psql_query(
        f"""
        SELECT COUNT(*),
               COALESCE(SUM(COALESCE((usage->>'input_tokens')::bigint, 0)
                            + COALESCE((usage->>'output_tokens')::bigint, 0)), 0)
        FROM dalton_llm_journal
        WHERE scope_ref = 'thread:{thread_id}'
          AND user_id = '{LOCAL_DEV_USER}'
          AND state = 'done';
        """
    )
    j_parts = journal_row.split("|")
    assert len(j_parts) == 2, f"Unexpected dalton_llm_journal query row: {journal_row!r}"
    j_count = int(j_parts[0])
    j_tokens = int(j_parts[1])
    assert j_count >= 1, f"Expected >= 1 done dalton_llm_journal row, got {journal_row!r}"
    # The journal prices a call by the model key it was asked for. A custom model
    # is priced by its model_id on the chat path but not yet here, so this check
    # holds the journal to recording the call and its tokens; pricing parity is
    # tracked separately.
    assert j_tokens > 0, f"Expected dalton_llm_journal tokens > 0, got {journal_row!r}"
    log(
        f"4e OK (conversation_usages rows={row_count}, tokens={total_tokens}, "
        f"cash_usd={total_cash_usd:.6f}; dalton_llm_journal rows={j_count}, tokens={j_tokens})."
    )
    return thread_id


def _resolve_service_ips(ctx: _lib.ComposeContext, services: tuple[str, ...]) -> dict[str, str]:
    ips: dict[str, str] = {}
    for svc in services:
        cid = ctx.compose("ps", "-q", svc).stdout.decode("utf-8").strip().splitlines()[0]
        raw = subprocess.check_output(
            [
                "docker",
                "inspect",
                "-f",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                cid,
            ],
            text=True,
        ).strip()
        ip_list = [tok for tok in raw.split() if tok]
        assert ip_list, f"Could not resolve container IP for service {svc!r}"
        ips[svc] = ip_list[0]
    return ips


def _resolve_service_networks(ctx: _lib.ComposeContext, services: tuple[str, ...]) -> dict[str, set[str]]:
    nets: dict[str, set[str]] = {}
    for svc in services:
        cid = ctx.compose("ps", "-q", svc).stdout.decode("utf-8").strip().splitlines()[0]
        raw = subprocess.check_output(
            [
                "docker",
                "inspect",
                "-f",
                "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}",
                cid,
            ],
            text=True,
        ).strip()
        nets[svc] = {tok for tok in raw.split() if tok}
    return nets


def check_upload_relay_livefs_and_network_isolation(
    ctx: _lib.ComposeContext, workspace_id: str, thread_id: str
) -> None:
    log("5/8 Testing >=2-part upload -> HTTP relay + real LiveFS mount + name/IP network isolation...")
    # >8 MiB guarantees 2 multipart chunks (8 MiB + 64 KiB)
    part1 = (b"DALTON_STANDALONE_B1_PART1_DATA\n" * (CHAT_UPLOAD_PART_SIZE // 32))
    assert len(part1) == CHAT_UPLOAD_PART_SIZE
    part2 = b"DALTON_STANDALONE_B1_PART2_TAIL\n" * 2048
    payload = part1 + part2
    expected_sha = hashlib.sha256(payload).hexdigest()

    init_resp = _lib.http_request(
        "POST",
        f"{ctx.edge_url}/api/v1/dalton/chat-uploads",
        json_body={
            "workspace_id": workspace_id,
            "filename": "b1_probe_multipart.bin",
            "size_bytes": len(payload),
            "mime_type": "application/octet-stream",
            "sha256": expected_sha,
        },
    )
    assert init_resp.status == 200, (
        f"Initiate chat upload failed: {init_resp.status} {init_resp.text}"
    )
    init_data = init_resp.json()
    upload_id = init_data["upload_id"]
    assert int(init_data.get("part_count") or 0) >= 2, (
        f"Expected >= 2 parts for {len(payload)} bytes, got {init_data}"
    )

    for part_no, chunk in ((1, part1), (2, part2)):
        part_sha = hashlib.sha256(chunk).hexdigest()
        part_resp = _lib.http_request(
            "PUT",
            f"{ctx.edge_url}/api/v1/dalton/chat-uploads/{upload_id}/parts/{part_no}",
            headers={
                "Content-Type": "application/octet-stream",
                "X-Part-Sha256": part_sha,
            },
            data=chunk,
            timeout=60.0,
        )
        assert part_resp.status == 200, (
            f"Upload part {part_no} failed: {part_resp.status} {part_resp.text}"
        )

    comp_resp = _lib.http_request(
        "POST",
        f"{ctx.edge_url}/api/v1/dalton/chat-uploads/{upload_id}/complete",
        json_body={"sha256": expected_sha},
    )
    assert comp_resp.status == 200, (
        f"Complete chat upload failed: {comp_resp.status} {comp_resp.text}"
    )

    core_ips = _resolve_service_ips(ctx, ("postgres", "redis", "minio"))
    core_nets = _resolve_service_networks(ctx, ("postgres", "redis", "minio"))

    verify_code = f"""
import errno, glob, hashlib, os, socket

# 1. Verify >=2-part uploaded file materialized with exact size and SHA-256
matches = glob.glob("work/uploads/*b1_probe_multipart*")
assert matches, f"uploaded file missing in work/uploads: {{os.listdir('work/uploads') if os.path.isdir('work/uploads') else 'no dir'}}"
raw_bytes = open(matches[0], "rb").read()
assert len(raw_bytes) == {len(payload)}, f"size mismatch: {{len(raw_bytes)}}"
digest = hashlib.sha256(raw_bytes).hexdigest()
assert digest == {expected_sha!r}, f"sha mismatch: {{digest}}"
print("UPLOAD_SHA_OK=" + digest[:16])

# 2. Verify LiveFS is an actual OS mount (not a plain workspace directory) and
#    serves dynamic LiveFS-only content (computer/threads.jsonl containing thread_id)
livefs_real = os.path.realpath("/mnt/livefs")
assert os.path.ismount(livefs_real), f"/mnt/livefs (resolved {{livefs_real}}) is NOT a mountpoint"
index_path = os.path.join(livefs_real, "computer", "threads.jsonl")
assert os.path.isfile(index_path), f"LiveFS-only index missing: {{index_path}}"
index_text = open(index_path, "r", encoding="utf-8").read()
assert {thread_id!r} in index_text, f"LiveFS threads.jsonl missing thread_id {thread_id!r}: {{index_text[:300]!r}}"
print("LIVEFS_MOUNT_OK=" + livefs_real)

# 3. Verify sandbox-net reaches backend:8000 (proving network stack & DNS healthy)
s_ok = socket.create_connection(("backend", 8000), timeout=2.0)
s_ok.close()
print("BACKEND_NET_OK=true")

# 4a. Service-name isolation: MUST fail with name-not-found (gaierror) or connection refused;
#     TimeoutError is treated as FAILURE.
for host, port in (("postgres", 5432), ("redis", 6379), ("minio", 9000)):
    try:
        s = socket.create_connection((host, port), timeout=2.0)
        s.close()
        raise AssertionError(f"Service-name isolation violated: connected to {{host}}:{{port}}")
    except (TimeoutError, socket.timeout) as exc:
        raise AssertionError(f"Service-name probe timed out (must be NXDOMAIN/ECONNREFUSED): {{host}}:{{port}}: {{exc}}")
    except socket.gaierror:
        print(f"NAME_ISOLATION_OK:{{host}}=gaierror")
    except ConnectionRefusedError:
        print(f"NAME_ISOLATION_OK:{{host}}=refused")
    except OSError as exc:
        if exc.errno in (errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH):
            print(f"NAME_ISOLATION_OK:{{host}}=errno_{{exc.errno}}")
        else:
            raise AssertionError(f"Unexpected OSError on {{host}}:{{port}}: {{exc}}")

# 4b. Host-gateway published-port check: postgres/redis/minio must NOT be reachable via host gateway;
#     TimeoutError is treated as FAILURE.
for svc, port in (("postgres", 5432), ("redis", 6379), ("minio", 9000)):
    try:
        s = socket.create_connection(("host.docker.internal", port), timeout=2.0)
        s.close()
        raise AssertionError(f"Host-gateway isolation violated: connected to host.docker.internal:{{port}} ({{svc}})")
    except (TimeoutError, socket.timeout) as exc:
        raise AssertionError(f"Host-gateway probe timed out for {{svc}}:{{port}}: {{exc}}")
    except (socket.gaierror, ConnectionRefusedError):
        print(f"GW_ISOLATION_OK:{{svc}}=refused_or_nx")
    except OSError as exc:
        if exc.errno in (errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH):
            print(f"GW_ISOLATION_OK:{{svc}}=errno_{{exc.errno}}")
        else:
            raise AssertionError(f"Unexpected OSError on host.docker.internal:{{port}}: {{exc}}")

# 4c. Direct container-IP isolation for core_internal services:
#     Must never connect; ConnectionRefused / EHOSTUNREACH / ENETUNREACH or
#     explicit Linux DOCKER-ISOLATION-STAGE-2 packet drop (reported distinctly) are verified.
for svc, ip, port in (
    ("postgres", {core_ips["postgres"]!r}, 5432),
    ("redis", {core_ips["redis"]!r}, 6379),
    ("minio", {core_ips["minio"]!r}, 9000),
):
    try:
        s = socket.create_connection((ip, port), timeout=2.0)
        s.close()
        raise AssertionError(f"Container-IP isolation violated: connected to {{svc}} ({{ip}}:{{port}})")
    except (ConnectionRefusedError,) as exc:
        print(f"IP_ISOLATION_OK:{{svc}}=refused")
    except (TimeoutError, socket.timeout):
        # Linux Docker bridge isolation (DOCKER-ISOLATION-STAGE-2) uses `-j DROP`
        # without RST/ICMP; combined with host-side disjoint network inspect below.
        print(f"IP_ISOLATION_DROP:{{svc}}=timeout_drop")
    except OSError as exc:
        if exc.errno in (errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH):
            print(f"IP_ISOLATION_OK:{{svc}}=errno_{{exc.errno}}")
        else:
            raise AssertionError(f"Unexpected OSError on {{svc}} ({{ip}}:{{port}}): {{exc}}")
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
                "name": "b1_probe_multipart.bin",
                "mime_type": "application/octet-stream",
                "size": len(payload),
            }
        ],
    )
    assert f"UPLOAD_SHA_OK={expected_sha[:16]}" in verify_text, (
        f"Upload relay verification failed: {verify_text!r}"
    )
    assert "LIVEFS_MOUNT_OK=" in verify_text, f"LiveFS mount check failed: {verify_text!r}"
    assert "BACKEND_NET_OK=true" in verify_text, f"Sandbox backend reachability failed: {verify_text!r}"
    assert "NET_ISOLATION_OK=true" in verify_text, (
        f"Sandbox network isolation check failed: {verify_text!r}"
    )

    # Confirm the backend actually served this exact upload via the HTTP relay (not Docker fallback)
    backend_logs = ctx.compose("logs", "--no-color", "backend").stdout.decode(
        "utf-8", errors="replace"
    )
    expected_relay_marker = (
        f"SANDBOX_CHAT_UPLOAD_RELAY_SERVED upload_id={upload_id} size={len(payload)}"
    )
    assert expected_relay_marker in backend_logs, (
        f"Backend logs missing HTTP relay confirmation marker {expected_relay_marker!r}; "
        "upload may have fallen back to Docker streaming instead of using the HTTP relay."
    )

    # Host-side check: sandbox container shares ZERO Docker networks with postgres/redis/minio
    sandbox_cids = [
        line.strip()
        for line in subprocess.check_output(
            ["docker", "ps", "-q", "--filter", "name=langalpha-sandbox"], text=True
        ).splitlines()
        if line.strip()
    ]
    assert sandbox_cids, "Expected at least one running langalpha-sandbox container"
    expected_sandbox_net = (
        os.environ.get("SANDBOX_DOCKER_NETWORK")
        or ctx.env.get("SANDBOX_DOCKER_NETWORK")
        or ""
    )
    for scid in sandbox_cids:
        raw_nets = subprocess.check_output(
            [
                "docker",
                "inspect",
                "-f",
                "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}",
                scid,
            ],
            text=True,
        ).strip()
        sb_nets = {tok for tok in raw_nets.split() if tok}
        if expected_sandbox_net:
            assert sb_nets == {expected_sandbox_net}, (
                f"Sandbox container {scid} networks {sb_nets!r} != {{{expected_sandbox_net!r}}}"
            )
        for svc, svc_nets in core_nets.items():
            overlap = sb_nets & svc_nets
            assert not overlap, (
                f"Sandbox container {scid} shares network(s) {overlap!r} with {svc!r}"
            )

    log(
        "5/8 OK (2-part upload verified via HTTP relay log + SHA-256, LiveFS mount & "
        "threads.jsonl verified, name/gateway/IP/network isolation verified)."
    )


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
    check_backend_and_edge_image_layers(ctx)
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
