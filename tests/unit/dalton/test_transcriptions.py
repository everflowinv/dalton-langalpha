"""Unit tests for Backlog A1b: transcription job permissions, identity, persistence, ticket auth, and fake bridge worker."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

from alembic import op as alembic_op
from fastapi import FastAPI, HTTPException
import httpx
import psycopg
import pytest

from ptc_agent.agent.backends.sandbox import SandboxBackend
from ptc_agent.core.sandbox.jobs import Ended, JobHandle, JobRef
from src.dalton.app.transcriptions import router as transcriptions_router
from src.dalton.database import transcriptions as db_transcriptions
from src.dalton.database.transcriptions import TranscriptionJobStatus
from src.dalton.migrations.versions import d022_transcription_jobs
from src.dalton.services import transcriptions as svc_transcriptions
from src.dalton.tools import build_dalton_tools
from src.dalton.tools import transcription_tools

REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_DIR = REPO_ROOT / "scripts" / "dalton"
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import researchdata_bridge as rb  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers & in-memory ledger fixture (multi-worker versioned DB simulation)
# ---------------------------------------------------------------------------


def _make_test_bridge(
    tmp_path: Path,
    *,
    delay_s: float = 0.01,
    fixed_text: str | None = None,
    fail_category: str | None = None,
) -> rb.ResearchDataBridge:
    """Create an isolated `ResearchDataBridge` with ASR jobs under `tmp_path`."""
    ocw_root = tmp_path / "openclaw_workspace"
    ocw_root.mkdir(parents=True, exist_ok=True)
    env_file = tmp_path / "bridge.env"
    env_file.write_text("GEMINI_API_KEY=test_key\n", encoding="utf-8")

    cfg = rb.BridgeConfig()
    cfg.env_path = env_file
    cfg.token = "mcp-secret-token"
    cfg.admin_token = "admin-secret-token"
    cfg.ocw_root = ocw_root
    cfg.wiki_ingest_audit_file = tmp_path / "var" / "dalton" / "wiki_ingest_audit.jsonl"
    cfg.wiki_ingest_temp_dir = tmp_path / "var" / "dalton" / "tmp" / "wiki_ingest"
    cfg.asr_jobs_dir = tmp_path / "var" / "dalton" / "asr_jobs"
    cfg.asr_fake_delay_s = delay_s
    cfg.asr_fake_text = fixed_text
    cfg.asr_fake_fail_category = fail_category
    return rb.ResearchDataBridge(cfg)


async def _send_raw_http_request(
    bridge: rb.ResearchDataBridge,
    method: str,
    path: str,
    headers: dict[str, str],
    body_obj: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Invoke `rb._handle_client` over an in-memory StreamReader/StreamWriter pair."""
    body_bytes = (
        json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
        if body_obj is not None
        else b""
    )
    req_lines = [f"{method} {path} HTTP/1.1"]
    for k, v in headers.items():
        req_lines.append(f"{k}: {v}")
    if body_bytes:
        req_lines.append(f"Content-Length: {len(body_bytes)}")
    raw_req = ("\r\n".join(req_lines) + "\r\n\r\n").encode("utf-8") + body_bytes

    reader = asyncio.StreamReader()
    reader.feed_data(raw_req)
    reader.feed_eof()

    written = bytearray()
    writer = create_autospec(asyncio.StreamWriter, instance=True)
    writer.write.side_effect = lambda data: written.extend(data)
    writer.drain.return_value = None
    writer.wait_closed.return_value = None

    await rb._handle_client(bridge, reader, writer)

    resp_raw = bytes(written).decode("utf-8", errors="replace")
    head, _, body_text = resp_raw.partition("\r\n\r\n")
    status_line = head.splitlines()[0]
    status_code = int(status_line.split(" ")[1])
    parsed_body = json.loads(body_text) if body_text.strip() else {}
    return status_code, parsed_body


class InMemoryTranscriptionLedger:
    """In-memory ledger emulating `dalton_transcription_jobs` with strict `version` conditional updates."""

    def __init__(self) -> None:
        self.rows_by_id: dict[str, dict[str, Any]] = {}
        self.id_by_idemp: dict[str, str] = {}

    async def create_job(
        self,
        *,
        created_by_user_id: str,
        workspace_id: str,
        thread_id: str | None,
        source_object_key: str,
        source_size_bytes: int,
        source_sha256: str,
        idempotency_key: str,
        language: str | None = None,
        hotwords: list[str] | None = None,
        status: str | TranscriptionJobStatus = TranscriptionJobStatus.AWAITING_UPLOAD,
        stage: str | None = None,
        job_id: str | None = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        if idempotency_key and idempotency_key in self.id_by_idemp:
            existing_id = self.id_by_idemp[idempotency_key]
            return dict(self.rows_by_id[existing_id])
        resolved_id = str(job_id or uuid4())
        if resolved_id in self.rows_by_id:
            return dict(self.rows_by_id[resolved_id])
        now = datetime.now(timezone.utc).isoformat()
        status_str = (
            status.value
            if isinstance(status, TranscriptionJobStatus)
            else str(status)
        )
        row: dict[str, Any] = {
            "job_id": resolved_id,
            "created_by_user_id": created_by_user_id,
            "workspace_id": workspace_id,
            "thread_id": thread_id or "",
            "source_object_key": source_object_key,
            "source_size_bytes": int(source_size_bytes),
            "source_sha256": source_sha256,
            "language": language,
            "hotwords": list(hotwords or []),
            "status": status_str,
            "stage": stage or status_str,
            "segment_count": 0,
            "completed_segments": 0,
            "provider_request_ids": [],
            "usage": {},
            "result_object_key": None,
            "error_category": None,
            "error_message": None,
            "idempotency_key": idempotency_key,
            "version": 1,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        self.rows_by_id[resolved_id] = row
        if idempotency_key:
            self.id_by_idemp[idempotency_key] = resolved_id
        return dict(row)

    async def get_job(
        self, job_id: str, *, conn: Any = None
    ) -> dict[str, Any] | None:
        row = self.rows_by_id.get(job_id)
        return dict(row) if row is not None else None

    async def update_versioned(
        self,
        job_id: str,
        *,
        expected_version: int,
        status: str | TranscriptionJobStatus | None = None,
        stage: str | None = None,
        segment_count: int | None = None,
        completed_segments: int | None = None,
        provider_request_ids: list[str] | None = None,
        usage: dict[str, Any] | None = None,
        result_object_key: str | None = None,
        error_category: str | None = None,
        error_message: str | None = None,
        clear_error: bool = False,
        conn: Any = None,
    ) -> tuple[bool, dict[str, Any] | None]:
        row = self.rows_by_id.get(job_id)
        if row is None:
            return False, None
        if int(row["version"]) != int(expected_version):
            return False, dict(row)
        if status is not None:
            row["status"] = (
                status.value
                if isinstance(status, TranscriptionJobStatus)
                else str(status)
            )
        if stage is not None:
            row["stage"] = stage
        if segment_count is not None:
            row["segment_count"] = int(segment_count)
        if completed_segments is not None:
            row["completed_segments"] = int(completed_segments)
        if provider_request_ids is not None:
            row["provider_request_ids"] = list(provider_request_ids)
        if usage is not None:
            row["usage"] = dict(usage)
        if result_object_key is not None:
            row["result_object_key"] = result_object_key
        if clear_error:
            row["error_category"] = None
            row["error_message"] = None
        else:
            if error_category is not None:
                row["error_category"] = error_category
            if error_message is not None:
                row["error_message"] = error_message
        now = datetime.now(timezone.utc).isoformat()
        row["version"] = int(row["version"]) + 1
        row["updated_at"] = now
        if row["status"] in ("succeeded", "failed", "cancelled"):
            row["completed_at"] = now
        return True, dict(row)


def _wire_ledger_and_bridge(
    monkeypatch: pytest.MonkeyPatch,
    ledger: InMemoryTranscriptionLedger,
    bridge: rb.ResearchDataBridge,
) -> None:
    """Wire `db_transcriptions` and `svc_transcriptions` to the in-memory ledger and in-process `ResearchDataBridge` using `create_autospec`."""
    monkeypatch.setenv(
        "DALTON_TRANSCRIPTION_TICKET_SECRET", "unit-test-ticket-hmac-secret-key-32b"
    )
    # A1 总开关：这些用例测的是转写本体行为，统一打开总开关（存储已由下方
    # is_storage_enabled 替身置真）。
    monkeypatch.setenv("DALTON_TRANSCRIPTION_ENABLED", "1")
    monkeypatch.setattr(
        db_transcriptions,
        "create_transcription_job",
        create_autospec(
            db_transcriptions.create_transcription_job, side_effect=ledger.create_job
        ),
    )
    monkeypatch.setattr(
        db_transcriptions,
        "get_transcription_job",
        create_autospec(
            db_transcriptions.get_transcription_job, side_effect=ledger.get_job
        ),
    )
    monkeypatch.setattr(
        db_transcriptions,
        "update_transcription_job_versioned",
        create_autospec(
            db_transcriptions.update_transcription_job_versioned,
            side_effect=ledger.update_versioned,
        ),
    )
    monkeypatch.setattr(
        svc_transcriptions,
        "call_bridge_submit_transcription",
        create_autospec(
            svc_transcriptions.call_bridge_submit_transcription,
            side_effect=bridge.handle_transcription_submit,
        ),
    )
    monkeypatch.setattr(
        svc_transcriptions,
        "call_bridge_get_transcription",
        create_autospec(
            svc_transcriptions.call_bridge_get_transcription,
            side_effect=bridge.handle_transcription_status,
        ),
    )
    monkeypatch.setattr(
        svc_transcriptions,
        "call_bridge_get_transcription_result",
        create_autospec(
            svc_transcriptions.call_bridge_get_transcription_result,
            side_effect=bridge.handle_transcription_result,
        ),
    )

    async def _bridge_action(
        job_id: str, action: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        if action == "retry":
            return await bridge.handle_transcription_retry(job_id, payload)
        if action == "cancel":
            return await bridge.handle_transcription_cancel(job_id, payload)
        return 400, {"ok": False, "error": "invalid_action"}

    monkeypatch.setattr(
        svc_transcriptions,
        "call_bridge_action_transcription",
        create_autospec(
            svc_transcriptions.call_bridge_action_transcription,
            side_effect=_bridge_action,
        ),
    )
    monkeypatch.setattr(
        svc_transcriptions,
        "is_storage_enabled",
        create_autospec(
            svc_transcriptions.is_storage_enabled,
            return_value=True,
        ),
    )

    def _default_object_size(key: str) -> int | None:
        for row in ledger.rows_by_id.values():
            if str(row.get("source_object_key") or "") == str(key or ""):
                return int(row.get("source_size_bytes") or 0)
        return 128000

    monkeypatch.setattr(
        svc_transcriptions,
        "object_size",
        create_autospec(
            svc_transcriptions.object_size,
            side_effect=_default_object_size,
        ),
    )


# ---------------------------------------------------------------------------
# 1. Bridge Controlled Routes: Admin Token vs Normal MCP Token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_transcription_routes_reject_mcp_token_and_require_admin_token(
    tmp_path: Path,
) -> None:
    """Normal MCP token (`DALTON_BRIDGE_TOKEN`) is rejected with 403 on `/transcriptions*`; missing token gets 401; admin token succeeds."""
    bridge = _make_test_bridge(tmp_path, delay_s=0.01)
    submit_payload = {
        "job_id": "asr_bridgeauth01",
        "caller": {
            "user_id": "u-internal-1",
            "workspace_id": "ws-1",
            "thread_id": "th-1",
        },
        "source_object_key": "transcriptions/ws-1/asr_bridgeauth01/source/call.m4a",
        "source_size_bytes": 120000,
        "source_sha256": "a" * 64,
        "language": "zh",
        "hotwords": ["东方雨虹", "防水"],
        "idempotency_key": "idemp-bridgeauth01",
    }

    # 1. Missing token -> 401 Unauthorized on POST /transcriptions, GET /{id}, GET /{id}/result, retry, cancel
    for method, route, body in (
        ("POST", "/transcriptions", submit_payload),
        ("GET", "/transcriptions/asr_bridgeauth01", None),
        ("GET", "/transcriptions/asr_bridgeauth01/result", None),
        ("POST", "/transcriptions/asr_bridgeauth01/retry", {}),
        ("POST", "/transcriptions/asr_bridgeauth01/cancel", {}),
    ):
        code, resp = await _send_raw_http_request(bridge, method, route, {}, body)
        assert code == 401, f"Expected 401 for {method} {route} without token"
        assert resp["ok"] is False

    # 2. Normal MCP token (DALTON_BRIDGE_TOKEN) -> 403 Forbidden on all `/transcriptions*` routes
    mcp_headers = {"Authorization": f"Bearer {bridge.cfg.token}"}
    for method, route, body in (
        ("POST", "/transcriptions", submit_payload),
        ("GET", "/transcriptions/asr_bridgeauth01", None),
        ("GET", "/transcriptions/asr_bridgeauth01/result", None),
        ("POST", "/transcriptions/asr_bridgeauth01/retry", {}),
        ("POST", "/transcriptions/asr_bridgeauth01/cancel", {}),
    ):
        code_mcp, resp_mcp = await _send_raw_http_request(
            bridge, method, route, mcp_headers, body
        )
        assert code_mcp == 403, f"Expected 403 for {method} {route} with MCP token"
        assert resp_mcp["ok"] is False
        assert resp_mcp["error"] == "Forbidden"

    # Also rejected when supplied via X-Dalton-Bridge-Token header
    code_hdr, resp_hdr = await _send_raw_http_request(
        bridge,
        "POST",
        "/transcriptions",
        {"X-Dalton-Bridge-Token": bridge.cfg.token},
        submit_payload,
    )
    assert code_hdr == 403
    assert resp_hdr["ok"] is False

    # 3. Admin token (`DALTON_BRIDGE_ADMIN_TOKEN`) -> 200 OK
    # Even if sandbox-style forged X-Dalton-User header is attached, accounting uses `caller.user_id`
    admin_headers = {
        "Authorization": f"Bearer {bridge.cfg.admin_token}",
        "X-Dalton-User": "forged-sandbox-user",
        "X-Dalton-Workspace": "forged-sandbox-ws",
    }
    code_ok, resp_ok = await _send_raw_http_request(
        bridge, "POST", "/transcriptions", admin_headers, submit_payload
    )
    assert code_ok == 200
    assert resp_ok["ok"] is True
    job_state = resp_ok["job"]
    assert job_state["job_id"] == "asr_bridgeauth01"
    assert job_state["caller"]["user_id"] == "u-internal-1"
    assert job_state["caller"]["workspace_id"] == "ws-1"


# ---------------------------------------------------------------------------
# 2. Bridge Disk State Persistence & Restart Recovery + Fake Worker Modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_asr_jobs_disk_persistence_restart_recovery_and_failure_retry(
    tmp_path: Path,
) -> None:
    """Bridge persists `var/dalton/asr_jobs/<job_id>/state.json` atomically; restarting the bridge recovers jobs and results."""
    bridge1 = _make_test_bridge(
        tmp_path, delay_s=0.01, fixed_text="第一段纪要：管理层展望稳健。"
    )
    submit_payload = {
        "job_id": "asr_persist0001",
        "caller": {
            "user_id": "u-analyst-1",
            "workspace_id": "ws-alpha",
            "thread_id": "th-alpha",
        },
        "source_object_key": "transcriptions/ws-alpha/asr_persist0001/source/meeting.mp3",
        "source_size_bytes": 480000,
        "source_sha256": "b" * 64,
        "language": "zh",
        "hotwords": ["毛利率", "资本开支"],
        "idempotency_key": "idemp-persist0001",
    }

    code_sub, body_sub = await bridge1.handle_transcription_submit(submit_payload)
    assert code_sub == 200
    assert body_sub["job"]["status"] in ("queued", "transcribing", "succeeded")

    # Wait for fake worker to finish (`queued -> transcribing -> succeeded`)
    for _ in range(50):
        code_st, body_st = await bridge1.handle_transcription_status("asr_persist0001")
        assert code_st == 200
        if body_st["job"]["status"] == "succeeded":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Fake worker did not reach succeeded within timeout")

    state_file = bridge1._asr_jobs_dir() / "asr_persist0001" / "state.json"
    result_file = bridge1._asr_jobs_dir() / "asr_persist0001" / "result.json"
    assert state_file.is_file()
    assert result_file.is_file()

    # Simulate bridge process restart by constructing a brand-new `ResearchDataBridge` pointing at the same `tmp_path`
    bridge2 = _make_test_bridge(tmp_path, delay_s=0.01)
    code_st2, body_st2 = await bridge2.handle_transcription_status("asr_persist0001")
    assert code_st2 == 200
    assert body_st2["job"]["status"] == "succeeded"
    assert body_st2["job"]["caller"]["user_id"] == "u-analyst-1"
    assert body_st2["job"]["usage"]["user_id"] == "u-analyst-1"

    code_res2, body_res2 = await bridge2.handle_transcription_result("asr_persist0001")
    assert code_res2 == 200
    assert body_res2["result"]["text"] == "第一段纪要：管理层展望稳健。"
    assert body_res2["caller"]["user_id"] == "u-analyst-1"

    # Configurable failure mode (`fail_category`) -> `queued -> transcribing -> failed`, then retry succeeds
    bridge_fail = _make_test_bridge(
        tmp_path, delay_s=0.01, fail_category="asr_upstream_timeout"
    )
    fail_payload = {
        **submit_payload,
        "job_id": "asr_failretry01",
        "idempotency_key": "idemp-failretry01",
    }
    code_fsub, _ = await bridge_fail.handle_transcription_submit(fail_payload)
    assert code_fsub == 200
    for _ in range(50):
        _, f_st = await bridge_fail.handle_transcription_status("asr_failretry01")
        if f_st["job"]["status"] == "failed":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Fake worker in failure mode did not transition to failed")

    assert f_st["job"]["error_category"] == "asr_upstream_timeout"
    # Result endpoint returns 409 while failed
    code_fres, _ = await bridge_fail.handle_transcription_result("asr_failretry01")
    assert code_fres == 409

    # Retry on a healthy bridge instance (`fail_category=None`) recovers the failed job to `succeeded`
    code_retry, _ = await bridge2.handle_transcription_retry(
        "asr_failretry01",
        {"caller": {"user_id": "u-analyst-1", "workspace_id": "ws-alpha"}},
    )
    assert code_retry == 200
    for _ in range(50):
        _, r_st = await bridge2.handle_transcription_status("asr_failretry01")
        if r_st["job"]["status"] == "succeeded":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Retried job did not reach succeeded")


# ---------------------------------------------------------------------------
# 3. Direct Tool `prepare_transcription`: External Rejection, Sandbox File
#    Checks, 0600 Ticket File & Zero Secret Leakage
# ---------------------------------------------------------------------------


def _make_job_handle(cid: str = "job-1") -> JobHandle:
    return JobHandle(
        JobRef(
            command_id=cid,
            state_dir=f"/.system/jobs/{cid}",
            log_path=f"/tmp/{cid}.log",
            trace_path=f"/tmp/{cid}.jsonl",
            log_relpath=f"{cid}.log",
        ),
        datetime.now(tz=timezone.utc),
    )


def _make_ended(stdout_text: str, *, job_exit_code: int = 0) -> Ended:
    return Ended(_make_job_handle(), exit_code=job_exit_code, output=stdout_text)


@pytest.mark.asyncio
async def test_prepare_transcription_tool_external_rejected_and_internal_issues_0600_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`prepare_transcription` rejects external users, verifies workspace access, writes a 0600 ticket in sandbox, and never leaks the ticket in output or argv."""
    ledger = InMemoryTranscriptionLedger()
    bridge = _make_test_bridge(tmp_path)
    _wire_ledger_and_bridge(monkeypatch, ledger, bridge)

    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = "/home/daytona/workspace"

    tools = build_dalton_tools(filesystem_backend=backend, role="analyst")
    prep_tool = next((t for t in tools if t.name == "prepare_transcription"), None)
    assert prep_tool is not None

    # 1. External member is rejected before touching sandbox or DB
    mock_tool_tier = create_autospec(
        transcription_tools.get_user_tier, return_value="external"
    )
    mock_svc_tier = create_autospec(
        svc_transcriptions.get_user_tier, return_value="external"
    )
    monkeypatch.setattr(transcription_tools, "get_user_tier", mock_tool_tier)
    monkeypatch.setattr(svc_transcriptions, "get_user_tier", mock_svc_tier)
    res_ext = await prep_tool.ainvoke(
        {"path": "audio/interview.m4a", "language": "zh", "hotwords": "腾讯"},
        config={
            "configurable": {
                "user_id": "u-ext-99",
                "workspace_id": "ws-1",
                "thread_id": "th-1",
            }
        },
    )
    ext_payload = json.loads(res_ext)
    assert ext_payload["ok"] is False
    assert ext_payload["error_code"] == "external_member_forbidden"
    backend.start_code_job.assert_not_called()
    assert ledger.rows_by_id == {}

    # 2. Internal member succeeds; verify workspace access check, streaming sha256 inspection, and 0600 ticket write
    mock_tool_tier.return_value = "internal"
    mock_svc_tier.return_value = "internal"
    mock_verify_ws = create_autospec(
        svc_transcriptions.verify_workspace_access, return_value=None
    )
    monkeypatch.setattr(
        svc_transcriptions, "verify_workspace_access", mock_verify_ws
    )

    fake_audio_sha = hashlib.sha256(b"fake-audio-content").hexdigest()
    inspect_payload = json.dumps(
        {
            "ok": True,
            "resolved_root": "/home/daytona/workspace",
            "resolved_target": "/home/daytona/workspace/audio/interview.m4a",
            "size_bytes": 1048576,
            "sha256": fake_audio_sha,
        }
    )
    executed_codes: list[str] = []
    uploaded_files: list[tuple[str, bytes]] = []
    captured_ticket_doc: dict[str, Any] = {}

    async def _fake_aupload_files(files: list[tuple[str, bytes]]) -> list[Any]:
        for rel_p, raw_bytes in files:
            uploaded_files.append((rel_p, raw_bytes))
            captured_ticket_doc.update(json.loads(raw_bytes.decode("utf-8")))
        return []

    async def _fake_start_code_job(code: str, *, wait_s: float = 15.0) -> Ended:
        executed_codes.append(code)
        if "['parent']" in code:
            # The 0700 directory step runs before any ticket byte is uploaded.
            assert not uploaded_files, "ticket uploaded before its directory was made 0700"
            return _make_ended(json.dumps({"ok": True}))
        if "abs_ticket_path" in code:
            m = re.search(r"b64decode\('([^']+)'\)", code)
            assert m is not None
            env_obj = json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
            assert "ticket_payload" not in env_obj
            assert "ticket" not in env_obj
            return _make_ended(
                json.dumps(
                    {
                        "ok": True,
                        "ticket_file": env_obj["rel_ticket_path"],
                        "mode": 0o600,
                        "dir_mode": 0o700,
                    }
                )
            )
        return _make_ended(inspect_payload)

    backend.aupload_files.side_effect = _fake_aupload_files
    backend.start_code_job.side_effect = _fake_start_code_job

    res_ok = await prep_tool.ainvoke(
        {"path": "audio/interview.m4a", "language": "zh", "hotwords": "AI,Blackwell"},
        config={
            "configurable": {
                "user_id": "u-internal-7",
                "workspace_id": "ws-nvda",
                "thread_id": "th-nvda-1",
            }
        },
    )
    parsed_out = json.loads(res_ok)
    assert parsed_out["ok"] is True
    job_id = parsed_out["job_id"]
    ticket_file = parsed_out["ticket_file"]
    assert ticket_file.endswith(f"/{job_id}.ticket.json")

    # Crucial security check: the signed ticket token MUST NOT appear in tool output,
    # command arguments, or written Python script source (neither raw nor base64-encoded).
    assert "ticket" not in parsed_out
    assert len(uploaded_files) == 1
    signed_ticket_token = captured_ticket_doc["ticket"]
    ticket_b64 = base64.b64encode(signed_ticket_token.encode("utf-8")).decode("ascii")
    assert signed_ticket_token not in res_ok
    assert len(executed_codes) == 3
    # [0] inspects the audio, [1] makes the ticket directory 0700, [2] finalizes 0600.
    assert "0o700" in executed_codes[1] and "0o600" not in executed_codes[1]
    assert "0o600" in executed_codes[2]
    assert "0o700" in executed_codes[2]
    for script_src in executed_codes:
        assert signed_ticket_token not in script_src
        assert ticket_b64 not in script_src
        for b64_match in re.findall(r"b64decode\('([^']+)'\)", script_src):
            decoded_src = base64.b64decode(b64_match).decode("utf-8")
            assert signed_ticket_token not in decoded_src
            assert "ticket_payload" not in decoded_src

    # Verify DB ledger row holds real caller identity (`u-internal-7`), sha256, size, and object key
    db_row = ledger.rows_by_id[job_id]
    assert db_row["created_by_user_id"] == "u-internal-7"
    assert db_row["workspace_id"] == "ws-nvda"
    assert db_row["thread_id"] == "th-nvda-1"
    assert db_row["source_size_bytes"] == 1048576
    assert db_row["source_sha256"] == fake_audio_sha
    assert db_row["status"] == TranscriptionJobStatus.AWAITING_UPLOAD.value


# ---------------------------------------------------------------------------
# 4. Colleague Workspace Accounting & Forged `X-Dalton-*` Header Immunity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_colleague_workspace_accounting_and_forged_headers_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When internal user A runs in colleague user B's workspace, accounting records user A (real caller), and forged `X-Dalton-*` headers cannot alter identity."""
    ledger = InMemoryTranscriptionLedger()
    bridge = _make_test_bridge(tmp_path, delay_s=0.01)
    _wire_ledger_and_bridge(monkeypatch, ledger, bridge)

    monkeypatch.setattr(
        svc_transcriptions,
        "get_user_tier",
        create_autospec(svc_transcriptions.get_user_tier, return_value="internal"),
    )
    # Workspace `ws-owned-by-bob` belongs to `u-bob`, but `u-alice` has colleague access
    monkeypatch.setattr(
        svc_transcriptions,
        "verify_workspace_access",
        create_autospec(svc_transcriptions.verify_workspace_access, return_value=None),
    )

    job, ticket, _exp_ts = await svc_transcriptions.prepare_transcription_job(
        user_id="u-alice",
        workspace_id="ws-owned-by-bob",
        thread_id="th-colleague-1",
        source_filename="expert_call.m4a",
        source_size_bytes=256000,
        source_sha256="c" * 64,
        language="zh",
        hotwords="宁德时代",
    )
    job_id = str(job["job_id"])

    # Build ASGI AsyncClient for `/api/v1/dalton/transcriptions*`
    app = FastAPI()
    app.include_router(transcriptions_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Caller sends forged `X-Dalton-User: u-bob` and `X-Dalton-Workspace: ws-hijack` headers
        resp_sub = await client.post(
            "/api/v1/dalton/transcriptions",
            headers={
                "X-Dalton-Transcription-Ticket": ticket,
                "X-Dalton-User": "u-bob",
                "X-Dalton-Workspace": "ws-hijack",
                "X-Dalton-Thread": "th-hijack",
            },
            json={
                "submit": True,
                "job_id": job_id,
                "source_object_key": job["source_object_key"],
                "source_size_bytes": 256000,
                "source_sha256": "c" * 64,
                "language": "zh",
                "hotwords": "宁德时代",
            },
        )
        assert resp_sub.status_code == 200
        sub_data = resp_sub.json()
        assert sub_data["job"]["created_by_user_id"] == "u-alice"
        assert sub_data["job"]["workspace_id"] == "ws-owned-by-bob"
        assert sub_data["job"]["thread_id"] == "th-colleague-1"

        # Wait for bridge fake worker to finish and check bridge-side accounting
        for _ in range(50):
            resp_st = await client.get(
                f"/api/v1/dalton/transcriptions/{job_id}",
                headers={
                    "X-Dalton-Transcription-Ticket": ticket,
                    "X-Dalton-User": "u-bob",
                },
            )
            assert resp_st.status_code == 200
            if resp_st.json()["job"]["status"] == "succeeded":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Job did not complete")

        st_job = resp_st.json()["job"]
        assert st_job["created_by_user_id"] == "u-alice"
        assert st_job["usage"]["user_id"] == "u-alice"
        assert st_job["usage"]["workspace_id"] == "ws-owned-by-bob"


# ---------------------------------------------------------------------------
# 5. Cross-User Isolation, Expired/Tampered Ticket, and Access Revocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_user_job_id_invisible_and_workspace_reader_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Internal employee B without workspace access gets 404 on user A's `job_id`; user C with workspace read access can view status and result."""
    ledger = InMemoryTranscriptionLedger()
    bridge = _make_test_bridge(tmp_path, delay_s=0.0)
    _wire_ledger_and_bridge(monkeypatch, ledger, bridge)

    monkeypatch.setattr(
        svc_transcriptions,
        "get_user_tier",
        create_autospec(svc_transcriptions.get_user_tier, return_value="internal"),
    )

    # Workspace access rule: `u-creator` and `u-colleague-ro` have access to `ws-secret`; `u-other-internal` does NOT
    async def _ws_access(
        workspace_id: str,
        user_id: str | None,
        *,
        need: str = "read",
        channel: str = "web",
    ) -> None:
        if workspace_id == "ws-secret" and user_id in ("u-creator", "u-colleague-ro"):
            return None
        raise HTTPException(status_code=404, detail="Workspace not found")

    monkeypatch.setattr(
        svc_transcriptions,
        "verify_workspace_access",
        create_autospec(
            svc_transcriptions.verify_workspace_access, side_effect=_ws_access
        ),
    )

    job, ticket, _exp_ts = await svc_transcriptions.prepare_transcription_job(
        user_id="u-creator",
        workspace_id="ws-secret",
        thread_id="th-secret",
        source_filename="board.m4a",
        source_size_bytes=64000,
        source_sha256="d" * 64,
    )
    job_id = str(job["job_id"])
    await svc_transcriptions.submit_transcription_job(
        job_id=job_id,
        caller_user_id="u-creator",
        ticket_claims=svc_transcriptions.verify_transcription_ticket(
            ticket, expected_job_id=job_id, required_op="submit"
        ),
    )
    # Wait for fake worker
    for _ in range(30):
        st = await svc_transcriptions.get_transcription_job_status(
            job_id=job_id, caller_user_id="u-creator"
        )
        if st["job"]["status"] == "succeeded":
            break
        await asyncio.sleep(0.01)

    # 1. `u-other-internal` (also internal tier!) tries to query status, result, retry, cancel -> 404 Not Found
    for fn in (
        lambda: svc_transcriptions.get_transcription_job_status(
            job_id=job_id, caller_user_id="u-other-internal"
        ),
        lambda: svc_transcriptions.get_transcription_job_result(
            job_id=job_id, caller_user_id="u-other-internal"
        ),
        lambda: svc_transcriptions.retry_transcription_job(
            job_id=job_id, caller_user_id="u-other-internal"
        ),
        lambda: svc_transcriptions.cancel_transcription_job(
            job_id=job_id, caller_user_id="u-other-internal"
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await fn()
        assert exc_info.value.status_code == 404

    # 2. `u-colleague-ro` (has read access to `ws-secret`) CAN read status and result
    st_colleague = await svc_transcriptions.get_transcription_job_status(
        job_id=job_id, caller_user_id="u-colleague-ro"
    )
    assert st_colleague["job"]["job_id"] == job_id
    res_colleague = await svc_transcriptions.get_transcription_job_result(
        job_id=job_id, caller_user_id="u-colleague-ro"
    )
    assert res_colleague["job_id"] == job_id
    assert "text" in res_colleague["result"]


@pytest.mark.asyncio
async def test_expired_ticket_and_revoked_access_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expired tickets are rejected with 401; demotion to external or loss of workspace access after ticket issuance is rejected with 403."""
    ledger = InMemoryTranscriptionLedger()
    bridge = _make_test_bridge(tmp_path, delay_s=0.0)
    _wire_ledger_and_bridge(monkeypatch, ledger, bridge)

    user_tier = "internal"
    ws_allowed = True

    async def _tier(uid: str | None) -> str:
        return user_tier

    async def _ws_check(
        workspace_id: str,
        user_id: str | None,
        *,
        need: str = "read",
        channel: str = "web",
    ) -> None:
        if not ws_allowed:
            raise HTTPException(status_code=403, detail="Workspace access revoked")

    monkeypatch.setattr(
        svc_transcriptions,
        "get_user_tier",
        create_autospec(svc_transcriptions.get_user_tier, side_effect=_tier),
    )
    monkeypatch.setattr(
        svc_transcriptions,
        "verify_workspace_access",
        create_autospec(
            svc_transcriptions.verify_workspace_access, side_effect=_ws_check
        ),
    )

    job, valid_ticket, _exp_ts = await svc_transcriptions.prepare_transcription_job(
        user_id="u-temp-internal",
        workspace_id="ws-rev",
        thread_id="th-rev",
        source_filename="call.m4a",
        source_size_bytes=32000,
        source_sha256="e" * 64,
    )
    job_id = str(job["job_id"])

    # 1. Expired ticket (`now` past `exp`) -> 401 Unauthorized
    expired_ticket_tok, _ = svc_transcriptions.issue_transcription_ticket(
        job_id=job_id,
        user_id="u-temp-internal",
        workspace_id="ws-rev",
        thread_id="th-rev",
        source_object_key=str(job["source_object_key"]),
        source_size_bytes=32000,
        source_sha256="e" * 64,
        ttl_s=60,
        now_ts=int(time.time()) - 3600,
    )
    with pytest.raises(HTTPException) as exc_exp:
        svc_transcriptions.verify_transcription_ticket(
            expired_ticket_tok, expected_job_id=job_id, required_op="submit"
        )
    assert exc_exp.value.status_code == 401
    assert "expired" in str(exc_exp.value.detail).lower()

    # 2. Ticket bound to `job_id` used on another job -> 404 Not Found
    with pytest.raises(HTTPException) as exc_wrong_job:
        svc_transcriptions.verify_transcription_ticket(
            valid_ticket, expected_job_id="asr_otherjob999", required_op="status"
        )
    assert exc_wrong_job.value.status_code == 404

    # 3. Demoted to `external` AFTER ticket was issued -> submit and status both rejected with 403
    claims = svc_transcriptions.verify_transcription_ticket(
        valid_ticket, expected_job_id=job_id, required_op="submit"
    )
    user_tier = "external"
    with pytest.raises(HTTPException) as exc_demoted_sub:
        await svc_transcriptions.submit_transcription_job(
            job_id=job_id,
            caller_user_id=str(claims["user_id"]),
            ticket_claims=claims,
        )
    assert exc_demoted_sub.value.status_code == 403

    with pytest.raises(HTTPException) as exc_demoted_st:
        await svc_transcriptions.get_transcription_job_status(
            job_id=job_id, caller_user_id=str(claims["user_id"])
        )
    assert exc_demoted_st.value.status_code == 403

    # 4. Restore internal tier, but revoke workspace access (`ws_allowed = False`) -> submit and status rejected with 403
    user_tier = "internal"
    ws_allowed = False
    with pytest.raises(HTTPException) as exc_ws_revoked_sub:
        await svc_transcriptions.submit_transcription_job(
            job_id=job_id,
            caller_user_id=str(claims["user_id"]),
            ticket_claims=claims,
        )
    assert exc_ws_revoked_sub.value.status_code == 403

    with pytest.raises(HTTPException) as exc_ws_revoked_st:
        await svc_transcriptions.get_transcription_job_status(
            job_id=job_id, caller_user_id=str(claims["user_id"])
        )
    assert exc_ws_revoked_st.value.status_code == 403


# ---------------------------------------------------------------------------
# 6. Multi-Worker Versioned Conditional Update (`WHERE job_id = %s AND version = %s`)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_backend_workers_concurrent_versioned_update_do_not_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two backend workers reading `version = 1` and updating concurrently: worker 1 advances to `version = 2`, worker 2's stale `expected_version = 1` fails cleanly and returns worker 1's row."""
    state: dict[str, Any] = {
        "job_id": "asr_multiworker1",
        "created_by_user_id": "u-1",
        "workspace_id": "ws-1",
        "thread_id": "th-1",
        "source_object_key": "transcriptions/ws-1/asr_multiworker1/source/a.m4a",
        "source_size_bytes": 1000,
        "source_sha256": "f" * 64,
        "language": "zh",
        "hotwords": [],
        "status": "queued",
        "stage": "queued",
        "segment_count": 1,
        "completed_segments": 0,
        "provider_request_ids": [],
        "usage": {},
        "result_object_key": None,
        "error_category": None,
        "error_message": None,
        "idempotency_key": "idemp-mw1",
        "version": 1,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "completed_at": None,
    }
    executed_queries: list[tuple[str, tuple[Any, ...]]] = []

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    last_updated_row: dict[str, Any] | None = None

    async def _fake_execute(query: str, params: tuple[Any, ...] = ()) -> None:
        nonlocal last_updated_row
        executed_queries.append((query, tuple(params)))
        if "UPDATE dalton_transcription_jobs" in query:
            expected_ver = params[-1]
            if state["version"] == expected_ver:
                state["status"] = params[0]
                state["version"] += 1
                last_updated_row = dict(state)
            else:
                last_updated_row = None
        elif "FROM dalton_transcription_jobs" in query:
            last_updated_row = dict(state)

    async def _fake_fetchone() -> dict[str, Any] | None:
        return last_updated_row

    cursor.execute.side_effect = _fake_execute
    cursor.fetchone.side_effect = _fake_fetchone

    @asynccontextmanager
    async def _fake_get_db_connection(conn: Any = None):
        db_conn = create_autospec(psycopg.AsyncConnection, instance=True)

        @asynccontextmanager
        async def _cursor_ctx(*args: Any, **kwargs: Any):
            yield cursor

        db_conn.cursor.side_effect = _cursor_ctx
        yield db_conn

    monkeypatch.setattr(
        db_transcriptions,
        "get_db_connection",
        create_autospec(
            db_transcriptions.get_db_connection, side_effect=_fake_get_db_connection
        ),
    )

    # Worker A updates with expected_version=1 -> succeeds (version becomes 2, status becomes 'succeeded')
    ok_a, row_a = await db_transcriptions.update_transcription_job_versioned(
        "asr_multiworker1",
        expected_version=1,
        status=TranscriptionJobStatus.SUCCEEDED,
    )
    assert ok_a is True
    assert row_a is not None
    assert row_a["version"] == 2
    assert row_a["status"] == "succeeded"

    # Worker B (stale view `expected_version=1`) tries to overwrite status with 'transcribing' -> fails conditional WHERE
    ok_b, row_b = await db_transcriptions.update_transcription_job_versioned(
        "asr_multiworker1",
        expected_version=1,
        status=TranscriptionJobStatus.TRANSCRIBING,
    )
    assert ok_b is False
    assert row_b is not None
    assert row_b["version"] == 2
    assert row_b["status"] == "succeeded"
    assert any(
        "WHERE job_id = %s AND version = %s" in " ".join(q.split())
        for q, _ in executed_queries
    )


# ---------------------------------------------------------------------------
# 7. Migration `d022_transcription_jobs` Upgrade / Downgrade Round-Trip
# ---------------------------------------------------------------------------


def test_migration_d022_transcription_jobs_upgrade_and_downgrade_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`d022_transcription_jobs` chains after `d021` and executes reversible SQL in `upgrade()` and `downgrade()`."""
    assert d022_transcription_jobs.revision == "d022"
    assert d022_transcription_jobs.down_revision == "d021"

    executed_sql: list[str] = []
    mock_execute = create_autospec(
        alembic_op.execute, side_effect=lambda sql: executed_sql.append(str(sql))
    )
    monkeypatch.setattr(d022_transcription_jobs.op, "execute", mock_execute)

    d022_transcription_jobs.upgrade()
    assert len(executed_sql) >= 2
    up_sql = "\n".join(executed_sql)
    assert "CREATE TABLE IF NOT EXISTS dalton_transcription_jobs" in up_sql
    for required_col in (
        "job_id",
        "created_by_user_id",
        "workspace_id",
        "thread_id",
        "source_object_key",
        "source_size_bytes",
        "source_sha256",
        "language",
        "hotwords",
        "status",
        "stage",
        "segment_count",
        "completed_segments",
        "provider_request_ids",
        "usage",
        "result_object_key",
        "error_category",
        "idempotency_key",
        "version",
        "created_at",
        "updated_at",
        "completed_at",
    ):
        assert required_col in up_sql, f"Missing column {required_col} in d022 upgrade SQL"
    assert "idx_dalton_transcription_jobs_user_created" in up_sql
    assert "idx_dalton_transcription_jobs_workspace_created" in up_sql
    assert "idx_dalton_transcription_jobs_status_updated" in up_sql

    executed_sql.clear()
    d022_transcription_jobs.downgrade()
    down_sql = "\n".join(executed_sql)
    assert "DROP TABLE IF EXISTS dalton_transcription_jobs" in down_sql


# ---------------------------------------------------------------------------
# 8. A1b fix1 Review Items (5 Explicit Regressions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix1_ticket_credential_never_enters_command_argv_or_script_source() -> None:
    """Fix 1: `_write_ticket_file_in_sandbox` writes the credential via the backend file API and runs a credential-free chmod/replace command."""
    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = "/home/daytona/workspace"

    secret_ticket = "eyJ0eXAiOiJkYWx0b25fYXNyX3RpY2tldCJ9.super_secret_hmac_signature_999"
    ticket_payload = {
        "job_id": "asr_fix1_job01",
        "ticket": secret_ticket,
        "workspace_id": "ws-fix1",
        "thread_id": "th-fix1",
        "source_path": "calls/q1.m4a",
        "source_object_key": "transcriptions/ws-fix1/asr_fix1_job01/source/q1.m4a",
        "source_size_bytes": 4096,
        "source_sha256": "1" * 64,
        "allowed_operations": ["submit", "status", "result", "retry", "cancel"],
        "expires_at": "2026-10-29T02:00:00+00:00",
    }

    uploaded_blobs: list[tuple[str, bytes]] = []
    executed_scripts: list[str] = []

    async def _capture_upload(files: list[tuple[str, bytes]]) -> list[Any]:
        uploaded_blobs.extend(files)
        return []

    async def _capture_code_job(code: str, *, wait_s: float = 15.0) -> Ended:
        executed_scripts.append(code)
        if "['parent']" in code:
            return _make_ended(json.dumps({"ok": True}))
        m = re.search(r"b64decode\('([^']+)'\)", code)
        assert m is not None
        env_obj = json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
        return _make_ended(
            json.dumps(
                {
                    "ok": True,
                    "ticket_file": env_obj["rel_ticket_path"],
                    "mode": 0o600,
                    "dir_mode": 0o700,
                }
            )
        )

    backend.aupload_files.side_effect = _capture_upload
    backend.start_code_job.side_effect = _capture_code_job

    rel_path, err = await transcription_tools._write_ticket_file_in_sandbox(
        backend,
        ws_root="/home/daytona/workspace",
        thread_id="th-fix1",
        job_id="asr_fix1_job01",
        ticket_payload=ticket_payload,
    )
    assert err is None
    assert rel_path == ".agents/threads/th-fix1/transcriptions/asr_fix1_job01.ticket.json"

    # 1. File content was written through `backend.aupload_files` to a temp file
    assert len(uploaded_blobs) == 1
    tmp_rel_path, raw_bytes = uploaded_blobs[0]
    assert tmp_rel_path.startswith(".agents/threads/th-fix1/transcriptions/.asr_fix1_job01.ticket.")
    assert tmp_rel_path.endswith(".tmp")
    assert json.loads(raw_bytes.decode("utf-8"))["ticket"] == secret_ticket

    # 2. The executed command / Python script contains ZERO credential strings (raw or base64)
    # The 0700 directory step, then the finalize step; neither carries the ticket.
    assert len(executed_scripts) == 2
    script = executed_scripts[1]
    assert secret_ticket not in executed_scripts[0]
    secret_b64 = base64.b64encode(secret_ticket.encode("utf-8")).decode("ascii")
    payload_json = json.dumps(ticket_payload, ensure_ascii=False)
    payload_b64 = base64.b64encode(payload_json.encode("utf-8")).decode("ascii")
    assert secret_ticket not in script
    assert "super_secret_hmac_signature_999" not in script
    assert secret_b64 not in script
    assert payload_b64 not in script
    for b64_chunk in re.findall(r"b64decode\('([^']+)'\)", script):
        decoded = base64.b64decode(b64_chunk).decode("utf-8")
        assert secret_ticket not in decoded
        assert "super_secret_hmac_signature_999" not in decoded
        assert "ticket_payload" not in decoded


@pytest.mark.asyncio
async def test_fix2_prepare_transcription_rechecks_live_workspace_access_when_turn_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix 2: Even when `turn_admitted_workspace_id` is present in `RunnableConfig`, `prepare_transcription` checks live workspace read permission; revoking access mid-turn immediately rejects preparation."""
    ledger = InMemoryTranscriptionLedger()
    bridge = _make_test_bridge(tmp_path)
    _wire_ledger_and_bridge(monkeypatch, ledger, bridge)

    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = "/home/daytona/workspace"
    prep_tool = transcription_tools.build_prepare_transcription_tool(
        filesystem_backend=backend
    )

    monkeypatch.setattr(
        transcription_tools,
        "get_user_tier",
        create_autospec(transcription_tools.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        svc_transcriptions,
        "get_user_tier",
        create_autospec(svc_transcriptions.get_user_tier, return_value="internal"),
    )

    access_revoked = True
    verify_calls: list[tuple[str, str | None]] = []

    async def _live_ws_verify(
        workspace_id: str,
        user_id: str | None,
        *,
        need: str = "read",
        channel: str = "web",
    ) -> None:
        verify_calls.append((workspace_id, user_id))
        if access_revoked:
            raise HTTPException(status_code=403, detail="Workspace access revoked by owner")

    monkeypatch.setattr(
        svc_transcriptions,
        "verify_workspace_access",
        create_autospec(
            svc_transcriptions.verify_workspace_access, side_effect=_live_ws_verify
        ),
    )

    # Turn started when user had access (`turn_admitted_workspace_id == "ws-shared"`),
    # but owner revoked access before `prepare_transcription` was called.
    out_revoked = await prep_tool.ainvoke(
        {"path": "audio/call.m4a", "language": "zh"},
        config={
            "configurable": {
                "user_id": "u-colleague-1",
                "workspace_id": "ws-shared",
                "thread_id": "th-shared-1",
                "turn_admitted_workspace_id": "ws-shared",
            }
        },
    )
    assert out_revoked.startswith("ERROR:")
    assert "denied or revoked" in out_revoked
    assert verify_calls == [("ws-shared", "u-colleague-1")]
    backend.start_code_job.assert_not_called()
    assert ledger.rows_by_id == {}


@pytest.mark.asyncio
async def test_fix3_stale_bridge_response_from_old_attempt_or_version_never_overwrites_newer_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix 3: When a delayed `failed` response from attempt 1 arrives after retry has already moved the job to `queued` (attempt 2), `_sync_job_from_bridge_state` discards it and preserves `queued`."""
    ledger = InMemoryTranscriptionLedger()
    bridge = _make_test_bridge(tmp_path)
    _wire_ledger_and_bridge(monkeypatch, ledger, bridge)

    monkeypatch.setattr(
        svc_transcriptions,
        "get_user_tier",
        create_autospec(svc_transcriptions.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        svc_transcriptions,
        "verify_workspace_access",
        create_autospec(svc_transcriptions.verify_workspace_access, return_value=None),
    )

    job, _ticket, _exp = await svc_transcriptions.prepare_transcription_job(
        user_id="u-creator",
        workspace_id="ws-order",
        thread_id="th-order",
        source_filename="call.m4a",
        source_size_bytes=50000,
        source_sha256="2" * 64,
    )
    job_id = str(job["job_id"])

    # Step 1: Attempt 1 is submitted (`attempt=1, version=1, status='queued'`)
    job_a1_queued = await svc_transcriptions._sync_job_from_bridge_state(
        job,
        {
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "attempt": 1,
            "version": 1,
        },
    )
    assert job_a1_queued["status"] == "queued"
    assert job_a1_queued["usage"]["attempt"] == 1
    assert job_a1_queued["usage"]["bridge_version"] == 1

    # Step 2: Attempt 1 fails on bridge (`attempt=1, version=3, status='failed'`)
    stale_failed_snapshot = {
        "job_id": job_id,
        "status": "failed",
        "stage": "failed",
        "attempt": 1,
        "version": 3,
        "error_category": "asr_timeout",
        "error_message": "Attempt 1 timed out",
    }
    job_a1_failed = await svc_transcriptions._sync_job_from_bridge_state(
        job_a1_queued, stale_failed_snapshot
    )
    assert job_a1_failed["status"] == "failed"
    assert job_a1_failed["error_category"] == "asr_timeout"

    # Step 3: User retries -> bridge starts attempt 2 (`attempt=2, version=4, status='queued'`)
    job_a2_queued = await svc_transcriptions._sync_job_from_bridge_state(
        job_a1_failed,
        {
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "attempt": 2,
            "version": 4,
            "error_category": None,
            "error_message": None,
        },
        allow_reset_from_terminal=True,
    )
    assert job_a2_queued["status"] == "queued"
    assert job_a2_queued["error_category"] is None
    assert job_a2_queued["usage"]["attempt"] == 2
    assert job_a2_queued["usage"]["bridge_version"] == 4

    # Step 4a: Delayed `failed` response from attempt 1 (`attempt=1, version=3`) arrives against current row -> discarded!
    after_stale_direct = await svc_transcriptions._sync_job_from_bridge_state(
        job_a2_queued, stale_failed_snapshot
    )
    assert after_stale_direct["status"] == "queued"
    assert after_stale_direct["error_category"] is None
    assert after_stale_direct["usage"]["attempt"] == 2
    assert after_stale_direct["usage"]["bridge_version"] == 4

    # Step 4b: Even if a concurrent worker held the pre-retry `job_a1_queued` snapshot in memory when
    # the delayed `failed` response from attempt 1 arrived, the CAS reload sees attempt 2 in DB and discards it!
    after_stale_cas = await svc_transcriptions._sync_job_from_bridge_state(
        job_a1_queued, stale_failed_snapshot
    )
    assert after_stale_cas["status"] == "queued"
    assert after_stale_cas["error_category"] is None
    assert after_stale_cas["usage"]["attempt"] == 2
    assert ledger.rows_by_id[job_id]["status"] == "queued"

    # Step 5: Within attempt 2, a newer `transcribing` (`attempt=2, version=5`) is accepted,
    # and a stale `queued` (`attempt=2, version=4`) is rejected (monotonic version within attempt).
    job_a2_transcribing = await svc_transcriptions._sync_job_from_bridge_state(
        job_a2_queued,
        {
            "job_id": job_id,
            "status": "transcribing",
            "stage": "transcribing",
            "attempt": 2,
            "version": 5,
        },
    )
    assert job_a2_transcribing["status"] == "transcribing"
    assert job_a2_transcribing["usage"]["bridge_version"] == 5

    after_same_attempt_old_ver = await svc_transcriptions._sync_job_from_bridge_state(
        job_a2_transcribing,
        {
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "attempt": 2,
            "version": 4,
        },
    )
    assert after_same_attempt_old_ver["status"] == "transcribing"
    assert after_same_attempt_old_ver["usage"]["bridge_version"] == 5


@pytest.mark.asyncio
async def test_fix4_bridge_process_exit_cancellation_marks_interrupted_and_resumes_on_restart(
    tmp_path: Path,
) -> None:
    """Fix 4: `CancelledError` from bridge shutdown (`asyncio.run` cleanup) keeps the job non-terminal (`interrupted=True`, not `cancelled`), and restarting the bridge resumes it to `succeeded`."""
    # Start bridge1 with a longer fake worker delay so the job stays in `transcribing`
    bridge1 = _make_test_bridge(
        tmp_path, delay_s=5.0, fixed_text="进程重启后自动恢复完成的转写文本。"
    )
    submit_payload = {
        "job_id": "asr_shutdown01",
        "caller": {
            "user_id": "u-internal-1",
            "workspace_id": "ws-restart",
            "thread_id": "th-restart",
        },
        "source_object_key": "transcriptions/ws-restart/asr_shutdown01/source/long.m4a",
        "source_size_bytes": 128000,
        "source_sha256": "3" * 64,
        "language": "zh",
        "hotwords": [],
        "idempotency_key": "idemp-shutdown01",
    }
    code_sub, _ = await bridge1.handle_transcription_submit(submit_payload)
    assert code_sub == 200

    mgr1 = bridge1._asr_job_manager()
    for _ in range(30):
        st = mgr1.read_state("asr_shutdown01")
        if st and st.get("status") == "transcribing":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Worker did not enter transcribing state")

    # Simulate bridge process exit (`asyncio.run` cancels all in-flight tasks without calling `cancel_job`)
    running_task = mgr1._tasks.get("asr_shutdown01")
    assert running_task is not None and not running_task.done()
    running_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running_task

    interrupted_state = mgr1.read_state("asr_shutdown01")
    assert interrupted_state is not None
    # Must NOT be marked `cancelled`!
    assert interrupted_state["status"] == "transcribing"
    assert interrupted_state["interrupted"] is True
    assert interrupted_state["completed_at"] is None

    # Restart bridge (new instance with fast delay_s=0.01): job automatically recovers and finishes to `succeeded`
    bridge2 = _make_test_bridge(
        tmp_path, delay_s=0.01, fixed_text="进程重启后自动恢复完成的转写文本。"
    )
    for _ in range(50):
        code_st, body_st = await bridge2.handle_transcription_status("asr_shutdown01")
        assert code_st == 200
        if body_st["job"]["status"] == "succeeded":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Interrupted job did not recover to succeeded after bridge restart")

    final_job = body_st["job"]
    assert final_job["status"] == "succeeded"
    assert "interrupted" not in final_job
    code_res, body_res = await bridge2.handle_transcription_result("asr_shutdown01")
    assert code_res == 200
    assert body_res["result"]["text"] == "进程重启后自动恢复完成的转写文本。"


@pytest.mark.asyncio
async def test_fix5_only_creator_can_retry_colleague_with_write_access_rejected_with_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix 5: Colleague B with workspace write access cannot retry creator A's job (gets 404 Not Found); creator A retrying succeeds and bills to A."""
    ledger = InMemoryTranscriptionLedger()
    bridge_fail = _make_test_bridge(
        tmp_path, delay_s=0.01, fail_category="asr_transient_error"
    )
    _wire_ledger_and_bridge(monkeypatch, ledger, bridge_fail)

    monkeypatch.setattr(
        svc_transcriptions,
        "get_user_tier",
        create_autospec(svc_transcriptions.get_user_tier, return_value="internal"),
    )
    # Both `u-creator-a` and `u-colleague-b` have full write access to `ws-team`
    monkeypatch.setattr(
        svc_transcriptions,
        "verify_workspace_access",
        create_autospec(svc_transcriptions.verify_workspace_access, return_value=None),
    )

    job, ticket_a, _exp = await svc_transcriptions.prepare_transcription_job(
        user_id="u-creator-a",
        workspace_id="ws-team",
        thread_id="th-team-1",
        source_filename="earnings.m4a",
        source_size_bytes=96000,
        source_sha256="4" * 64,
    )
    job_id = str(job["job_id"])
    await svc_transcriptions.submit_transcription_job(
        job_id=job_id,
        caller_user_id="u-creator-a",
        ticket_claims=svc_transcriptions.verify_transcription_ticket(
            ticket_a, expected_job_id=job_id, required_op="submit"
        ),
    )

    for _ in range(50):
        st = await svc_transcriptions.get_transcription_job_status(
            job_id=job_id, caller_user_id="u-creator-a"
        )
        if st["job"]["status"] == "failed":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Job did not reach failed state")

    # Colleague B can read the failed job's status in the shared workspace...
    st_b = await svc_transcriptions.get_transcription_job_status(
        job_id=job_id, caller_user_id="u-colleague-b"
    )
    assert st_b["job"]["status"] == "failed"

    # ...but Colleague B is forbidden from retrying Creator A's job (returns 404 identical to non-existent job)
    with pytest.raises(HTTPException) as exc_b_retry:
        await svc_transcriptions.retry_transcription_job(
            job_id=job_id, caller_user_id="u-colleague-b"
        )
    assert exc_b_retry.value.status_code == 404
    assert exc_b_retry.value.detail == "Transcription job not found"

    # Switch bridge worker to healthy (`fail_category=None`) and verify Creator A retrying succeeds and bills to A
    bridge_fail._asr_job_manager().worker.config.fail_category = None
    bridge_fail._asr_job_manager().worker.config.fixed_text = "A自己重试成功的转写结果。"

    retried = await svc_transcriptions.retry_transcription_job(
        job_id=job_id, caller_user_id="u-creator-a"
    )
    assert retried["ok"] is True
    assert retried["job"]["created_by_user_id"] == "u-creator-a"
    assert retried["job"]["usage"]["attempt"] == 2

    for _ in range(50):
        st_a = await svc_transcriptions.get_transcription_job_status(
            job_id=job_id, caller_user_id="u-creator-a"
        )
        if st_a["job"]["status"] == "succeeded":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Creator A's retried job did not reach succeeded")

    assert st_a["job"]["created_by_user_id"] == "u-creator-a"
    assert st_a["job"]["usage"]["user_id"] == "u-creator-a"
    assert st_a["job"]["usage"]["attempt"] == 2

