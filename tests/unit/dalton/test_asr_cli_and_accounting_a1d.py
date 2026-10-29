"""Unit tests for Backlog A1d: sandbox CLI (`dalton_asr.py`), background long command contract, recovery backoff, and usage accounting/reconciliation."""

from __future__ import annotations

import asyncio
import email.message
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
from typing import Any
from unittest.mock import create_autospec
import urllib.error
import urllib.parse
import urllib.request
import urllib.response

from fastapi import FastAPI
import httpx
import pytest

from src.dalton.app.transcriptions import router as transcriptions_router
from src.dalton.database import transcriptions as db_transcriptions
from src.dalton.services import transcriptions as svc_transcriptions
from src.server.utils.api import get_current_user_id

REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_DIR = REPO_ROOT / "scripts" / "dalton"
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import asr_jobs  # noqa: E402
import asr_usage_report  # noqa: E402

from tests.unit.dalton.test_transcriptions import (  # noqa: E402
    InMemoryTranscriptionLedger,
    _make_test_bridge,
    _wire_ledger_and_bridge,
)

# One copy, under the Dalton skill naming convention (`dalton-*`); a second,
# identical `transcript-polish/` directory would register as a second skill.
CLI_SCRIPT_DALTON_PATH = (
    REPO_ROOT
    / "plugins"
    / "dalton"
    / "skills"
    / "dalton-transcript-polish"
    / "scripts"
    / "dalton_asr.py"
)

CLI_SCRIPT_DESIGN_PATH = CLI_SCRIPT_DALTON_PATH


def _load_cli_module(script_path: Path = CLI_SCRIPT_DESIGN_PATH) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"dalton_asr_{script_path.parent.parent.name.replace('-', '_')}",
        script_path,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


dalton_asr = _load_cli_module(CLI_SCRIPT_DESIGN_PATH)


def _write_0600_ticket_file(
    tmp_path: Path,
    *,
    job_id: str = "asr_job_a1d_001",
    ticket: str = "eyJ2IjoxLCJ0eXAiOiJkYWx0b25fYXNyX3RpY2tldCJ9.secret_hmac_signature_value_123456",
    mode: int = 0o600,
    audio_bytes: bytes | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    audio_rel = "audio/meeting.m4a"
    audio_file = tmp_path / audio_rel
    audio_file.parent.mkdir(parents=True, exist_ok=True)
    raw_audio = (
        audio_bytes
        if audio_bytes is not None
        else (b"DALTON_TEST_M4A_AUDIO_" + (b"\x01\x02\x03\x04" * 1024))
    )
    if not audio_file.exists() or audio_bytes is not None:
        audio_file.write_bytes(raw_audio)
    actual_bytes = audio_file.read_bytes()
    actual_sha256 = hashlib.sha256(actual_bytes).hexdigest()

    ticket_path = tmp_path / f"{job_id}.ticket.json"
    payload: dict[str, Any] = {
        "job_id": job_id,
        "ticket": ticket,
        "workspace_id": "ws-test-1",
        "thread_id": "th-test-1",
        "source_path": audio_rel,
        "source_object_key": f"transcriptions/ws-test-1/{job_id}/source/meeting.m4a",
        "source_size_bytes": len(actual_bytes),
        "source_sha256": actual_sha256,
        "allowed_operations": [
            "upload",
            "submit",
            "status",
            "result",
            "retry",
            "cancel",
        ],
    }
    if extra:
        payload.update(extra)
    ticket_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(ticket_path, mode)
    return ticket_path


# ---------------------------------------------------------------------------
# 1. Ticket File Permissions (`0600` Required) & Symlink Rejection
# ---------------------------------------------------------------------------


def test_cli_rejects_non_0600_ticket_permissions_and_symlinks(tmp_path: Path) -> None:
    """Ticket file with permissions != 0600 (e.g. 0644, 0640, 0666, 0400, 0700) or symlink is rejected before any network call."""
    secret_tok = "eyJ2IjoxLCJ0eXAiOiJkYWx0b25fYXNyX3RpY2tldCJ9.super_secret_sig_do_not_leak_999"

    for bad_mode in (0o644, 0o640, 0o666, 0o400, 0o700):
        tfile = _write_0600_ticket_file(
            tmp_path,
            job_id=f"job_mode_{oct(bad_mode)}",
            ticket=secret_tok,
            mode=bad_mode,
        )
        out = io.StringIO()
        urlopen_spy = create_autospec(urllib.request.urlopen)
        rc = dalton_asr.main(
            ["status", "--ticket-file", str(tfile)],
            environ={"PATH": "/usr/bin:/bin"},
            urlopen_fn=urlopen_spy,
            stream=out,
        )
        assert rc == 2
        urlopen_spy.assert_not_called()
        out_text = out.getvalue().strip()
        assert secret_tok not in out_text
        parsed = json.loads(out_text)
        assert parsed["ok"] is False
        assert parsed["error_code"] == "invalid_ticket_permissions"
        assert "0600" in parsed["error"]

    # Symlink to a valid 0600 ticket file must also be rejected
    valid_tfile = _write_0600_ticket_file(
        tmp_path,
        job_id="job_valid_0600",
        ticket=secret_tok,
        mode=0o600,
    )
    symlink_tfile = tmp_path / "symlink.ticket.json"
    symlink_tfile.symlink_to(valid_tfile)

    out_sym = io.StringIO()
    urlopen_spy_sym = create_autospec(urllib.request.urlopen)
    rc_sym = dalton_asr.main(
        ["status", "--ticket-file", str(symlink_tfile)],
        environ={"PATH": "/usr/bin:/bin"},
        urlopen_fn=urlopen_spy_sym,
        stream=out_sym,
    )
    assert rc_sym == 2
    urlopen_spy_sym.assert_not_called()
    parsed_sym = json.loads(out_sym.getvalue().strip())
    assert parsed_sym["ok"] is False
    assert parsed_sym["error_code"] == "ticket_file_symlink"


# ---------------------------------------------------------------------------
# 2. Rejects Credentials from CLI Arguments or Environment Variables +
#    Endpoint & Sandbox URL Resolution
# ---------------------------------------------------------------------------


def test_cli_rejects_credentials_from_args_or_env_and_restricts_endpoints(
    tmp_path: Path,
) -> None:
    """Credentials passed via CLI flags or environment variables are rejected; non-transcription endpoints are refused."""
    secret_tok = "eyJ2IjoxLCJ0eXAiOiJkYWx0b25fYXNyX3RpY2tldCJ9.secret_sig_env_or_arg_888"
    valid_tfile = _write_0600_ticket_file(
        tmp_path, job_id="job_arg_env", ticket=secret_tok, mode=0o600
    )
    urlopen_spy = create_autospec(urllib.request.urlopen)

    # 1. Forbidden CLI flags (--ticket, --token, --auth-token, --api-key, --secret)
    for bad_argv in (
        ["status", "--ticket-file", str(valid_tfile), "--ticket", secret_tok],
        ["status", f"--ticket={secret_tok}", "--ticket-file", str(valid_tfile)],
        ["status", "--ticket-file", str(valid_tfile), "--token", secret_tok],
        ["status", "--ticket-file", str(valid_tfile), "--auth-token", secret_tok],
        ["status", "--ticket-file", str(valid_tfile), "--api-key", secret_tok],
        ["status", "--ticket-file", str(valid_tfile), "--secret", secret_tok],
    ):
        out = io.StringIO()
        rc = dalton_asr.main(
            bad_argv,
            environ={"PATH": "/usr/bin:/bin"},
            urlopen_fn=urlopen_spy,
            stream=out,
        )
        assert rc == 2
        urlopen_spy.assert_not_called()
        raw_out = out.getvalue().strip()
        assert secret_tok not in raw_out
        parsed = json.loads(raw_out)
        assert parsed["ok"] is False
        assert parsed["error_code"] == "credential_arg_forbidden"

    # 2. Forbidden environment variables
    for env_key in dalton_asr.FORBIDDEN_CREDENTIAL_ENV_VARS:
        out_env = io.StringIO()
        rc_env = dalton_asr.main(
            ["status", "--ticket-file", str(valid_tfile)],
            environ={"PATH": "/usr/bin:/bin", env_key: secret_tok},
            urlopen_fn=urlopen_spy,
            stream=out_env,
        )
        assert rc_env == 2
        urlopen_spy.assert_not_called()
        raw_env_out = out_env.getvalue().strip()
        assert secret_tok not in raw_env_out
        parsed_env = json.loads(raw_env_out)
        assert parsed_env["ok"] is False
        assert parsed_env["error_code"] == "credential_env_forbidden"

    # 3. Sandbox backend URL resolution order (DALTON_API_URL -> LANGALPHA_API_URL -> egress_relay.json -> default)
    egress_cfg = tmp_path / "egress_relay.json"
    egress_cfg.write_text(
        json.dumps({"relay_base_url": "http://10.0.0.5:8000/"}), encoding="utf-8"
    )
    assert (
        dalton_asr.resolve_backend_base_url(
            {},
            environ={"DALTON_API_URL": "http://127.0.0.1:18080/"},
            egress_config_paths=[egress_cfg],
        )
        == "http://127.0.0.1:18080"
    )
    assert (
        dalton_asr.resolve_backend_base_url(
            {},
            environ={},
            egress_config_paths=[egress_cfg],
        )
        == "http://10.0.0.5:8000"
    )

    # 4. Endpoint restriction: only /api/v1/dalton/transcriptions* is allowed
    assert (
        dalton_asr.build_transcription_endpoint_url(
            "http://127.0.0.1:8000", "/job_1/result"
        )
        == "http://127.0.0.1:8000/api/v1/dalton/transcriptions/job_1/result"
    )
    with pytest.raises(dalton_asr.DaltonAsrCliError) as exc_path:
        dalton_asr.build_transcription_endpoint_url(
            "http://127.0.0.1:8000", "/../../admin/secrets"
        )
    assert exc_path.value.error_code == "forbidden_endpoint_path"


# ---------------------------------------------------------------------------
# 3. `run` Full Flow Against a Local Fake HTTP Server (`ThreadingHTTPServer`)
#    + Separate Raw JSON vs Merged Transcript + Zero Transcript/Secret in Stdout
# ---------------------------------------------------------------------------


@pytest.fixture
def local_fake_backend_server(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Provide an in-process stdlib `urllib.request.BaseHandler` fake HTTP backend emulating `/api/v1/dalton/transcriptions*` without binding a kernel socket."""
    state: dict[str, Any] = {
        "job_id": "asr_fullflow_01",
        "ticket": "eyJ2IjoxLCJ0eXAiOiJkYWx0b25fYXNyX3RpY2tldCJ9.fullflow_secret_signature_777777",
        "base_url": "http://127.0.0.1:18080",
        "status_sequence": [
            {
                "status": "awaiting_upload",
                "stage": "awaiting_upload",
                "segment_count": 0,
                "completed_segments": 0,
                "updated_at": "2026-10-29T01:00:00Z",
            },
            {
                "status": "downloading",
                "stage": "downloading",
                "segment_count": 2,
                "completed_segments": 0,
                "updated_at": "2026-10-29T01:00:02Z",
            },
            {
                "status": "transcribing",
                "stage": "transcribing",
                "segment_count": 2,
                "completed_segments": 1,
                "updated_at": "2026-10-29T01:00:05Z",
            },
            {
                "status": "succeeded",
                "stage": "succeeded",
                "segment_count": 2,
                "completed_segments": 2,
                "updated_at": "2026-10-29T01:00:10Z",
            },
        ],
        "poll_idx": 0,
        "submitted_bodies": [],
        "requests_log": [],
        "transient_failures_before_poll": [],
        "cancelled": False,
        "retried": False,
        "upload_id": "mp-upload-test-001",
        "uploaded_parts": {},
        "upload_completed": False,
        "upload_aborted": False,
        "raw_response_available": True,
        "secret_transcript_text": (
            "[S1-Speaker 1]\n这是高度机密的专家访谈逐字稿正文第一段，绝不可出现在命令标准输出中。\n\n"
            "[S2-Speaker 1]\n这是第二分段管理层关于 2026 年资本开支指引的详细回答。"
        ),
    }

    def _make_response(
        req: urllib.request.Request, code: int, payload: dict[str, Any]
    ) -> urllib.response.addinfourl:
        body_b = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs = email.message.Message()
        hdrs["Content-Type"] = "application/json; charset=utf-8"
        hdrs["Content-Length"] = str(len(body_b))
        fp = io.BytesIO(body_b)
        if code >= 400:
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                f"HTTP {code}",
                hdrs,
                fp,
            )
        resp = urllib.response.addinfourl(fp, hdrs, req.full_url, code)
        resp.msg = f"HTTP {code}"  # type: ignore[attr-defined]
        return resp

    def _get_header(req: urllib.request.Request, name: str) -> str:
        target = name.lower()
        for k, v in req.header_items():
            if k.lower() == target:
                return str(v)
        return ""

    def _dispatch_request(req: urllib.request.Request) -> urllib.response.addinfourl:
        method = req.get_method().upper()
        path = urllib.parse.urlsplit(req.full_url).path
        tk = _get_header(req, "X-Dalton-Transcription-Ticket")
        jid = state["job_id"]

        if method == "GET":
            state["requests_log"].append(("GET", path))
            if tk != state["ticket"]:
                return _make_response(req, 401, {"ok": False, "detail": "Invalid ticket"})

            if path == f"/api/v1/dalton/transcriptions/{jid}":
                if state["poll_idx"] >= 1 and state["transient_failures_before_poll"]:
                    fail_spec = state["transient_failures_before_poll"].pop(0)
                    if isinstance(fail_spec, int):
                        return _make_response(
                            req,
                            fail_spec,
                            {"ok": False, "detail": f"Simulated HTTP {fail_spec}"},
                        )
                    if isinstance(fail_spec, dict):
                        return _make_response(req, 200, fail_spec)
                seq = state["status_sequence"]
                idx = min(state["poll_idx"], len(seq) - 1)
                state["poll_idx"] += 1
                snap = dict(seq[idx])
                snap["job_id"] = jid
                return _make_response(
                    req,
                    200,
                    {"ok": True, "job": snap, "bridge_sync_status": "ok"},
                )

            if path == f"/api/v1/dalton/transcriptions/{jid}/result":
                return _make_response(
                    req,
                    200,
                    {
                        "ok": True,
                        "job_id": jid,
                        "status": "succeeded",
                        "usage": {
                            "user_id": "u-analyst-real",
                            "duration_seconds": 1200.0,
                            "segment_count": 2,
                            "completed_segments": 2,
                        },
                        "result": {
                            "job_id": jid,
                            "language": "zh-CN",
                            "duration_seconds": 1200.0,
                            "text": state["secret_transcript_text"],
                            "provider_request_ids": ["req-s1", "req-s2"],
                            "segments": [
                                {
                                    "index": 0,
                                    "start_s": 0.0,
                                    "end_s": 600.0,
                                    "status": "succeeded",
                                    "provider_request_id": "req-s1",
                                },
                                {
                                    "index": 1,
                                    "start_s": 600.0,
                                    "end_s": 1200.0,
                                    "status": "succeeded",
                                    "provider_request_id": "req-s2",
                                },
                            ],
                            "utterances": [
                                {
                                    "segment_index": 0,
                                    "segment_label": "S1",
                                    "speaker": "S1-Speaker 1",
                                    "raw_start_ms": 500,
                                    "raw_end_ms": 4500,
                                    "start_ms": 500,
                                    "end_ms": 4500,
                                    "text": "这是高度机密的专家访谈逐字稿正文第一段，绝不可出现在命令标准输出中。",
                                },
                                {
                                    "segment_index": 1,
                                    "segment_label": "S2",
                                    "speaker": "S2-Speaker 1",
                                    "raw_start_ms": 1000,
                                    "raw_end_ms": 6000,
                                    "start_ms": 601000,
                                    "end_ms": 606000,
                                    "text": "这是第二分段管理层关于 2026 年资本开支指引的详细回答。",
                                },
                            ],
                        },
                    },
                )

            if path == f"/api/v1/dalton/transcriptions/{jid}/result/raw":
                if not state.get("raw_response_available", True):
                    return _make_response(
                        req, 404, {"ok": False, "detail": "Raw provider response not available"}
                    )
                return _make_response(
                    req,
                    200,
                    {
                        "job_id": jid,
                        "provider": "volcengine_seed_asr",
                        "language": "zh-CN",
                        "duration_seconds": 1200.0,
                        "segments": [
                            {
                                "index": 0,
                                "start_s": 0.0,
                                "end_s": 600.0,
                                "status": "succeeded",
                                "provider_request_id": "req-s1",
                            },
                            {
                                "index": 1,
                                "start_s": 600.0,
                                "end_s": 1200.0,
                                "status": "succeeded",
                                "provider_request_id": "req-s2",
                            },
                        ],
                        "utterances": [
                            {
                                "segment_index": 0,
                                "segment_label": "S1",
                                "speaker": "S1-Speaker 1",
                                "raw_start_ms": 500,
                                "raw_end_ms": 4500,
                                "start_ms": 500,
                                "end_ms": 4500,
                                "text": "这是高度机密的专家访谈逐字稿正文第一段，绝不可出现在命令标准输出中。",
                            },
                            {
                                "segment_index": 1,
                                "segment_label": "S2",
                                "speaker": "S2-Speaker 1",
                                "raw_start_ms": 1000,
                                "raw_end_ms": 6000,
                                "start_ms": 601000,
                                "end_ms": 606000,
                                "text": "这是第二分段管理层关于 2026 年资本开支指引的详细回答。",
                            },
                        ],
                    },
                )

            return _make_response(req, 404, {"ok": False, "detail": "Not found"})

        if method == "PUT":
            raw_put = req.data or b""
            parsed_put = urllib.parse.urlsplit(req.full_url)
            qs = urllib.parse.parse_qs(parsed_put.query)
            pnum = int((qs.get("partNumber") or ["1"])[0])
            etag = f'"etag-part-{pnum}-{len(raw_put)}"'
            state["uploaded_parts"][pnum] = {
                "part_number": pnum,
                "etag": etag,
                "size": len(raw_put),
                "data": raw_put,
            }
            state["requests_log"].append(("PUT", parsed_put.path, {"part_number": pnum, "size": len(raw_put)}))
            resp = _make_response(req, 200, {"ok": True})
            resp.headers["ETag"] = etag
            return resp

        if method == "POST":
            raw_b = req.data or b"{}"
            body = json.loads(raw_b.decode("utf-8")) if raw_b else {}
            state["requests_log"].append(("POST", path, body))
            if tk != state["ticket"]:
                return _make_response(req, 401, {"ok": False, "detail": "Invalid ticket"})

            if path == f"/api/v1/dalton/transcriptions/{jid}/upload/start":
                part_size = int(state.get("part_size") or dalton_asr.UPLOAD_PART_BYTES)
                src_size = int(state.get("source_size_bytes") or 4118)
                part_count = max(1, (src_size + part_size - 1) // part_size)
                existing_list = [
                    {
                        "part_number": p["part_number"],
                        "etag": p["etag"],
                        "size": p["size"],
                    }
                    for p in sorted(state["uploaded_parts"].values(), key=lambda x: x["part_number"])
                ]
                return _make_response(
                    req,
                    200,
                    {
                        "ok": True,
                        "job_id": jid,
                        "upload_id": state["upload_id"],
                        "part_size": part_size,
                        "part_count": part_count,
                        "source_size_bytes": src_size,
                        "uploaded_parts": existing_list,
                        "resumed": bool(existing_list),
                    },
                )

            if path == f"/api/v1/dalton/transcriptions/{jid}/upload/part":
                pnum = int(body.get("part_number") or 1)
                signed_url = (
                    f"{state['base_url']}/fake-s3/{jid}/part"
                    f"?uploadId={state['upload_id']}&partNumber={pnum}"
                )
                return _make_response(
                    req,
                    200,
                    {
                        "ok": True,
                        "job_id": jid,
                        "upload_id": state["upload_id"],
                        "part_number": pnum,
                        "upload_url": signed_url,
                        "expires_in": 900,
                    },
                )

            if path == f"/api/v1/dalton/transcriptions/{jid}/upload/complete":
                state["upload_completed"] = True
                total_sz = sum(p["size"] for p in state["uploaded_parts"].values())
                return _make_response(
                    req,
                    200,
                    {
                        "ok": True,
                        "job_id": jid,
                        "upload_id": state["upload_id"],
                        "completed": True,
                        "object_size": total_sz,
                        "parts_completed": len(state["uploaded_parts"]),
                    },
                )

            if path == f"/api/v1/dalton/transcriptions/{jid}/upload/abort":
                state["upload_aborted"] = True
                state["uploaded_parts"].clear()
                return _make_response(
                    req,
                    200,
                    {
                        "ok": True,
                        "job_id": jid,
                        "upload_id": state["upload_id"],
                        "aborted": True,
                    },
                )

            if path == "/api/v1/dalton/transcriptions":
                state["submitted_bodies"].append(body)
                return _make_response(
                    req,
                    200,
                    {
                        "ok": True,
                        "job": {
                            "job_id": jid,
                            "status": "queued",
                            "stage": "queued",
                            "segment_count": 2,
                            "completed_segments": 0,
                            "updated_at": "2026-10-29T01:00:01Z",
                        },
                    },
                )

            if path == f"/api/v1/dalton/transcriptions/{jid}/cancel":
                state["cancelled"] = True
                return _make_response(
                    req,
                    200,
                    {
                        "ok": True,
                        "job": {
                            "job_id": jid,
                            "status": "cancelled",
                            "stage": "cancelled",
                            "segment_count": 3,
                            "completed_segments": 1,
                            "provider_request_ids": ["req-s1", "req-s2"],
                        },
                        "cancellation": {
                            "submitted_segments_count": 1,
                            "submitted_request_ids": ["req-s2"],
                            "unsubmitted_cancelled_count": 1,
                            "completed_segments_count": 1,
                            "provider_cancel_verified": False,
                            "billing_verified": False,
                            "notice": dalton_asr.PROVIDER_CANCEL_UNVERIFIED_NOTICE,
                        },
                    },
                )

            if path == f"/api/v1/dalton/transcriptions/{jid}/retry":
                state["retried"] = True
                return _make_response(
                    req,
                    200,
                    {
                        "ok": True,
                        "job": {
                            "job_id": jid,
                            "status": "queued",
                            "stage": "queued",
                            "attempt": 2,
                            "segment_count": 2,
                            "completed_segments": 1,
                            "updated_at": "2026-10-29T01:05:00Z",
                        },
                    },
                )

            if path == f"/api/v1/dalton/transcriptions/{jid}/result/raw":
                if not state.get("raw_response_available", True):
                    return _make_response(
                        req, 404, {"ok": False, "detail": "Raw provider response not available"}
                    )
                return _make_response(
                    req,
                    200,
                    {
                        "job_id": jid,
                        "provider": "volcengine_seed_asr",
                        "language": "zh-CN",
                        "duration_seconds": 1200.0,
                        "segments": [
                            {
                                "index": 0,
                                "start_s": 0.0,
                                "end_s": 600.0,
                                "status": "succeeded",
                                "provider_request_id": "req-s1",
                            },
                            {
                                "index": 1,
                                "start_s": 600.0,
                                "end_s": 1200.0,
                                "status": "succeeded",
                                "provider_request_id": "req-s2",
                            },
                        ],
                        "utterances": [
                            {
                                "segment_index": 0,
                                "segment_label": "S1",
                                "speaker": "S1-Speaker 1",
                                "raw_start_ms": 500,
                                "raw_end_ms": 4500,
                                "start_ms": 500,
                                "end_ms": 4500,
                                "text": "这是高度机密的专家访谈逐字稿正文第一段，绝不可出现在命令标准输出中。",
                            },
                            {
                                "segment_index": 1,
                                "segment_label": "S2",
                                "speaker": "S2-Speaker 1",
                                "raw_start_ms": 1000,
                                "raw_end_ms": 6000,
                                "start_ms": 601000,
                                "end_ms": 606000,
                                "text": "这是第二分段管理层关于 2026 年资本开支指引的详细回答。",
                            },
                        ],
                    },
                )

            return _make_response(req, 404, {"ok": False, "detail": "Not found"})

        if method == "PUT":
            raw_put = req.data or b""
            parsed_put = urllib.parse.urlsplit(req.full_url)
            qs = urllib.parse.parse_qs(parsed_put.query)
            pnum = int((qs.get("partNumber") or ["1"])[0])
            etag = f'"etag-part-{pnum}-{len(raw_put)}"'
            state["uploaded_parts"][pnum] = {
                "part_number": pnum,
                "etag": etag,
                "size": len(raw_put),
                "data": raw_put,
            }
            state["requests_log"].append(("PUT", parsed_put.path, {"part_number": pnum, "size": len(raw_put)}))
            resp = _make_response(req, 200, {"ok": True})
            resp.headers["ETag"] = etag
            return resp

        return _make_response(req, 405, {"ok": False, "detail": "Method not allowed"})

    class _InProcessBackendHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req: urllib.request.Request) -> urllib.response.addinfourl:
            return _dispatch_request(req)

    orig_build_opener = urllib.request.build_opener

    def _patched_build_opener(*handlers: Any) -> urllib.request.OpenerDirector:
        return orig_build_opener(_InProcessBackendHTTPHandler(), *handlers)

    monkeypatch.setattr(urllib.request, "build_opener", _patched_build_opener)
    state["urlopen"] = _patched_build_opener().open
    return state


def test_cli_run_full_flow_local_fake_http_separate_files_and_no_transcript_in_stdout(
    tmp_path: Path,
    local_fake_backend_server: dict[str, Any],
) -> None:
    """`dalton_asr.py run` submits awaiting_upload job, polls stages without fake percentages, writes separate raw JSON & merged transcript files, and never leaks transcript text or ticket in stdout."""
    jid = local_fake_backend_server["job_id"]
    secret_ticket = local_fake_backend_server["ticket"]
    base_url = local_fake_backend_server["base_url"]

    tfile = _write_0600_ticket_file(
        tmp_path,
        job_id=jid,
        ticket=secret_ticket,
        mode=0o600,
    )
    out_dir = tmp_path / "output_run"
    stdout_buf = io.StringIO()

    rc = dalton_asr.main(
        [
            "run",
            "--ticket-file",
            str(tfile),
            "--output-dir",
            str(out_dir),
            "--language",
            "zh-CN",
            "--hotwords",
            "宁德时代,资本开支",
            "--poll-interval",
            "0.0",
        ],
        environ={"DALTON_API_URL": base_url},
        sleep_fn=lambda _s: None,
        stream=stdout_buf,
    )
    assert rc == 0

    stdout_raw = stdout_buf.getvalue()
    # 1. Ticket credential and transcript body MUST NEVER appear in stdout
    assert secret_ticket not in stdout_raw
    assert "fullflow_secret_signature_777777" not in stdout_raw
    assert "这是高度机密的专家访谈逐字稿正文第一段" not in stdout_raw
    assert "这是第二分段管理层关于 2026 年资本开支指引的详细回答" not in stdout_raw

    # 2. Parse stdout lines: verify progress stages and no fabricated percentage
    events = [json.loads(line) for line in stdout_raw.strip().splitlines() if line.strip()]
    progress_events = [e for e in events if e.get("event") in ("submitted", "progress")]
    assert len(progress_events) >= 3
    observed_stages = [e["stage"] for e in progress_events]
    assert "queued" in observed_stages
    assert "downloading" in observed_stages
    assert "transcribing" in observed_stages
    for pe in progress_events:
        assert "percent" not in pe
        assert "progress_percent" not in pe
        assert "%" not in json.dumps(pe)
        assert "segment_count" in pe
        assert "completed_segments" in pe
        assert "updated_at" in pe

    # 3. Verify final result_saved event and separate output files
    final_ev = events[-1]
    assert final_ev["ok"] is True
    assert final_ev["event"] == "result_saved"
    assert final_ev["job_id"] == jid
    assert final_ev["status"] == "succeeded"

    raw_json_path = Path(final_ev["raw_json_path"])
    transcript_path = Path(final_ev["transcript_path"])
    meta_path = Path(final_ev["meta_path"])
    assert raw_json_path.is_file()
    assert transcript_path.is_file()
    assert meta_path.is_file()
    assert raw_json_path != transcript_path

    # Raw JSON preserves utterance raw_start_ms / start_ms and segment metadata
    raw_doc = json.loads(raw_json_path.read_text(encoding="utf-8"))
    assert raw_doc["job_id"] == jid
    assert len(raw_doc["segments"]) == 2
    assert raw_doc["utterances"][1]["speaker"] == "S2-Speaker 1"
    assert raw_doc["utterances"][1]["raw_start_ms"] == 1000
    assert raw_doc["utterances"][1]["start_ms"] == 601000

    # Merged transcript text file holds the full verbatim text
    transcript_text = transcript_path.read_text(encoding="utf-8")
    assert "[S1-Speaker 1]" in transcript_text
    assert "这是高度机密的专家访谈逐字稿正文第一段" in transcript_text
    assert "[S2-Speaker 1]" in transcript_text

    # Only the `dalton-transcript-polish` copy exists.
    assert not (REPO_ROOT / "plugins" / "dalton" / "skills" / "transcript-polish").exists()


# ---------------------------------------------------------------------------
# 4. Polling Interrupted (`KeyboardInterrupt` / 1-Hour Stop Rule) Does NOT
#    Cancel Job; Subsequent `status` and `result` Retrieve It
# ---------------------------------------------------------------------------


def test_cli_polling_interrupted_preserves_job_and_status_result_retrieve_later(
    tmp_path: Path,
    local_fake_backend_server: dict[str, Any],
) -> None:
    """Stopping the polling command (e.g. 1-hour background command stop) does NOT cancel the job; `status` and `result` retrieve it afterwards."""
    jid = local_fake_backend_server["job_id"]
    secret_ticket = local_fake_backend_server["ticket"]
    base_url = local_fake_backend_server["base_url"]
    tfile = _write_0600_ticket_file(
        tmp_path, job_id=jid, ticket=secret_ticket, mode=0o600
    )

    # Simulate polling being interrupted by SIGINT / 1-hour stop after the job is submitted and in `transcribing`
    sleep_calls = 0

    def _interrupting_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise KeyboardInterrupt("Simulated 1-hour background command stop")

    out_run = io.StringIO()
    rc_interrupted = dalton_asr.main(
        [
            "run",
            "--ticket-file",
            str(tfile),
            "--output-dir",
            str(tmp_path / "out_interrupted"),
        ],
        environ={"DALTON_API_URL": base_url},
        sleep_fn=_interrupting_sleep,
        stream=out_run,
    )
    assert rc_interrupted == 130
    # Backend job was NOT cancelled!
    assert local_fake_backend_server["cancelled"] is False
    run_lines = [
        json.loads(line) for line in out_run.getvalue().strip().splitlines() if line.strip()
    ]
    last_interrupt_msg = run_lines[-1]
    assert last_interrupt_msg["ok"] is False
    assert last_interrupt_msg["interrupted"] is True
    assert last_interrupt_msg["job_id"] == jid

    # Advance server state to succeeded and verify `status` + `result` retrieve it cleanly
    local_fake_backend_server["poll_idx"] = len(
        local_fake_backend_server["status_sequence"]
    ) - 1

    out_status = io.StringIO()
    rc_status = dalton_asr.main(
        ["status", "--ticket-file", str(tfile)],
        environ={"DALTON_API_URL": base_url},
        stream=out_status,
    )
    assert rc_status == 0
    st_doc = json.loads(out_status.getvalue().strip())
    assert st_doc["ok"] is True
    assert st_doc["job_id"] == jid
    assert st_doc["status"] == "succeeded"

    out_res_dir = tmp_path / "out_recovered"
    out_result = io.StringIO()
    rc_result = dalton_asr.main(
        [
            "result",
            "--ticket-file",
            str(tfile),
            "--output-dir",
            str(out_res_dir),
        ],
        environ={"DALTON_API_URL": base_url},
        stream=out_result,
    )
    assert rc_result == 0
    res_doc = json.loads(out_result.getvalue().strip())
    assert res_doc["ok"] is True
    assert Path(res_doc["raw_json_path"]).is_file()
    assert Path(res_doc["transcript_path"]).is_file()
    assert "这是高度机密的专家访谈逐字稿正文第一段" not in out_result.getvalue()


# ---------------------------------------------------------------------------
# 5. `cancel` Semantics: Stops Unsubmitted Segments & Reports Unverified
#    Provider Cancel / Billing for Already-Submitted Segments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_semantics_bridge_backend_and_cli(
    tmp_path: Path,
    local_fake_backend_server: dict[str, Any],
) -> None:
    """Explicit `cancel` marks unsubmitted segments `cancelled` and honestly outputs that already-submitted Volcengine segments have unverified cancellation/billing."""
    # 1. Verify CLI `cancel` output against local fake HTTP server
    jid = local_fake_backend_server["job_id"]
    secret_ticket = local_fake_backend_server["ticket"]
    base_url = local_fake_backend_server["base_url"]
    tfile = _write_0600_ticket_file(
        tmp_path, job_id=jid, ticket=secret_ticket, mode=0o600
    )

    out_cancel = io.StringIO()
    rc_cancel = dalton_asr.main(
        ["cancel", "--ticket-file", str(tfile)],
        environ={"DALTON_API_URL": base_url},
        stream=out_cancel,
    )
    assert rc_cancel == 0
    assert local_fake_backend_server["cancelled"] is True
    cancel_doc = json.loads(out_cancel.getvalue().strip())
    assert cancel_doc["ok"] is True
    assert cancel_doc["status"] == "cancelled"
    assert cancel_doc["submitted_segments_count"] == 1
    assert cancel_doc["submitted_request_ids"] == ["req-s2"]
    assert cancel_doc["unsubmitted_cancelled_count"] == 1
    assert cancel_doc["provider_cancel_verified"] is False
    assert cancel_doc["billing_verified"] is False
    assert "未核实" in cancel_doc["notice"]
    assert "已停止后续未提交的分片" in cancel_doc["notice"]

    # 2. Verify bridge `AsrJobManager.cancel_job` on a multi-segment job state
    jobs_dir = tmp_path / "bridge_cancel_jobs"
    mgr = asr_jobs.AsrJobManager(jobs_dir)
    multi_job_id = "asr_multicancel_01"
    job_dir = jobs_dir / multi_job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    initial_state = {
        "job_id": multi_job_id,
        "status": "transcribing",
        "stage": "transcribing",
        "version": 3,
        "attempt": 1,
        "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
        "segment_count": 4,
        "completed_segments": 1,
        "provider_request_ids": ["req-seg-0", "req-seg-1"],
        "segments": [
            {
                "index": 0,
                "start_s": 0.0,
                "end_s": 600.0,
                "status": "succeeded",
                "provider_request_id": "req-seg-0",
            },
            {
                "index": 1,
                "start_s": 600.0,
                "end_s": 1200.0,
                "status": "transcribing",
                "provider_request_id": "req-seg-1",
            },
            {
                "index": 2,
                "start_s": 1200.0,
                "end_s": 1800.0,
                "status": "pending",
                "provider_request_id": None,
            },
            {
                "index": 3,
                "start_s": 1800.0,
                "end_s": 2100.0,
                "status": "pending",
                "provider_request_id": None,
            },
        ],
        "usage": {"user_id": "u-1", "workspace_id": "ws-1"},
    }
    (job_dir / "state.json").write_text(
        json.dumps(initial_state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    code_b, resp_b = await mgr.cancel_job(multi_job_id)
    assert code_b == 200
    c_info = resp_b["cancellation"]
    assert c_info["completed_segments_count"] == 1
    assert c_info["submitted_segments_count"] == 1
    assert c_info["submitted_request_ids"] == ["req-seg-1"]
    assert c_info["unsubmitted_cancelled_count"] == 2
    assert c_info["provider_cancel_verified"] is False
    segs_after = resp_b["job"]["segments"]
    assert segs_after[0]["status"] == "succeeded"
    assert segs_after[1]["provider_request_id"] == "req-seg-1"
    assert segs_after[1]["provider_cancel_verified"] is False
    assert segs_after[2]["status"] == "cancelled"
    assert segs_after[3]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 6. Smooth Polling Backoff Across Network Errors & Bridge Restart
# ---------------------------------------------------------------------------


def test_cli_smooth_backoff_on_network_errors_and_bridge_restart(
    tmp_path: Path,
    local_fake_backend_server: dict[str, Any],
) -> None:
    """Transient network errors (`URLError` / `TimeoutError`), HTTP 502/503, and `bridge_sync_status='unreachable'` during bridge restart back off smoothly and do NOT fail the job."""
    jid = local_fake_backend_server["job_id"]
    secret_ticket = local_fake_backend_server["ticket"]
    base_url = local_fake_backend_server["base_url"]
    tfile = _write_0600_ticket_file(
        tmp_path, job_id=jid, ticket=secret_ticket, mode=0o600
    )

    # Inject HTTP 502, HTTP 503, and a `bridge_sync_status: "unreachable"` snapshot during polling
    local_fake_backend_server["transient_failures_before_poll"] = [
        502,
        503,
        {
            "ok": True,
            "job": {
                "job_id": jid,
                "status": "transcribing",
                "stage": "transcribing",
                "segment_count": 2,
                "completed_segments": 1,
                "updated_at": "2026-10-29T01:00:04Z",
            },
            "bridge_sync_status": "unreachable",
        },
    ]

    # Also wrap urlopen so the very first poll attempt after submit raises a transient URLError (connection reset during restart)
    real_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    call_counter = 0

    def _flaky_urlopen(req: urllib.request.Request, timeout: float = 30.0) -> Any:
        nonlocal call_counter
        call_counter += 1
        if call_counter == 3:
            raise urllib.error.URLError("Connection reset by peer during bridge restart")
        return real_opener.open(req, timeout=timeout)

    recorded_sleeps: list[float] = []
    out_buf = io.StringIO()
    rc = dalton_asr.main(
        [
            "run",
            "--ticket-file",
            str(tfile),
            "--output-dir",
            str(tmp_path / "out_backoff"),
            "--poll-interval",
            "0.25",
        ],
        environ={"DALTON_API_URL": base_url},
        urlopen_fn=_flaky_urlopen,
        sleep_fn=lambda s: recorded_sleeps.append(s),
        initial_backoff_s=0.5,
        max_backoff_s=4.0,
        stream=out_buf,
    )
    assert rc == 0
    events = [
        json.loads(line) for line in out_buf.getvalue().strip().splitlines() if line.strip()
    ]
    retry_events = [e for e in events if e.get("event") == "poll_retry"]
    reconn_events = [e for e in events if e.get("event") == "bridge_reconnecting"]
    assert len(retry_events) >= 3
    assert len(reconn_events) == 1
    assert any(s >= 0.5 for s in recorded_sleeps)
    assert events[-1]["ok"] is True
    assert events[-1]["status"] == "succeeded"


# ---------------------------------------------------------------------------
# 7. Background Long Command Contract & Skill Documentation
# ---------------------------------------------------------------------------


def test_skill_docs_require_main_agent_and_subagent_refuses_run_in_background() -> None:
    """Skill docs explicitly state transcription must be initiated by the main agent via `Bash(run_in_background=True)` because subagents do not support background commands."""
    from ptc_agent.agent.tools.bash import _SUBAGENT_REFUSED_ARGS

    assert "run_in_background" in _SUBAGENT_REFUSED_ARGS

    for skill_md_path in (
        REPO_ROOT
        / "plugins"
        / "dalton"
        / "skills"
        / "dalton-transcript-polish"
        / "SKILL.md",
    ):
        assert skill_md_path.is_file()
        content = skill_md_path.read_text(encoding="utf-8")
        assert "转写必须由主代理发起" in content
        assert "run_in_background=True" in content
        assert "prepare_transcription" in content
        assert "dalton_asr.py" in content


# ---------------------------------------------------------------------------
# 8. Usage Accounting & Bridge/Backend Reconciliation:
#    - Colleague Workspace -> Billed to Real Initiator (`created_by_user_id`)
#    - Same `(job_id, attempt)` Never Double-Counted
#    - Failed Attempt Also Recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_accounting_colleague_workspace_failed_and_retried_attempts_and_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In colleague's workspace, both failed attempt 1 and succeeded attempt 2 are billed to the real initiator, never duplicated across polls, and reconcile cleanly by `(job_id, attempt)`."""
    ledger = InMemoryTranscriptionLedger()
    # Start bridge in failure mode for attempt 1
    bridge = _make_test_bridge(
        tmp_path,
        delay_s=0.01,
        fail_category="asr_upstream_timeout",
    )
    _wire_ledger_and_bridge(monkeypatch, ledger, bridge)

    async def _list_for_usage(
        *,
        user_id: str | None = None,
        workspace_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 500,
        offset: int = 0,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        rows = list(ledger.rows_by_id.values())
        if user_id:
            rows = [r for r in rows if r.get("created_by_user_id") == user_id]
        if workspace_id:
            rows = [r for r in rows if r.get("workspace_id") == workspace_id]
        return rows[offset : offset + limit]

    monkeypatch.setattr(
        db_transcriptions,
        "list_transcription_jobs_for_usage",
        create_autospec(
            db_transcriptions.list_transcription_jobs_for_usage,
            side_effect=_list_for_usage,
        ),
    )
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

    # Workspace `ws-colleague-owner-bob` is owned by `u-bob`, but internal member `u-initiator-alice` initiates the job
    job, ticket, _exp = await svc_transcriptions.prepare_transcription_job(
        user_id="u-initiator-alice",
        workspace_id="ws-colleague-owner-bob",
        thread_id="th-shared-99",
        source_filename="roadshow.m4a",
        source_size_bytes=512000,
        source_sha256="9" * 64,
        language="zh-CN",
        hotwords=["台积电", "CoWoS"],
    )
    job_id = str(job["job_id"])

    app = FastAPI()
    app.include_router(transcriptions_router)
    app.dependency_overrides[get_current_user_id] = lambda: "u-initiator-alice"
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Submit Attempt 1 with spoofed sandbox headers (`X-Dalton-User: u-bob`)
        r_sub = await client.post(
            "/api/v1/dalton/transcriptions",
            headers={
                "X-Dalton-Transcription-Ticket": ticket,
                "X-Dalton-User": "u-bob",
                "X-Dalton-Workspace": "ws-forged",
            },
            json={
                "submit": True,
                "job_id": job_id,
                "source_object_key": job["source_object_key"],
                "source_size_bytes": 512000,
                "source_sha256": "9" * 64,
            },
        )
        assert r_sub.status_code == 200

        # Wait for Attempt 1 to fail
        for _ in range(50):
            r_st1 = await client.get(
                f"/api/v1/dalton/transcriptions/{job_id}",
                headers={
                    "X-Dalton-Transcription-Ticket": ticket,
                    "X-Dalton-User": "u-bob",
                },
            )
            assert r_st1.status_code == 200
            if r_st1.json()["job"]["status"] == "failed":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Attempt 1 did not reach failed status")

        # Poll status 4 more times on the failed attempt to verify idempotency (no duplicate attempt 1 record)
        for _ in range(4):
            r_dup = await client.get(
                f"/api/v1/dalton/transcriptions/{job_id}",
                headers={"X-Dalton-Transcription-Ticket": ticket},
            )
            assert r_dup.status_code == 200

        failed_job = ledger.rows_by_id[job_id]
        attempts_after_a1 = failed_job["usage"]["attempts"]
        assert len(attempts_after_a1) == 1
        a1_rec = attempts_after_a1[0]
        assert a1_rec["job_id"] == job_id
        assert a1_rec["attempt"] == 1
        assert a1_rec["user_id"] == "u-initiator-alice"
        assert a1_rec["workspace_id"] == "ws-colleague-owner-bob"
        assert a1_rec["status"] == "failed"
        assert a1_rec["ok"] is False
        assert a1_rec["error_category"] == "asr_upstream_timeout"
        assert a1_rec["duration_seconds"] == 60.0
        assert a1_rec["segment_count"] == 1

        # Bridge JSONL ledger also has exactly 1 record for (job_id, attempt=1)
        mgr = bridge._asr_job_manager()
        bridge_jsonl_a1 = mgr.read_usage_ledger()
        assert len(bridge_jsonl_a1) == 1
        assert bridge_jsonl_a1[0]["job_id"] == job_id
        assert bridge_jsonl_a1[0]["attempt"] == 1
        assert bridge_jsonl_a1[0]["user_id"] == "u-initiator-alice"
        assert bridge_jsonl_a1[0]["ok"] is False
        assert bridge_jsonl_a1[0]["error_category"] == "asr_upstream_timeout"

        # Now reconfigure worker to succeed and trigger Retry (Attempt 2)
        mgr.worker.config.fail_category = None
        mgr.worker.config.fixed_text = "重试成功的转写正文。"

        r_retry = await client.post(
            f"/api/v1/dalton/transcriptions/{job_id}/retry",
            headers={
                "X-Dalton-Transcription-Ticket": ticket,
                "X-Dalton-User": "u-bob",
            },
        )
        assert r_retry.status_code == 200

        for _ in range(50):
            r_st2 = await client.get(
                f"/api/v1/dalton/transcriptions/{job_id}",
                headers={"X-Dalton-Transcription-Ticket": ticket},
            )
            assert r_st2.status_code == 200
            if r_st2.json()["job"]["status"] == "succeeded":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Attempt 2 did not reach succeeded status")

        # Poll status and result repeatedly after Attempt 2 succeeds -> still exactly 2 attempt records
        for _ in range(4):
            await client.get(
                f"/api/v1/dalton/transcriptions/{job_id}",
                headers={"X-Dalton-Transcription-Ticket": ticket},
            )
            await client.get(
                f"/api/v1/dalton/transcriptions/{job_id}/result",
                headers={"X-Dalton-Transcription-Ticket": ticket},
            )

        final_job = ledger.rows_by_id[job_id]
        attempts_all = final_job["usage"]["attempts"]
        assert len(attempts_all) == 2
        assert [a["attempt"] for a in attempts_all] == [1, 2]
        assert attempts_all[0]["ok"] is False
        assert attempts_all[0]["error_category"] == "asr_upstream_timeout"
        assert attempts_all[0]["user_id"] == "u-initiator-alice"
        assert attempts_all[1]["ok"] is True
        assert attempts_all[1]["error_category"] is None
        assert attempts_all[1]["user_id"] == "u-initiator-alice"

        # Even if someone tampered with bridge usage.user_id on a snapshot, backend enforces created_by_user_id
        tampered_snap = dict(mgr.read_state(job_id) or {})
        tampered_snap["version"] = int(tampered_snap.get("version") or 10) + 1
        tampered_snap["usage"] = {
            **dict(tampered_snap.get("usage") or {}),
            "user_id": "u-forged-sandbox-attacker",
        }
        synced_after_tamper = await svc_transcriptions._sync_job_from_bridge_state(
            final_job, tampered_snap
        )
        assert synced_after_tamper["usage"]["user_id"] == "u-initiator-alice"
        assert all(
            a["user_id"] == "u-initiator-alice"
            for a in synced_after_tamper["usage"]["attempts"]
        )

        # Query read-only usage endpoint GET /api/v1/dalton/transcriptions/usage
        r_usage = await client.get("/api/v1/dalton/transcriptions/usage")
        assert r_usage.status_code == 200
        usage_report = r_usage.json()
        assert usage_report["ok"] is True
        assert usage_report["totals"]["attempt_count"] == 2
        assert usage_report["totals"]["succeeded_attempts"] == 1
        assert usage_report["totals"]["failed_attempts"] == 1
        assert usage_report["totals"]["duration_seconds"] == 120.0
        assert usage_report["totals"]["segment_count"] == 2
        assert usage_report["totals"]["pricing_configured"] is False
        assert len(usage_report["by_user"]) == 1
        assert usage_report["by_user"][0]["user_id"] == "u-initiator-alice"
        assert len(usage_report["by_date"]) == 1

        # Task ticket is forbidden from calling aggregate /usage endpoint
        r_usage_ticket = await client.get(
            "/api/v1/dalton/transcriptions/usage",
            headers={"X-Dalton-Transcription-Ticket": ticket},
        )
        assert r_usage_ticket.status_code == 403

    # Verify reconciliation between bridge JSONL (`asr_usage.jsonl`) and backend DB rows by `(job_id, attempt)`
    bridge_records = asr_usage_report.load_bridge_usage_records(
        usage_jsonl_path=mgr.usage_ledger_path
    )
    assert len(bridge_records) == 2

    reconciled_summary = db_transcriptions.summarize_transcription_usage(
        list(ledger.rows_by_id.values()),
        bridge_records=bridge_records,
    )
    recon = reconciled_summary["reconciliation"]
    assert recon["matched_count"] == 2
    assert recon["backend_only_count"] == 0
    assert recon["bridge_only_count"] == 0
    assert recon["mismatch_count"] == 0
    # Crucial: reconciling 2 backend records + 2 matching bridge JSONL records produces 2 deduplicated attempts, NEVER 4!
    assert recon["deduplicated_attempt_count"] == 2
    assert reconciled_summary["totals"]["attempt_count"] == 2
    assert reconciled_summary["totals"]["duration_seconds"] == 120.0

    # Verify `asr_usage_report.py` CLI output
    backend_dump_path = tmp_path / "backend_jobs.json"
    backend_dump_path.write_text(
        json.dumps(list(ledger.rows_by_id.values()), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report_text = asr_usage_report.format_usage_report_text(reconciled_summary)
    assert "u-initiator-alice" in report_text
    assert "matched=2" in report_text
    assert "单价未配置" in report_text


# ---------------------------------------------------------------------------
# A1d fix1 — Item 1 (Backend): Upload Ticket Endpoints, Key Binding,
#             Status Check, Size Mismatch Abort & Pre-Submit Verification
# ---------------------------------------------------------------------------


class FakeMultipartStorage:
    """In-memory fake S3 multipart storage backing `s3_compatible` helpers."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._seq = 0
        self.uploads: dict[tuple[str, str], dict[int, dict[str, Any]]] = {}
        self.completed_objects: dict[str, bytes] = {}
        self.aborted_uploads: list[tuple[str, str]] = []
        self.deleted_keys: list[str] = []
        self.signed_part_requests: list[tuple[str, str, int, int, int]] = []
        self.override_object_size: dict[str, int | None] = {}

    def is_enabled(self) -> bool:
        return self.enabled

    def open_multipart_upload(
        self,
        key: str,
        *,
        content_type: str = "application/octet-stream",
    ) -> str | None:
        if not self.enabled:
            return None
        self._seq += 1
        upload_id = f"mp-{self._seq:03d}"
        self.uploads[(key, upload_id)] = {}
        return upload_id

    def sign_upload_part(
        self,
        key: str,
        upload_id: str,
        *,
        part_number: int,
        size: int,
        expires_in: int = 900,
        for_browser: bool = False,
    ) -> str | None:
        if not self.enabled or (key, upload_id) not in self.uploads:
            return None
        self.signed_part_requests.append(
            (key, upload_id, int(part_number), int(size), int(expires_in))
        )
        return (
            f"http://127.0.0.1:19000/s3-upload"
            f"?key={urllib.parse.quote(key, safe='')}"
            f"&uploadId={upload_id}&partNumber={part_number}&attempt={len(self.signed_part_requests)}"
        )

    def list_uploaded_parts(
        self,
        key: str,
        upload_id: str,
    ) -> list[dict[str, Any]] | None:
        parts = self.uploads.get((key, upload_id))
        if parts is None:
            return None
        return [
            {
                "part_number": pnum,
                "etag": info["etag"],
                "size": len(info["data"]),
            }
            for pnum, info in sorted(parts.items())
        ]

    def complete_multipart_upload(
        self,
        key: str,
        upload_id: str,
        parts: list[tuple[int, str]],
    ) -> bool:
        bucket = self.uploads.get((key, upload_id))
        if bucket is None:
            return False
        assembled = bytearray()
        for pnum, _etag in sorted(parts, key=lambda x: x[0]):
            if pnum not in bucket:
                return False
            assembled.extend(bucket[pnum]["data"])
        self.completed_objects[key] = bytes(assembled)
        self.uploads.pop((key, upload_id), None)
        return True

    def abort_multipart_upload(self, key: str, upload_id: str) -> bool:
        self.aborted_uploads.append((key, upload_id))
        self.uploads.pop((key, upload_id), None)
        return True

    def object_size(self, key: str) -> int | None:
        if key in self.override_object_size:
            return self.override_object_size[key]
        blob = self.completed_objects.get(key)
        return len(blob) if blob is not None else None

    def delete_object(self, key: str) -> bool:
        self.deleted_keys.append(key)
        self.completed_objects.pop(key, None)
        return True


def _wire_fake_multipart_storage(
    monkeypatch: pytest.MonkeyPatch,
    storage: FakeMultipartStorage,
) -> None:
    from src.utils import storage as storage_mod
    from src.utils.storage import s3_compatible as s3_mod

    monkeypatch.setattr(
        svc_transcriptions,
        "is_storage_enabled",
        create_autospec(storage_mod.is_storage_enabled, side_effect=storage.is_enabled),
    )
    monkeypatch.setattr(
        svc_transcriptions,
        "open_multipart_upload",
        create_autospec(
            s3_mod.open_multipart_upload,
            side_effect=storage.open_multipart_upload,
        ),
    )
    monkeypatch.setattr(
        svc_transcriptions,
        "sign_upload_part",
        create_autospec(
            s3_mod.sign_upload_part,
            side_effect=storage.sign_upload_part,
        ),
    )
    monkeypatch.setattr(
        svc_transcriptions,
        "list_uploaded_parts",
        create_autospec(
            s3_mod.list_uploaded_parts,
            side_effect=storage.list_uploaded_parts,
        ),
    )
    monkeypatch.setattr(
        svc_transcriptions,
        "complete_multipart_upload",
        create_autospec(
            s3_mod.complete_multipart_upload,
            side_effect=storage.complete_multipart_upload,
        ),
    )
    monkeypatch.setattr(
        svc_transcriptions,
        "abort_multipart_upload",
        create_autospec(
            s3_mod.abort_multipart_upload,
            side_effect=storage.abort_multipart_upload,
        ),
    )
    monkeypatch.setattr(
        svc_transcriptions,
        "object_size",
        create_autospec(s3_mod.object_size, side_effect=storage.object_size),
    )
    monkeypatch.setattr(
        svc_transcriptions,
        "delete_object",
        create_autospec(s3_mod.delete_object, side_effect=storage.delete_object),
    )


@pytest.mark.asyncio
async def test_fix1_upload_endpoints_ticket_and_status_and_smuggled_key_and_size_mismatch_and_pre_submit_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 1 (Backend):
    - Ticket without `upload` op is rejected (`403`).
    - Non-`awaiting_upload` status is rejected (`409`).
    - Smuggled `key` / `source_object_key` / `url` in request body is rejected (`400`); object key is strictly the ticket-bound `source_object_key`.
    - Submitting before upload completion (`object_size != source_size_bytes`) is rejected (`409`).
    - Size mismatch on complete aborts the multipart upload (`abort_multipart_upload` + `delete_object`) and returns `400`.
    - Object storage disabled explicitly: the service-level upload guard errors `503`; via the router the A1 master switch answers `404` (feature absent).
    """
    ledger = InMemoryTranscriptionLedger()
    bridge = _make_test_bridge(tmp_path, delay_s=0.01)
    _wire_ledger_and_bridge(monkeypatch, ledger, bridge)
    storage = FakeMultipartStorage(enabled=True)
    _wire_fake_multipart_storage(monkeypatch, storage)

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

    audio_payload = b"AUDIO_BYTES_" * 500
    audio_sha = hashlib.sha256(audio_payload).hexdigest()
    job, full_ticket, _exp = await svc_transcriptions.prepare_transcription_job(
        user_id="u-internal-upload",
        workspace_id="ws-upload-1",
        thread_id="th-upload-1",
        source_filename="interview.m4a",
        source_size_bytes=len(audio_payload),
        source_sha256=audio_sha,
    )
    job_id = str(job["job_id"])
    bound_key = str(job["source_object_key"])

    # Verify issued ticket includes `upload` operation
    verified_claims = svc_transcriptions.verify_transcription_ticket(
        full_ticket,
        required_op="upload",
        expected_job_id=job_id,
    )
    assert "upload" in verified_claims["ops"]

    # Mint a ticket WITHOUT `upload` op
    no_upload_ticket, _ = svc_transcriptions.issue_transcription_ticket(
        job_id=job_id,
        user_id="u-internal-upload",
        workspace_id="ws-upload-1",
        thread_id="th-upload-1",
        source_object_key=bound_key,
        source_size_bytes=len(audio_payload),
        source_sha256=audio_sha,
        allowed_operations=("submit", "status", "result", "retry", "cancel"),
    )

    app = FastAPI()
    app.include_router(transcriptions_router)
    app.dependency_overrides[get_current_user_id] = lambda: "u-internal-upload"
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Ticket without `upload` op is rejected with 403
        r_no_op = await client.post(
            f"/api/v1/dalton/transcriptions/{job_id}/upload/start",
            headers={"X-Dalton-Transcription-Ticket": no_upload_ticket},
            json={},
        )
        assert r_no_op.status_code == 403
        assert "upload" in r_no_op.json()["detail"]

        # 2. Pre-submit check: submitting before object is uploaded returns 409
        r_premature_submit = await client.post(
            "/api/v1/dalton/transcriptions",
            headers={"X-Dalton-Transcription-Ticket": full_ticket},
            json={"submit": True, "job_id": job_id},
        )
        assert r_premature_submit.status_code == 409
        assert "incomplete" in r_premature_submit.json()["detail"]

        # 3. Smuggled key/URL in request body is rejected with 400
        for smuggled_body in (
            {"source_object_key": "transcriptions/other-ws/evil.m4a"},
            {"key": "transcriptions/other-ws/evil.m4a"},
            {"object_key": "transcriptions/other-ws/evil.m4a"},
            {"url": "http://evil.example.com/put"},
            {"bucket": "other-bucket"},
        ):
            r_smug = await client.post(
                f"/api/v1/dalton/transcriptions/{job_id}/upload/start",
                headers={"X-Dalton-Transcription-Ticket": full_ticket},
                json=smuggled_body,
            )
            assert r_smug.status_code == 400

        # 4. Object storage disabled explicitly: the service-level upload guard
        #    still refuses with 503 (never downgrades); through the router the
        #    A1 master switch (storage off => feature absent) answers 404 first.
        storage.enabled = False
        with pytest.raises(svc_transcriptions.HTTPException) as svc_disabled:
            await svc_transcriptions.start_transcription_upload(
                job_id=job_id,
                caller_user_id="u-internal-upload",
                ticket_claims=svc_transcriptions.verify_transcription_ticket(
                    full_ticket, required_op="upload", expected_job_id=job_id
                ),
                raw_body={},
            )
        assert svc_disabled.value.status_code == 503
        r_disabled = await client.post(
            f"/api/v1/dalton/transcriptions/{job_id}/upload/start",
            headers={"X-Dalton-Transcription-Ticket": full_ticket},
            json={},
        )
        assert r_disabled.status_code == 404
        storage.enabled = True

        # 5. Start multipart upload -> binds strictly to `bound_key`
        r_start = await client.post(
            f"/api/v1/dalton/transcriptions/{job_id}/upload/start",
            headers={"X-Dalton-Transcription-Ticket": full_ticket},
            json={},
        )
        assert r_start.status_code == 200
        start_data = r_start.json()
        upload_id = start_data["upload_id"]
        assert start_data["part_size"] == 16 * 1024 * 1024
        assert start_data["part_count"] == 1
        assert (bound_key, upload_id) in storage.uploads

        # 6. Sign part 1 URL (15-minute TTL = 900s)
        r_part = await client.post(
            f"/api/v1/dalton/transcriptions/{job_id}/upload/part",
            headers={"X-Dalton-Transcription-Ticket": full_ticket},
            json={"part_number": 1, "upload_id": upload_id},
        )
        assert r_part.status_code == 200
        part_data = r_part.json()
        assert part_data["expires_in"] == 900
        assert storage.signed_part_requests[-1] == (
            bound_key,
            upload_id,
            1,
            len(audio_payload),
            900,
        )

        # 7. Upload wrong size bytes and try to complete -> aborts multipart upload and returns 400
        storage.uploads[(bound_key, upload_id)][1] = {
            "etag": '"etag-bad-size"',
            "data": b"TOO_SHORT",
        }
        r_comp_bad = await client.post(
            f"/api/v1/dalton/transcriptions/{job_id}/upload/complete",
            headers={"X-Dalton-Transcription-Ticket": full_ticket},
            json={"upload_id": upload_id},
        )
        assert r_comp_bad.status_code == 400
        assert (bound_key, upload_id) in storage.aborted_uploads

        # 8. Re-start, upload exact size, and complete -> succeeds, then submit succeeds
        r_start2 = await client.post(
            f"/api/v1/dalton/transcriptions/{job_id}/upload/start",
            headers={"X-Dalton-Transcription-Ticket": full_ticket},
            json={},
        )
        assert r_start2.status_code == 200
        upload_id2 = r_start2.json()["upload_id"]
        storage.uploads[(bound_key, upload_id2)][1] = {
            "etag": '"etag-good-size"',
            "data": audio_payload,
        }
        r_comp_ok = await client.post(
            f"/api/v1/dalton/transcriptions/{job_id}/upload/complete",
            headers={"X-Dalton-Transcription-Ticket": full_ticket},
            json={"upload_id": upload_id2},
        )
        assert r_comp_ok.status_code == 200
        assert r_comp_ok.json()["object_size"] == len(audio_payload)

        # Submit now succeeds because `object_size == source_size_bytes`
        r_sub_ok = await client.post(
            "/api/v1/dalton/transcriptions",
            headers={"X-Dalton-Transcription-Ticket": full_ticket},
            json={"submit": True, "job_id": job_id},
        )
        assert r_sub_ok.status_code == 200

        # 9. Non-`awaiting_upload` status is rejected on upload endpoints with 409
        for ep in ("start", "part", "complete", "abort"):
            r_not_awaiting = await client.post(
                f"/api/v1/dalton/transcriptions/{job_id}/upload/{ep}",
                headers={"X-Dalton-Transcription-Ticket": full_ticket},
                json={"part_number": 1} if ep == "part" else {},
            )
            assert r_not_awaiting.status_code == 409


# ---------------------------------------------------------------------------
# A1d fix1 — Item 1 (CLI): Path Validation, SHA Mismatch Abort, Resume Only
#             Missing Parts, Part Retry + Re-Sign, and Streaming Memory Bound
# ---------------------------------------------------------------------------


def test_fix1_cli_multipart_upload_sha_mismatch_resume_part_retry_and_streaming_memory(
    tmp_path: Path,
    local_fake_backend_server: dict[str, Any],
) -> None:
    """Fix 1 (CLI):
    - Rejects absolute `source_path` and `..` traversal in ticket.
    - Reads in fixed chunks (never reading whole file into memory at once), computing SHA-256 incrementally.
    - When SHA-256 mismatches ticket, aborts upload without calling `/upload/complete`.
    - Retries failed part PUT up to 3 times with backoff and re-signs the part URL each time.
    - On resume after interruption, uses `uploaded_parts` from `/upload/start` to upload only missing parts and prints `"已上传 x / y 片"`.
    """
    jid = local_fake_backend_server["job_id"]
    secret_ticket = local_fake_backend_server["ticket"]
    base_url = local_fake_backend_server["base_url"]

    # 1. Reject absolute `source_path` and `..` traversal
    for bad_src in ("/etc/passwd", "../secrets.txt", "audio/../../outside.wav"):
        local_fake_backend_server["poll_idx"] = 0
        bad_tfile = _write_0600_ticket_file(
            tmp_path,
            job_id=jid,
            ticket=secret_ticket,
            extra={"source_path": bad_src},
        )
        out_bad = io.StringIO()
        rc_bad = dalton_asr.main(
            [
                "run",
                "--ticket-file",
                str(bad_tfile),
                "--output-dir",
                str(tmp_path / "out_bad_src"),
            ],
            environ={"DALTON_API_URL": base_url},
            workspace_root=tmp_path,
            stream=out_bad,
        )
        assert rc_bad == 2
        err_doc = json.loads(out_bad.getvalue().strip())
        assert err_doc["error_code"] == "invalid_source_path"

    # 2. Multi-part file: use 3 parts (simulate with custom part_size=64 KiB on a 150 KiB file, and verify read chunk sizes <= part_size)
    part_size = 64 * 1024
    part1_bytes = b"A" * part_size
    part2_bytes = b"B" * part_size
    part3_bytes = b"C" * (22 * 1024)
    full_audio = part1_bytes + part2_bytes + part3_bytes
    real_sha256 = hashlib.sha256(full_audio).hexdigest()

    local_fake_backend_server["poll_idx"] = 0
    local_fake_backend_server["part_size"] = part_size
    local_fake_backend_server["source_size_bytes"] = len(full_audio)

    # 2a. SHA-256 mismatch -> does NOT call `/upload/complete`, calls `/upload/abort`, and fails
    bad_sha_tfile = _write_0600_ticket_file(
        tmp_path,
        job_id=jid,
        ticket=secret_ticket,
        audio_bytes=full_audio,
        extra={
            "source_size_bytes": len(full_audio),
            "source_sha256": "f" * 64,
        },
    )
    out_sha = io.StringIO()
    rc_sha = dalton_asr.main(
        [
            "run",
            "--ticket-file",
            str(bad_sha_tfile),
            "--output-dir",
            str(tmp_path / "out_sha_fail"),
        ],
        environ={"DALTON_API_URL": base_url},
        workspace_root=tmp_path,
        sleep_fn=lambda _s: None,
        stream=out_sha,
    )
    assert rc_sha == 1
    assert local_fake_backend_server["upload_completed"] is False
    assert local_fake_backend_server["upload_aborted"] is True
    assert local_fake_backend_server["submitted_bodies"] == []
    sha_lines = [json.loads(line) for line in out_sha.getvalue().strip().splitlines() if line.strip()]
    assert sha_lines[-1]["error_code"] == "source_sha256_mismatch"

    # 3. Resume + part retry + streaming chunk counter:
    # Pre-populate part 1 in `uploaded_parts` as already uploaded before interruption.
    local_fake_backend_server["upload_aborted"] = False
    local_fake_backend_server["upload_completed"] = False
    local_fake_backend_server["poll_idx"] = 0
    local_fake_backend_server["requests_log"].clear()
    local_fake_backend_server["uploaded_parts"] = {
        1: {
            "part_number": 1,
            "etag": f'"etag-part-1-{len(part1_bytes)}"',
            "size": len(part1_bytes),
            "data": part1_bytes,
        }
    }

    good_tfile = _write_0600_ticket_file(
        tmp_path,
        job_id=jid,
        ticket=secret_ticket,
        audio_bytes=full_audio,
        extra={
            "source_size_bytes": len(full_audio),
            "source_sha256": real_sha256,
        },
    )

    # Track every `read(size)` call to prove streaming chunked reads (never reading whole file at once)
    read_sizes: list[int] = []

    class _ChunkTrackingReader:
        def __init__(self, path_obj: Path) -> None:
            self._fh = open(path_obj, "rb")

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            assert size == part_size, "Must read in fixed part_size chunks, never unbounded read()"
            return self._fh.read(size)

        def __enter__(self) -> _ChunkTrackingReader:
            return self

        def __exit__(self, *args: Any) -> None:
            self._fh.close()

    # Inject a transient PUT failure on the first attempt of part 2 to verify 3x retry + URL re-signing
    real_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    part2_put_attempts = 0

    def _flaky_put_urlopen(req: urllib.request.Request, timeout: float = 30.0) -> Any:
        nonlocal part2_put_attempts
        if req.get_method().upper() == "PUT" and "partNumber=2" in req.full_url:
            part2_put_attempts += 1
            if part2_put_attempts <= 2:
                raise urllib.error.URLError("Simulated transient S3 PUT connection reset")
        return real_opener.open(req, timeout=timeout)

    out_resume = io.StringIO()
    rc_resume = dalton_asr.main(
        [
            "run",
            "--ticket-file",
            str(good_tfile),
            "--output-dir",
            str(tmp_path / "out_resume_ok"),
            "--poll-interval",
            "0.0",
        ],
        environ={"DALTON_API_URL": base_url},
        workspace_root=tmp_path,
        open_file_fn=_ChunkTrackingReader,
        urlopen_fn=_flaky_put_urlopen,
        sleep_fn=lambda _s: None,
        stream=out_resume,
    )
    assert rc_resume == 0
    assert local_fake_backend_server["upload_completed"] is True

    # Verify streaming reads: every read requested exactly `part_size`, never the whole 150 KiB at once
    assert len(read_sizes) >= 3
    assert all(sz == part_size for sz in read_sizes)

    # Verify resume only PUT missing parts (parts 2 and 3; part 1 was NEVER PUT again!)
    put_logs = [entry for entry in local_fake_backend_server["requests_log"] if entry[0] == "PUT"]
    put_part_numbers = [entry[2]["part_number"] for entry in put_logs]
    assert 1 not in put_part_numbers
    assert put_part_numbers == [2, 3]

    # Verify part 2 was re-signed via `/upload/part` on each retry (3 sign calls for part 2)
    sign_part2_calls = [
        entry
        for entry in local_fake_backend_server["requests_log"]
        if entry[0] == "POST"
        and entry[1].endswith("/upload/part")
        and entry[2].get("part_number") == 2
    ]
    assert len(sign_part2_calls) == 3

    # Verify progress output lines include `"已上传 x / y 片"` for all 3 parts
    resume_events = [
        json.loads(line) for line in out_resume.getvalue().strip().splitlines() if line.strip()
    ]
    upload_ev = [e for e in resume_events if e.get("event") == "upload_progress"]
    assert [e["progress"] for e in upload_ev] == [
        "已上传 1 / 3 片",
        "已上传 2 / 3 片",
        "已上传 3 / 3 片",
    ]
    assert upload_ev[0]["resumed"] is True
    assert upload_ev[1]["resumed"] is False
    assert upload_ev[2]["resumed"] is False


# ---------------------------------------------------------------------------
# A1d fix1 — Item 2: `_internal/.egress_relay.json` Config Path
# ---------------------------------------------------------------------------


def test_fix2_egress_config_uses_internal_egress_relay_json_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 2: `_EGRESS_CONFIG_CANDIDATES` is strictly `('_internal/.egress_relay.json',)` (no `.system/...` candidates) and resolves relative to workspace root."""
    assert dalton_asr._EGRESS_CONFIG_CANDIDATES == ("_internal/.egress_relay.json",)
    for cand in dalton_asr._EGRESS_CONFIG_CANDIDATES:
        assert ".system" not in cand

    monkeypatch.chdir(tmp_path)
    internal_dir = tmp_path / "_internal"
    internal_dir.mkdir(parents=True, exist_ok=True)
    relay_file = internal_dir / ".egress_relay.json"
    relay_file.write_text(
        json.dumps({"relay_base_url": "http://172.18.0.1:18000/"}),
        encoding="utf-8",
    )
    resolved = dalton_asr.resolve_backend_base_url({}, environ={})
    assert resolved == "http://172.18.0.1:18000"


# ---------------------------------------------------------------------------
# A1d fix1 — Item 3: Raw Provider Response Streamed vs Unavailable
#             (`raw_response_available: false`, Never Faked with `null`)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix3_raw_provider_response_streamed_or_marked_unavailable_without_fake_null(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_fake_backend_server: dict[str, Any],
) -> None:
    """Fix 3:
    - Backend `GET /api/v1/dalton/transcriptions/{job_id}/result/raw` streams the bridge's raw provider JSON file behind `result` ticket op.
    - When raw file is unavailable (`404`), CLI does NOT create a fake `raw_response.json` with `null` and sets `raw_response_available: false` in `transcription_meta.json`.
    """
    # 1. Verify CLI behaviour when `/result/raw` returns 404 (unavailable)
    jid = local_fake_backend_server["job_id"]
    secret_ticket = local_fake_backend_server["ticket"]
    base_url = local_fake_backend_server["base_url"]
    local_fake_backend_server["raw_response_available"] = False

    tfile = _write_0600_ticket_file(tmp_path, job_id=jid, ticket=secret_ticket)
    out_unavail_dir = tmp_path / "out_no_raw"
    out_buf = io.StringIO()
    rc = dalton_asr.main(
        [
            "result",
            "--ticket-file",
            str(tfile),
            "--output-dir",
            str(out_unavail_dir),
        ],
        environ={"DALTON_API_URL": base_url},
        stream=out_buf,
    )
    assert rc == 0
    ev = json.loads(out_buf.getvalue().strip())
    assert ev["raw_response_available"] is False
    assert ev["raw_json_path"] is None
    assert not (out_unavail_dir / "raw_response.json").exists()
    meta_doc = json.loads((out_unavail_dir / "transcription_meta.json").read_text(encoding="utf-8"))
    assert meta_doc["raw_response_available"] is False
    assert meta_doc["raw_json_path"] is None

    # 2. Verify end-to-end backend + bridge raw streaming endpoint `GET /{job_id}/result/raw`
    ledger = InMemoryTranscriptionLedger()
    bridge = _make_test_bridge(tmp_path / "bridge_raw", delay_s=0.01)
    _wire_ledger_and_bridge(monkeypatch, ledger, bridge)

    async def _bridge_raw_stream(job_id_arg: str) -> tuple[int, Any]:
        code, body_or_path = await bridge.handle_transcription_raw_result(job_id_arg)
        if code != 200 or not isinstance(body_or_path, Path):
            return code, body_or_path

        async def _iter():
            with open(body_or_path, "rb") as fh:
                while True:
                    chunk = fh.read(4096)
                    if not chunk:
                        break
                    yield chunk

        return 200, _iter()

    monkeypatch.setattr(
        svc_transcriptions,
        "open_bridge_raw_result_stream",
        create_autospec(
            svc_transcriptions.open_bridge_raw_result_stream,
            side_effect=_bridge_raw_stream,
        ),
    )
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

    job, ticket, _exp = await svc_transcriptions.prepare_transcription_job(
        user_id="u-raw-tester",
        workspace_id="ws-raw-1",
        thread_id="th-raw-1",
        source_filename="call.m4a",
        source_size_bytes=128000,
        source_sha256="a" * 64,
    )
    b_job_id = str(job["job_id"])

    app = FastAPI()
    app.include_router(transcriptions_router)
    app.dependency_overrides[get_current_user_id] = lambda: "u-raw-tester"
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r_sub = await client.post(
            "/api/v1/dalton/transcriptions",
            headers={"X-Dalton-Transcription-Ticket": ticket},
            json={"submit": True, "job_id": b_job_id},
        )
        assert r_sub.status_code == 200

        for _ in range(50):
            r_st = await client.get(
                f"/api/v1/dalton/transcriptions/{b_job_id}",
                headers={"X-Dalton-Transcription-Ticket": ticket},
            )
            if r_st.json()["job"]["status"] == "succeeded":
                break
            await asyncio.sleep(0.01)

        r_raw = await client.get(
            f"/api/v1/dalton/transcriptions/{b_job_id}/result/raw",
            headers={"X-Dalton-Transcription-Ticket": ticket},
        )
        assert r_raw.status_code == 200
        raw_json = r_raw.json()
        assert raw_json["job_id"] == b_job_id
        assert raw_json["segment_count"] == 1
        assert isinstance(raw_json["segments"], list)


# ---------------------------------------------------------------------------
# A1d fix1 — Item 4: Atomic Output Writes & Symlink Protection
# ---------------------------------------------------------------------------


def test_fix4_atomic_output_writes_reject_symlink_output_dir_and_replace_symlink_targets(
    tmp_path: Path,
    local_fake_backend_server: dict[str, Any],
) -> None:
    """Fix 4:
    - Rejects `output_dir` if `output_dir` itself is a symbolic link (`output_dir_symlink`).
    - Writes all 3 output artifacts (`raw_response.json`, `transcript.txt`, `transcription_meta.json`) to temp files in `output_dir` and commits via `os.replace`, replacing any symlink entry inside `output_dir` without modifying the symlink target.
    """
    jid = local_fake_backend_server["job_id"]
    secret_ticket = local_fake_backend_server["ticket"]
    base_url = local_fake_backend_server["base_url"]
    tfile = _write_0600_ticket_file(tmp_path, job_id=jid, ticket=secret_ticket)

    # 1. `output_dir` is a symlink -> rejected with exit code 2
    real_dir = tmp_path / "real_target_dir"
    real_dir.mkdir(parents=True, exist_ok=True)
    symlink_out_dir = tmp_path / "symlink_output_dir"
    symlink_out_dir.symlink_to(real_dir)

    out_sym = io.StringIO()
    rc_sym = dalton_asr.main(
        [
            "result",
            "--ticket-file",
            str(tfile),
            "--output-dir",
            str(symlink_out_dir),
        ],
        environ={"DALTON_API_URL": base_url},
        stream=out_sym,
    )
    assert rc_sym == 2
    sym_err = json.loads(out_sym.getvalue().strip())
    assert sym_err["error_code"] == "output_dir_symlink"

    # 2. Pre-existing symlinks inside `output_dir` pointing to victim files outside `output_dir`
    #    must be replaced as directory entries via `os.replace` without touching the victim files!
    victim_dir = tmp_path / "victim_files"
    victim_dir.mkdir(parents=True, exist_ok=True)
    victim_raw = victim_dir / "victim_raw.json"
    victim_txt = victim_dir / "victim_transcript.txt"
    victim_meta = victim_dir / "victim_meta.json"
    victim_raw.write_text("DO_NOT_OVERWRITE_RAW", encoding="utf-8")
    victim_txt.write_text("DO_NOT_OVERWRITE_TXT", encoding="utf-8")
    victim_meta.write_text("DO_NOT_OVERWRITE_META", encoding="utf-8")

    safe_out_dir = tmp_path / "safe_atomic_out"
    safe_out_dir.mkdir(parents=True, exist_ok=True)
    (safe_out_dir / "raw_response.json").symlink_to(victim_raw)
    (safe_out_dir / "transcript.txt").symlink_to(victim_txt)
    (safe_out_dir / "transcription_meta.json").symlink_to(victim_meta)

    out_ok = io.StringIO()
    rc_ok = dalton_asr.main(
        [
            "result",
            "--ticket-file",
            str(tfile),
            "--output-dir",
            str(safe_out_dir),
        ],
        environ={"DALTON_API_URL": base_url},
        stream=out_ok,
    )
    assert rc_ok == 0

    # Victim files must be 100% untouched!
    assert victim_raw.read_text(encoding="utf-8") == "DO_NOT_OVERWRITE_RAW"
    assert victim_txt.read_text(encoding="utf-8") == "DO_NOT_OVERWRITE_TXT"
    assert victim_meta.read_text(encoding="utf-8") == "DO_NOT_OVERWRITE_META"

    # And the entries inside `safe_out_dir` are now regular files (not symlinks)
    for fname in ("raw_response.json", "transcript.txt", "transcription_meta.json"):
        fpath = safe_out_dir / fname
        assert not fpath.is_symlink()
        assert fpath.is_file()


# ---------------------------------------------------------------------------
# A1d fix1 — Item 5: Backend 5-Minute Periodic Reconciliation Loop & Lifespan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix5_periodic_reconciliation_loop_syncs_active_jobs_and_lifespan_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 5:
    - `reconcile_active_transcription_jobs_once` syncs non-terminal jobs (including `indeterminate`, up to 50 per pass), isolates single-job errors, skips rest of pass on `bridge_unreachable`, and persists completion + usage even after CLI polling stops.
    - `run_transcription_reconciliation_loop` runs periodically and stops cleanly when cancelled.
    """
    ledger = InMemoryTranscriptionLedger()
    bridge = _make_test_bridge(tmp_path / "bridge_reconcile", delay_s=0.01)
    _wire_ledger_and_bridge(monkeypatch, ledger, bridge)

    async def _list_non_terminal(
        *, limit: int = 50, after_job_id: str | None = None, conn: Any = None
    ) -> list[dict[str, Any]]:
        cursor_str = str(after_job_id or "").strip()
        active = [
            dict(r)
            for r in ledger.rows_by_id.values()
            if str(r.get("status") or "") not in ("succeeded", "failed", "cancelled")
            and str(r.get("job_id") or "") > cursor_str
        ]
        active.sort(key=lambda r: str(r.get("job_id") or ""))
        return active[:limit]

    monkeypatch.setattr(
        db_transcriptions,
        "list_non_terminal_transcription_jobs",
        create_autospec(
            db_transcriptions.list_non_terminal_transcription_jobs,
            side_effect=_list_non_terminal,
        ),
    )
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
        user_id="u-bg-reconcile",
        workspace_id="ws-bg-1",
        thread_id="th-bg-1",
        source_filename="long_meeting.m4a",
        source_size_bytes=256000,
        source_sha256="b" * 64,
    )
    job_id = str(job["job_id"])
    await svc_transcriptions.submit_transcription_job(
        caller_user_id="u-bg-reconcile",
        job_id=job_id,
    )

    # Wait for bridge worker to finish in background WITHOUT any CLI or HTTP status polling
    mgr = bridge._asr_job_manager()
    for _ in range(50):
        st = mgr.read_state(job_id)
        if st and st.get("status") == "succeeded":
            break
        await asyncio.sleep(0.01)

    # Before reconciliation pass, backend DB row is still in `queued` and has no attempt usage record
    assert ledger.rows_by_id[job_id]["status"] == "queued"
    assert ledger.rows_by_id[job_id]["usage"].get("attempts") is None

    # Run one background reconciliation pass -> job status and usage are synced to DB!
    pass_result = await svc_transcriptions.reconcile_active_transcription_jobs_once(limit=50)
    assert pass_result["scanned"] == 1
    assert pass_result["synced"] == 1
    assert pass_result["skipped_bridge_unreachable"] is False

    synced_row = ledger.rows_by_id[job_id]
    assert synced_row["status"] == "succeeded"
    assert len(synced_row["usage"]["attempts"]) == 1
    assert synced_row["usage"]["attempts"][0]["user_id"] == "u-bg-reconcile"
    assert synced_row["usage"]["attempts"][0]["ok"] is True

    # Test loop startup and clean cancellation on shutdown
    loop_task = asyncio.create_task(
        svc_transcriptions.run_transcription_reconciliation_loop(
            interval_s=0.05,
            batch_limit=50,
        )
    )
    await asyncio.sleep(0.08)
    loop_task.cancel()
    await asyncio.gather(loop_task, return_exceptions=True)
    assert loop_task.done()


# ---------------------------------------------------------------------------
# A1d fix1 — Item 6 & Item 7: Cross-Day Usage Date Filtering (`recorded_at`),
#             `truncated` Flag, `jobs_scanned`, and `offset` Pagination
# ---------------------------------------------------------------------------


def test_fix6_and_fix7_usage_cross_day_recorded_at_and_truncation_pagination() -> None:
    """Fix 6 & Fix 7:
    - Job created at `2026-10-28T23:30:00Z` and completed at `2026-10-29T00:30:00Z` is counted when querying `2026-10-29` (`start_date="2026-10-29", end_date="2026-10-29"`), and NOT on `2026-10-28`.
    - `summarize_transcription_usage` reports `jobs_scanned`, `truncated=True` when `len(jobs) >= limit`, and `offset`.
    """
    cross_day_job = {
        "job_id": "asr_crossday_01",
        "created_by_user_id": "u-night-owl",
        "workspace_id": "ws-night",
        "status": "succeeded",
        "segment_count": 2,
        "completed_segments": 2,
        "created_at": "2026-10-28T23:30:00+00:00",
        "updated_at": "2026-10-29T00:30:00+00:00",
        "completed_at": "2026-10-29T00:30:00+00:00",
        "usage": {
            "user_id": "u-night-owl",
            "attempts": [
                {
                    "job_id": "asr_crossday_01",
                    "attempt": 1,
                    "user_id": "u-night-owl",
                    "workspace_id": "ws-night",
                    "status": "succeeded",
                    "ok": True,
                    "duration_seconds": 900.0,
                    "segment_count": 2,
                    "completed_segments": 2,
                    "error_category": None,
                    "recorded_at": "2026-10-29T00:30:00+00:00",
                }
            ],
        },
    }

    # Querying Day 2 (`2026-10-29`) includes the cross-day attempt
    day2_summary = db_transcriptions.summarize_transcription_usage(
        [cross_day_job],
        start_date="2026-10-29",
        end_date="2026-10-29",
        limit=1,
        offset=0,
    )
    assert day2_summary["totals"]["attempt_count"] == 1
    assert day2_summary["totals"]["duration_seconds"] == 900.0
    assert day2_summary["by_date"][0]["date"] == "2026-10-29"
    assert day2_summary["jobs_scanned"] == 1
    assert day2_summary["truncated"] is True
    assert day2_summary["limit"] == 1
    assert day2_summary["offset"] == 0

    # Querying Day 1 (`2026-10-28`) does not include the attempt completed on Day 2
    day1_summary = db_transcriptions.summarize_transcription_usage(
        [cross_day_job],
        start_date="2026-10-28",
        end_date="2026-10-28",
        limit=500,
        offset=10,
    )
    assert day1_summary["totals"]["attempt_count"] == 0
    assert day1_summary["jobs_scanned"] == 1
    assert day1_summary["truncated"] is False
    assert day1_summary["offset"] == 10


# ---------------------------------------------------------------------------
# A1d fix1 — Item 8: Cancelled Attempt with Submitted Segments Accounting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix8_cancelled_attempt_with_submitted_segments_recorded_and_deduplicated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 8:
    - Explicit `cancel` on a job with already-submitted segments records an attempt with `status="cancelled"`, `ok=False`, `submitted_seconds`, `provider_request_ids`, `billing_verified=False`.
    - Deduplicated by `(job_id, attempt)` across bridge JSONL and backend `usage.attempts`.
    - `cancelled_attempts` is counted separately in usage summaries.
    """
    jobs_dir = tmp_path / "bridge_cancel_acct"
    mgr = asr_jobs.AsrJobManager(jobs_dir)
    job_id = "asr_cancel_acct_01"
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    initial_state = {
        "job_id": job_id,
        "status": "transcribing",
        "stage": "transcribing",
        "version": 2,
        "attempt": 1,
        "caller": {"user_id": "u-canceller", "workspace_id": "ws-cancel-1", "thread_id": "th-1"},
        "segment_count": 3,
        "completed_segments": 1,
        "provider_request_ids": ["req-c1", "req-c2"],
        "segments": [
            {
                "index": 0,
                "start_s": 0.0,
                "end_s": 600.0,
                "status": "succeeded",
                "provider_request_id": "req-c1",
            },
            {
                "index": 1,
                "start_s": 600.0,
                "end_s": 1200.0,
                "status": "transcribing",
                "provider_request_id": "req-c2",
            },
            {
                "index": 2,
                "start_s": 1200.0,
                "end_s": 1800.0,
                "status": "pending",
                "provider_request_id": None,
            },
        ],
        "usage": {"user_id": "u-canceller", "workspace_id": "ws-cancel-1"},
    }
    (job_dir / "state.json").write_text(
        json.dumps(initial_state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    code, resp = await mgr.cancel_job(job_id)
    assert code == 200
    # Calling cancel_job a second time must not duplicate the attempt record
    code2, _resp2 = await mgr.cancel_job(job_id)
    assert code2 == 200

    ledger_entries = mgr.read_usage_ledger()
    assert len(ledger_entries) == 1
    entry = ledger_entries[0]
    assert entry["job_id"] == job_id
    assert entry["attempt"] == 1
    assert entry["status"] == "cancelled"
    assert entry["ok"] is False
    assert entry["submitted_seconds"] == 1200.0
    assert entry["provider_request_ids"] == ["req-c1", "req-c2"]
    assert entry["billing_verified"] is False

    # Sync to backend job row and verify backend + bridge deduplication and separate `cancelled_attempts` counter
    ledger = InMemoryTranscriptionLedger()
    row = await ledger.create_job(
        created_by_user_id="u-canceller",
        workspace_id="ws-cancel-1",
        thread_id="th-1",
        source_object_key=f"transcriptions/ws-cancel-1/{job_id}/source/a.m4a",
        source_size_bytes=128000,
        source_sha256="c" * 64,
        idempotency_key="idemp-cancel-1",
        job_id=job_id,
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
        db_transcriptions,
        "get_transcription_job",
        create_autospec(
            db_transcriptions.get_transcription_job,
            side_effect=ledger.get_job,
        ),
    )

    synced = await svc_transcriptions._sync_job_from_bridge_state(row, resp["job"])
    assert len(synced["usage"]["attempts"]) == 1
    b_att = synced["usage"]["attempts"][0]
    assert b_att["status"] == "cancelled"
    assert b_att["ok"] is False
    assert b_att["submitted_seconds"] == 1200.0
    assert b_att["billing_verified"] is False
    assert synced["usage"]["summary"]["cancelled_attempts"] == 1
    assert synced["usage"]["summary"]["failed_attempts"] == 0
    assert synced["usage"]["summary"]["succeeded_attempts"] == 0

    summary = db_transcriptions.summarize_transcription_usage(
        [synced],
        bridge_records=ledger_entries,
    )
    assert summary["totals"]["attempt_count"] == 1
    assert summary["totals"]["cancelled_attempts"] == 1
    assert summary["totals"]["failed_attempts"] == 0
    assert summary["totals"]["succeeded_attempts"] == 0
    assert summary["reconciliation"]["matched_count"] == 1
    assert summary["reconciliation"]["deduplicated_attempt_count"] == 1


# ---------------------------------------------------------------------------
# A1d fix1 — Item 9: Short Commands (`status` / `result` / `cancel` / `retry`)
#             SIGTERM Emits `interrupted` JSON and Exits Non-Zero Without Traceback
# ---------------------------------------------------------------------------


def test_fix9_short_commands_sigterm_emits_interrupted_json_without_traceback(
    tmp_path: Path,
) -> None:
    """Fix 9: Receiving `SIGTERM` during `status`, `result`, `cancel`, or `retry` outputs structured `interrupted` JSON and exits with non-zero code `130` without printing a traceback."""
    tfile = _write_0600_ticket_file(tmp_path, job_id="asr_sigterm_short_01")

    def _sigterm_urlopen(_req: urllib.request.Request, timeout: float = 30.0) -> Any:
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("Signal handler should have raised _PollingInterrupted")

    for subcmd_argv in (
        ["status", "--ticket-file", str(tfile)],
        ["result", "--ticket-file", str(tfile), "--output-dir", str(tmp_path / "out_sigterm")],
        ["cancel", "--ticket-file", str(tfile)],
        ["retry", "--ticket-file", str(tfile)],
    ):
        out_buf = io.StringIO()
        rc = dalton_asr.main(
            subcmd_argv,
            environ={"DALTON_API_URL": "http://127.0.0.1:18080"},
            urlopen_fn=_sigterm_urlopen,
            stream=out_buf,
        )
        assert rc == 130
        raw_out = out_buf.getvalue().strip()
        assert "Traceback" not in raw_out
        parsed = json.loads(raw_out)
        assert parsed["ok"] is False
        assert parsed["interrupted"] is True
        assert parsed["job_id"] == "asr_sigterm_short_01"
        assert parsed["subcommand"] == subcmd_argv[0]


# ---------------------------------------------------------------------------
# A1d fix2 — Item 1: Upload Version Conflict Retry Re-checks `awaiting_upload`
#             (Never Resurrects Cancelled Job) and Exhaustion Returns 409
# ---------------------------------------------------------------------------


def _wire_upload_service_doubles(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[InMemoryTranscriptionLedger, FakeMultipartStorage]:
    ledger = InMemoryTranscriptionLedger()
    storage = FakeMultipartStorage(enabled=True)
    _wire_fake_multipart_storage(monkeypatch, storage)
    monkeypatch.setattr(
        db_transcriptions,
        "get_transcription_job",
        create_autospec(db_transcriptions.get_transcription_job, side_effect=ledger.get_job),
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
        "get_user_tier",
        create_autospec(svc_transcriptions.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        svc_transcriptions,
        "authorize_job_access",
        create_autospec(svc_transcriptions.authorize_job_access, return_value="internal"),
    )
    return ledger, storage


@pytest.mark.asyncio
async def test_fix2_item1_upload_version_conflict_checks_status_and_exhaustion_returns_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix2 Item 1: On version conflict during `start` / `complete` / `abort`, each retry re-checks that `latest.status == 'awaiting_upload'` (aborting any newly opened multipart upload and returning 409 if cancelled), and 3 failed retries also return 409."""
    ledger, storage = _wire_upload_service_doubles(monkeypatch)
    job_id = "asr_fix2_conflict_cancel_01"
    bound_key = f"transcriptions/ws-1/{job_id}/source/meeting.m4a"
    payload = b"AUDIO_CONFLICT_TEST_BYTES"

    await ledger.create_job(
        created_by_user_id="user-internal-1",
        workspace_id="ws-1",
        thread_id="th-1",
        source_object_key=bound_key,
        source_size_bytes=len(payload),
        source_sha256=hashlib.sha256(payload).hexdigest(),
        idempotency_key="idemp-fix2-item1",
        job_id=job_id,
    )
    ticket_claims = {
        "job_id": job_id,
        "workspace_id": "ws-1",
        "caller_user_id": "user-internal-1",
        "source_object_key": bound_key,
        "source_size_bytes": len(payload),
        "ops": ["upload", "submit", "status", "result", "retry", "cancel"],
    }

    # 1. `start` conflicts with a concurrent cancellation -> aborts newly opened multipart upload and raises 409 without resurrecting job
    async def _conflict_then_cancelled_on_start(
        jid: str, *, expected_version: int, **kwargs: Any
    ) -> tuple[bool, dict[str, Any] | None]:
        row = ledger.rows_by_id[jid]
        row["status"] = "cancelled"
        row["stage"] = "cancelled"
        row["version"] = int(row["version"]) + 1
        return False, dict(row)

    db_transcriptions.update_transcription_job_versioned.side_effect = (
        _conflict_then_cancelled_on_start
    )

    with pytest.raises(svc_transcriptions.HTTPException) as exc_start:
        await svc_transcriptions.start_transcription_upload(
            job_id=job_id,
            caller_user_id="user-internal-1",
            ticket_claims=ticket_claims,
        )
    assert exc_start.value.status_code == 409
    assert ledger.rows_by_id[job_id]["status"] == "cancelled"
    assert (bound_key, "mp-001") in storage.aborted_uploads

    # 2. Reset job to `awaiting_upload` and verify 3 consecutive version conflicts on `start` abort upload and raise 409
    ledger.rows_by_id[job_id]["status"] = "awaiting_upload"
    ledger.rows_by_id[job_id]["stage"] = "awaiting_upload"

    async def _always_conflict_awaiting(
        jid: str, *, expected_version: int, **kwargs: Any
    ) -> tuple[bool, dict[str, Any] | None]:
        row = ledger.rows_by_id[jid]
        row["version"] = int(row["version"]) + 1
        return False, dict(row)

    db_transcriptions.update_transcription_job_versioned.side_effect = _always_conflict_awaiting
    with pytest.raises(svc_transcriptions.HTTPException) as exc_start_exhaust:
        await svc_transcriptions.start_transcription_upload(
            job_id=job_id,
            caller_user_id="user-internal-1",
            ticket_claims=ticket_claims,
        )
    assert exc_start_exhaust.value.status_code == 409
    assert (bound_key, "mp-002") in storage.aborted_uploads

    # 3. `complete` conflicts with concurrent cancellation -> raises 409 without resurrecting
    #    cancelled status and without deleting the synthesized object (A1d fix3: cleanup
    #    belongs to the terminal state, never to the conflicting complete)
    db_transcriptions.update_transcription_job_versioned.side_effect = ledger.update_versioned
    started = await svc_transcriptions.start_transcription_upload(
        job_id=job_id,
        caller_user_id="user-internal-1",
        ticket_claims=ticket_claims,
    )
    up_id = started["upload_id"]
    storage.uploads[(bound_key, up_id)][1] = {
        "etag": '"etag-part-1"',
        "data": payload,
    }

    db_transcriptions.update_transcription_job_versioned.side_effect = (
        _conflict_then_cancelled_on_start
    )
    with pytest.raises(svc_transcriptions.HTTPException) as exc_comp:
        await svc_transcriptions.complete_transcription_upload(
            job_id=job_id,
            caller_user_id="user-internal-1",
            ticket_claims=ticket_claims,
            upload_id=up_id,
        )
    assert exc_comp.value.status_code == 409
    assert ledger.rows_by_id[job_id]["status"] == "cancelled"
    assert bound_key in storage.completed_objects
    assert bound_key not in storage.deleted_keys

    # 4. `abort` conflicts with concurrent cancellation or exhausts 3 retries -> raises 409
    ledger.rows_by_id[job_id]["status"] = "awaiting_upload"
    with pytest.raises(svc_transcriptions.HTTPException) as exc_abort_cancel:
        await svc_transcriptions.abort_transcription_upload(
            job_id=job_id,
            caller_user_id="user-internal-1",
            ticket_claims=ticket_claims,
            upload_id=up_id,
        )
    assert exc_abort_cancel.value.status_code == 409
    assert ledger.rows_by_id[job_id]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# A1d fix2 — Item 2: Concurrent `start` Interleaving Adopts Existing `upload_id`
#             and Aborts Newly Opened Upload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix2_item2_concurrent_start_interleaving_keeps_single_upload_id_and_aborts_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix2 Item 2: When two `start` calls interleave, the loser sees the winner's active `upload_id` on `latest`, aborts its own newly opened upload, and returns `resumed: True` with the winner's `upload_id` and uploaded parts."""
    ledger, storage = _wire_upload_service_doubles(monkeypatch)
    job_id = "asr_fix2_concurrent_start_01"
    bound_key = f"transcriptions/ws-1/{job_id}/source/concurrent.m4a"
    payload = b"X" * 1024

    await ledger.create_job(
        created_by_user_id="user-internal-1",
        workspace_id="ws-1",
        thread_id="th-1",
        source_object_key=bound_key,
        source_size_bytes=len(payload),
        source_sha256=hashlib.sha256(payload).hexdigest(),
        idempotency_key="idemp-fix2-item2",
        job_id=job_id,
    )
    ticket_claims = {
        "job_id": job_id,
        "workspace_id": "ws-1",
        "caller_user_id": "user-internal-1",
        "source_object_key": bound_key,
        "source_size_bytes": len(payload),
        "ops": ["upload", "submit", "status", "result", "retry", "cancel"],
    }

    # Simulate Caller A winning the DB update while Caller B is in flight after `open_multipart_upload`
    winner_upload_id = storage.open_multipart_upload(bound_key)
    assert winner_upload_id == "mp-001"
    storage.uploads[(bound_key, winner_upload_id)][1] = {
        "etag": '"etag-winner-p1"',
        "data": payload,
    }

    orig_update = ledger.update_versioned
    interleave_triggered = False

    async def _interleaved_update(
        jid: str, *, expected_version: int, **kwargs: Any
    ) -> tuple[bool, dict[str, Any] | None]:
        nonlocal interleave_triggered
        if not interleave_triggered:
            interleave_triggered = True
            # Winner (Caller A) commits `winner_upload_id` at version 1 -> version 2
            await orig_update(
                jid,
                expected_version=1,
                status="awaiting_upload",
                stage="awaiting_upload",
                usage={
                    "upload": {
                        "upload_id": winner_upload_id,
                        "part_size": 16 * 1024 * 1024,
                        "part_count": 1,
                        "source_size_bytes": len(payload),
                        "completed": False,
                        "aborted": False,
                    }
                },
            )
        return await orig_update(jid, expected_version=expected_version, **kwargs)

    db_transcriptions.update_transcription_job_versioned.side_effect = _interleaved_update

    resp_b = await svc_transcriptions.start_transcription_upload(
        job_id=job_id,
        caller_user_id="user-internal-1",
        ticket_claims=ticket_claims,
    )
    assert resp_b["ok"] is True
    assert resp_b["resumed"] is True
    assert resp_b["upload_id"] == winner_upload_id
    assert resp_b["uploaded_parts"] == [
        {"part_number": 1, "size": len(payload), "etag": '"etag-winner-p1"'}
    ]
    # Caller B's newly opened `mp-002` was aborted, leaving only `mp-001` active
    assert (bound_key, "mp-002") in storage.aborted_uploads
    assert list(storage.uploads.keys()) == [(bound_key, winner_upload_id)]
    assert ledger.rows_by_id[job_id]["usage"]["upload"]["upload_id"] == winner_upload_id


# ---------------------------------------------------------------------------
# A1d fix2 — Item 3: Idempotent `complete`, `start` on Already-Completed Upload,
#             and CLI Proceeding to `submit` on `already_completed`
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix2_item3_idempotent_complete_and_start_already_completed_and_cli_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix2 Item 3: Duplicate `complete` returns `already_completed: True` without listing parts; `start` on a completed upload returns `already_completed: True` without opening a new session; CLI proceeds to `submit`."""
    ledger, storage = _wire_upload_service_doubles(monkeypatch)
    job_id = "asr_fix2_idemp_comp_01"
    bound_key = f"transcriptions/ws-1/{job_id}/source/done.m4a"
    payload = b"COMPLETED_AUDIO_PAYLOAD_12345"

    await ledger.create_job(
        created_by_user_id="user-internal-1",
        workspace_id="ws-1",
        thread_id="th-1",
        source_object_key=bound_key,
        source_size_bytes=len(payload),
        source_sha256=hashlib.sha256(payload).hexdigest(),
        idempotency_key="idemp-fix2-item3",
        job_id=job_id,
    )
    ticket_claims = {
        "job_id": job_id,
        "workspace_id": "ws-1",
        "caller_user_id": "user-internal-1",
        "source_object_key": bound_key,
        "source_size_bytes": len(payload),
        "ops": ["upload", "submit", "status", "result", "retry", "cancel"],
    }

    # First start + part + complete
    st1 = await svc_transcriptions.start_transcription_upload(
        job_id=job_id,
        caller_user_id="user-internal-1",
        ticket_claims=ticket_claims,
    )
    up_id = st1["upload_id"]
    storage.uploads[(bound_key, up_id)][1] = {
        "etag": '"etag-done-1"',
        "data": payload,
    }
    comp1 = await svc_transcriptions.complete_transcription_upload(
        job_id=job_id,
        caller_user_id="user-internal-1",
        ticket_claims=ticket_claims,
        upload_id=up_id,
    )
    assert comp1["completed"] is True
    assert comp1["already_completed"] is False

    # Make `list_uploaded_parts` and `open_multipart_upload` fail if called again
    def _forbid_list_parts(_key: str, _uid: str) -> Any:
        raise AssertionError("list_uploaded_parts must not be called on idempotent complete")

    def _forbid_open_upload(_key: str, content_type: str = "application/octet-stream") -> Any:
        raise AssertionError("open_multipart_upload must not be called when upload is already completed")

    monkeypatch.setattr(
        svc_transcriptions,
        "list_uploaded_parts",
        create_autospec(storage.list_uploaded_parts, side_effect=_forbid_list_parts),
    )
    monkeypatch.setattr(
        svc_transcriptions,
        "open_multipart_upload",
        create_autospec(storage.open_multipart_upload, side_effect=_forbid_open_upload),
    )

    # Second complete -> returns `already_completed: True` without calling `list_uploaded_parts`
    comp2 = await svc_transcriptions.complete_transcription_upload(
        job_id=job_id,
        caller_user_id="user-internal-1",
        ticket_claims=ticket_claims,
        upload_id=up_id,
    )
    assert comp2["ok"] is True
    assert comp2["completed"] is True
    assert comp2["already_completed"] is True
    assert comp2["object_size"] == len(payload)

    # `start` on already-completed upload -> returns `already_completed: True` without opening a new session
    st2 = await svc_transcriptions.start_transcription_upload(
        job_id=job_id,
        caller_user_id="user-internal-1",
        ticket_claims=ticket_claims,
    )
    assert st2["ok"] is True
    assert st2["completed"] is True
    assert st2["already_completed"] is True
    assert st2["object_size"] == len(payload)

    # CLI receiving `already_completed: True` on `/upload/start` skips part upload and proceeds straight to `submit`
    tfile = _write_0600_ticket_file(
        tmp_path,
        job_id=job_id,
        audio_bytes=payload,
        extra={
            "source_path": "audio/meeting.m4a",
            "source_object_key": bound_key,
            "source_size_bytes": len(payload),
            "source_sha256": hashlib.sha256(payload).hexdigest(),
        },
    )
    submitted_calls: list[dict[str, Any]] = []

    def _make_json_resp(req: urllib.request.Request, code: int, obj: dict[str, Any]) -> Any:
        body_b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        hdrs = email.message.Message()
        hdrs["Content-Type"] = "application/json; charset=utf-8"
        hdrs["Content-Length"] = str(len(body_b))
        return urllib.response.addinfourl(io.BytesIO(body_b), hdrs, req.full_url, code)

    def _fake_urlopen(req: urllib.request.Request, timeout: float = 30.0) -> Any:
        url = req.full_url
        method = req.get_method()
        if method == "GET" and url.endswith(f"/{job_id}"):
            if not submitted_calls:
                return _make_json_resp(
                    req,
                    200,
                    {
                        "ok": True,
                        "job": {
                            "job_id": job_id,
                            "status": "awaiting_upload",
                            "stage": "awaiting_upload",
                        },
                    },
                )
            return _make_json_resp(
                req,
                200,
                {
                    "ok": True,
                    "job": {
                        "job_id": job_id,
                        "status": "succeeded",
                        "stage": "succeeded",
                        "segment_count": 1,
                        "completed_segments": 1,
                    },
                },
            )
        if method == "POST" and url.endswith(f"/{job_id}/upload/start"):
            return _make_json_resp(req, 200, st2)
        if method == "POST" and url.endswith("/api/v1/dalton/transcriptions"):
            body = json.loads((req.data or b"{}").decode("utf-8"))
            submitted_calls.append(body)
            return _make_json_resp(
                req,
                200,
                {
                    "ok": True,
                    "job": {
                        "job_id": job_id,
                        "status": "succeeded",
                        "stage": "succeeded",
                        "segment_count": 1,
                        "completed_segments": 1,
                    },
                },
            )
        if method == "GET" and url.endswith(f"/{job_id}/result"):
            return _make_json_resp(
                req,
                200,
                {
                    "ok": True,
                    "job_id": job_id,
                    "status": "succeeded",
                    "result": {
                        "text": "[Speaker 1]\n幂等完成进入提交。",
                        "utterances": [],
                        "duration_seconds": 10.0,
                    },
                },
            )
        if method == "GET" and url.endswith(f"/{job_id}/result/raw"):
            return _make_json_resp(req, 200, {"segments": []})
        raise AssertionError(f"Unexpected CLI request: {method} {url}")

    out_dir = tmp_path / "out_idemp"
    out_buf = io.StringIO()
    rc = dalton_asr.main(
        [
            "run",
            "--ticket-file",
            str(tfile),
            "--output-dir",
            str(out_dir),
        ],
        environ={"DALTON_API_URL": "http://127.0.0.1:18080"},
        workspace_root=tmp_path,
        urlopen_fn=_fake_urlopen,
        stream=out_buf,
    )
    assert rc == 0
    assert len(submitted_calls) == 1
    assert submitted_calls[0]["submit"] is True


# ---------------------------------------------------------------------------
# A1d fix3 — Item 1: `complete` Version Conflict Never Deletes the Synthesized
#             Source Object (Advanced Status -> already_completed, Terminal -> 409)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix3_item1_complete_conflict_never_deletes_synthesized_source_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix3 Item 1: On a `complete` version conflict with the row already past `awaiting_upload`, a submit-bound status (`queued`) with the object present returns `already_completed: True`; `cancelled` returns 409 — in both cases the synthesized object is kept and `delete_object` is never called."""
    ledger, storage = _wire_upload_service_doubles(monkeypatch)

    def _conflict_advancing_row_to(status: str) -> Any:
        async def _conflict(
            jid: str, *, expected_version: int, **kwargs: Any
        ) -> tuple[bool, dict[str, Any] | None]:
            row = ledger.rows_by_id[jid]
            row["status"] = status
            row["stage"] = status
            row["version"] = int(row["version"]) + 1
            return False, dict(row)

        return _conflict

    async def _stage_started_upload(
        job_id: str, bound_key: str, payload: bytes
    ) -> tuple[str, dict[str, Any]]:
        await ledger.create_job(
            created_by_user_id="user-internal-1",
            workspace_id="ws-1",
            thread_id="th-1",
            source_object_key=bound_key,
            source_size_bytes=len(payload),
            source_sha256=hashlib.sha256(payload).hexdigest(),
            idempotency_key=f"idemp-{job_id}",
            job_id=job_id,
        )
        ticket_claims = {
            "job_id": job_id,
            "workspace_id": "ws-1",
            "caller_user_id": "user-internal-1",
            "source_object_key": bound_key,
            "source_size_bytes": len(payload),
            "ops": ["upload", "submit", "status", "result", "retry", "cancel"],
        }
        started = await svc_transcriptions.start_transcription_upload(
            job_id=job_id,
            caller_user_id="user-internal-1",
            ticket_claims=ticket_claims,
        )
        up_id = started["upload_id"]
        storage.uploads[(bound_key, up_id)][1] = {
            "etag": '"etag-fix3-p1"',
            "data": payload,
        }
        return up_id, ticket_claims

    # 1. Conflict with `latest` already `queued` (another request completed and
    #    submitted) -> success `already_completed: True`, synthesized object kept
    job_q = "asr_fix3_complete_conflict_queued_01"
    key_q = f"transcriptions/ws-1/{job_q}/source/queued.m4a"
    payload_q = b"FIX3_QUEUED_CONFLICT_AUDIO_BYTES"
    up_q, ticket_q = await _stage_started_upload(job_q, key_q, payload_q)

    db_transcriptions.update_transcription_job_versioned.side_effect = (
        _conflict_advancing_row_to("queued")
    )
    resp_q = await svc_transcriptions.complete_transcription_upload(
        job_id=job_q,
        caller_user_id="user-internal-1",
        ticket_claims=ticket_q,
        upload_id=up_q,
    )
    assert resp_q["ok"] is True
    assert resp_q["completed"] is True
    assert resp_q["already_completed"] is True
    assert resp_q["object_size"] == len(payload_q)
    assert storage.completed_objects[key_q] == payload_q
    assert key_q not in storage.deleted_keys
    assert ledger.rows_by_id[job_q]["status"] == "queued"

    # 2. Conflict with `latest` `cancelled` -> 409, and still nothing deleted
    db_transcriptions.update_transcription_job_versioned.side_effect = ledger.update_versioned
    job_c = "asr_fix3_complete_conflict_cancelled_01"
    key_c = f"transcriptions/ws-1/{job_c}/source/cancelled.m4a"
    payload_c = b"FIX3_CANCELLED_CONFLICT_AUDIO_BYTES"
    up_c, ticket_c = await _stage_started_upload(job_c, key_c, payload_c)

    db_transcriptions.update_transcription_job_versioned.side_effect = (
        _conflict_advancing_row_to("cancelled")
    )
    with pytest.raises(svc_transcriptions.HTTPException) as exc_c:
        await svc_transcriptions.complete_transcription_upload(
            job_id=job_c,
            caller_user_id="user-internal-1",
            ticket_claims=ticket_c,
            upload_id=up_c,
        )
    assert exc_c.value.status_code == 409
    assert storage.completed_objects[key_c] == payload_c
    assert storage.deleted_keys == []
    assert ledger.rows_by_id[job_c]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# A1d fix3 — Item 2: `start` Conflict with an Unverifiable In-Flight Upload
#             Returns 503 Instead of Overwriting the Other Writer's `upload_id`
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix3_item2_start_conflict_unverifiable_foreign_upload_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix3 Item 2: When a concurrent `start` has already recorded its `upload_id` and `list_uploaded_parts` cannot verify it (returns None), the loser aborts its own newly opened upload, writes nothing to the job row, and raises 503 — the row keeps the winner's `upload_id`."""
    ledger, storage = _wire_upload_service_doubles(monkeypatch)
    job_id = "asr_fix3_start_unverifiable_01"
    bound_key = f"transcriptions/ws-1/{job_id}/source/unverifiable.m4a"
    payload = b"Y" * 2048

    await ledger.create_job(
        created_by_user_id="user-internal-1",
        workspace_id="ws-1",
        thread_id="th-1",
        source_object_key=bound_key,
        source_size_bytes=len(payload),
        source_sha256=hashlib.sha256(payload).hexdigest(),
        idempotency_key="idemp-fix3-item2",
        job_id=job_id,
    )
    ticket_claims = {
        "job_id": job_id,
        "workspace_id": "ws-1",
        "caller_user_id": "user-internal-1",
        "source_object_key": bound_key,
        "source_size_bytes": len(payload),
        "ops": ["upload", "submit", "status", "result", "retry", "cancel"],
    }

    # Winner (Caller A) opens its upload; the loser (Caller B) cannot list the
    # winner's parts (object storage flaking) once it loses the versioned write
    winner_upload_id = storage.open_multipart_upload(bound_key)
    assert winner_upload_id == "mp-001"

    from src.utils.storage import s3_compatible as s3_mod

    monkeypatch.setattr(
        svc_transcriptions,
        "list_uploaded_parts",
        create_autospec(s3_mod.list_uploaded_parts, return_value=None),
    )

    orig_update = ledger.update_versioned
    interleave_triggered = False

    async def _interleaved_update(
        jid: str, *, expected_version: int, **kwargs: Any
    ) -> tuple[bool, dict[str, Any] | None]:
        nonlocal interleave_triggered
        if not interleave_triggered:
            interleave_triggered = True
            # Winner commits its `upload_id` at version 1 -> version 2 while
            # Caller B is in flight after `open_multipart_upload`
            await orig_update(
                jid,
                expected_version=1,
                status="awaiting_upload",
                stage="awaiting_upload",
                usage={
                    "upload": {
                        "upload_id": winner_upload_id,
                        "part_size": 16 * 1024 * 1024,
                        "part_count": 1,
                        "source_size_bytes": len(payload),
                        "completed": False,
                        "aborted": False,
                    }
                },
            )
        return await orig_update(jid, expected_version=expected_version, **kwargs)

    db_transcriptions.update_transcription_job_versioned.side_effect = _interleaved_update

    with pytest.raises(svc_transcriptions.HTTPException) as exc_b:
        await svc_transcriptions.start_transcription_upload(
            job_id=job_id,
            caller_user_id="user-internal-1",
            ticket_claims=ticket_claims,
        )
    assert exc_b.value.status_code == 503
    # Caller B's newly opened `mp-002` was aborted; the winner's session and row
    # entry are untouched — nothing was overwritten
    assert (bound_key, "mp-002") in storage.aborted_uploads
    assert (bound_key, winner_upload_id) not in storage.aborted_uploads
    assert list(storage.uploads.keys()) == [(bound_key, winner_upload_id)]
    assert ledger.rows_by_id[job_id]["usage"]["upload"]["upload_id"] == winner_upload_id


# ---------------------------------------------------------------------------
# A1d fix2 — Item 4: Reconciliation Cursor Rotation Prevents Starvation
#             (60 Stuck Jobs All Synced Within 2 Passes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix2_item4_reconcile_cursor_rotates_all_60_stuck_jobs_within_two_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix2 Item 4: With 60 stuck non-terminal jobs and batch limit 50, `job_id` cursor rotation syncs all 60 jobs within 2 passes, topping up from the beginning and resetting the cursor when `< limit` rows remain."""
    monkeypatch.setattr(svc_transcriptions, "is_transcription_reconciliation_enabled", lambda: True)
    setattr(svc_transcriptions.reconcile_active_transcription_jobs_once, "_cursor", "")

    all_jobs = [
        {
            "job_id": f"asr_stuck_{idx:03d}",
            "created_by_user_id": "u-reconcile",
            "workspace_id": "ws-reconcile",
            "thread_id": "th-1",
            "status": "transcribing" if idx % 2 == 0 else "indeterminate",
            "stage": "transcribing",
            "version": 1,
            "usage": {"attempt": 1, "bridge_version": 2},
        }
        for idx in range(1, 61)
    ]

    async def _fake_list_non_terminal(
        *, limit: int = 50, after_job_id: str | None = None, conn: Any = None
    ) -> list[dict[str, Any]]:
        cursor_str = str(after_job_id or "").strip()
        filtered = [
            dict(j)
            for j in all_jobs
            if j["status"] not in ("awaiting_upload", "succeeded", "failed", "cancelled")
            and j["job_id"] > cursor_str
        ]
        filtered.sort(key=lambda j: j["job_id"])
        return filtered[:limit]

    monkeypatch.setattr(
        db_transcriptions,
        "list_non_terminal_transcription_jobs",
        create_autospec(
            db_transcriptions.list_non_terminal_transcription_jobs,
            side_effect=_fake_list_non_terminal,
        ),
    )

    polled_pass_1: list[str] = []
    polled_pass_2: list[str] = []
    current_pass_list = polled_pass_1

    async def _fake_bridge_get(job_id: str) -> tuple[int, dict[str, Any]]:
        current_pass_list.append(job_id)
        # Stuck job on bridge: same attempt=1, version=2 -> DB `updated_at` does not move
        return 200, {
            "ok": True,
            "job": {
                "job_id": job_id,
                "status": "transcribing",
                "stage": "transcribing",
                "attempt": 1,
                "version": 2,
            },
        }

    monkeypatch.setattr(
        svc_transcriptions,
        "call_bridge_get_transcription",
        create_autospec(
            svc_transcriptions.call_bridge_get_transcription,
            side_effect=_fake_bridge_get,
        ),
    )

    pass1 = await svc_transcriptions.reconcile_active_transcription_jobs_once(limit=50)
    assert pass1["scanned"] == 50
    assert pass1["synced"] == 50
    assert pass1["next_cursor"] == "asr_stuck_050"
    assert polled_pass_1 == [f"asr_stuck_{idx:03d}" for idx in range(1, 51)]

    current_pass_list = polled_pass_2
    pass2 = await svc_transcriptions.reconcile_active_transcription_jobs_once(limit=50)
    assert pass2["scanned"] == 50
    assert pass2["synced"] == 50
    # Pass 2 fetched 10 remaining (`051..060`), topped up 40 from the beginning (`001..040`), and reset cursor to ""
    assert pass2["next_cursor"] == ""
    assert polled_pass_2[:10] == [f"asr_stuck_{idx:03d}" for idx in range(51, 61)]
    assert set(polled_pass_1) | set(polled_pass_2) == {
        f"asr_stuck_{idx:03d}" for idx in range(1, 61)
    }


# ---------------------------------------------------------------------------
# A1d fix2 — Item 5: Cancel Merges Completed & In-Flight Request IDs by Union
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix2_item5_cancel_merges_request_ids_by_order_preserving_union(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix2 Item 5: When cancelling a multi-segment job where segment 0 already completed (`req-seg-0` on job row) and segment 1 is in flight (`req-seg-1` in `cancellation.submitted_request_ids`), backend preserves the union `['req-seg-0', 'req-seg-1']`."""
    ledger = InMemoryTranscriptionLedger()
    job_id = "asr_fix2_union_req_ids_01"
    await ledger.create_job(
        created_by_user_id="user-internal-1",
        workspace_id="ws-1",
        thread_id="th-1",
        source_object_key=f"transcriptions/ws-1/{job_id}/source/a.m4a",
        source_size_bytes=2048,
        source_sha256="d" * 64,
        idempotency_key="idemp-fix2-item5",
        job_id=job_id,
    )
    # Job row already recorded completed segment 0's request ID `req-seg-0`
    ledger.rows_by_id[job_id]["status"] = "transcribing"
    ledger.rows_by_id[job_id]["stage"] = "transcribing"
    ledger.rows_by_id[job_id]["segment_count"] = 2
    ledger.rows_by_id[job_id]["completed_segments"] = 1
    ledger.rows_by_id[job_id]["provider_request_ids"] = ["req-seg-0"]

    monkeypatch.setattr(
        db_transcriptions,
        "get_transcription_job",
        create_autospec(db_transcriptions.get_transcription_job, side_effect=ledger.get_job),
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
        "authorize_job_access",
        create_autospec(svc_transcriptions.authorize_job_access, return_value="internal"),
    )

    async def _fake_bridge_cancel(jid: str, action: str) -> tuple[int, dict[str, Any]]:
        assert jid == job_id and action == "cancel"
        return 200, {
            "ok": True,
            "job": {
                "job_id": job_id,
                "status": "cancelled",
                "stage": "cancelled",
                "attempt": 1,
                "version": 4,
                "segment_count": 2,
                "completed_segments": 1,
                "usage": {
                    "duration_seconds": 1200.0,
                    "submitted_seconds": 1200.0,
                    "completed_seconds": 600.0,
                },
            },
            "cancellation": {
                "cancelled_at": "2026-10-29T06:00:00Z",
                "submitted_segments_count": 1,
                "submitted_request_ids": ["req-seg-1", "req-seg-0"],
                "unsubmitted_cancelled_count": 0,
                "completed_segments_count": 1,
                "provider_cancel_verified": False,
                "billing_verified": False,
            },
        }

    monkeypatch.setattr(
        svc_transcriptions,
        "call_bridge_action_transcription",
        create_autospec(
            svc_transcriptions.call_bridge_action_transcription,
            side_effect=_fake_bridge_cancel,
        ),
    )

    res = await svc_transcriptions.cancel_transcription_job(
        job_id=job_id,
        caller_user_id="user-internal-1",
    )
    updated_job = res["job"]
    assert updated_job["provider_request_ids"] == ["req-seg-0", "req-seg-1"]
    att_rec = updated_job["usage"]["attempts"][0]
    assert att_rec["provider_request_ids"] == ["req-seg-0", "req-seg-1"]


# ---------------------------------------------------------------------------
# A1d fix2 — Item 6: Cancelling a Failed Job the Next Day Does Not Rewrite
#             the Finalized Attempt Record or Daily Usage Summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix2_item6_cancel_after_failed_attempt_next_day_preserves_attempt_usage_and_daily_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix2 Item 6: The first usage record for `(job_id, attempt)` is final. Cancelling a job the day after attempt 1 failed updates job status & cancellation metadata but preserves attempt 1's `failed` status, date, and counts across bridge state, bridge JSONL, and backend DB."""
    jobs_dir = tmp_path / "asr_jobs_fix2_item6"
    mgr = asr_jobs.AsrJobManager(
        jobs_dir,
        worker=asr_jobs.FakeAsrWorker(asr_jobs.FakeAsrWorkerConfig(duration_seconds=300.0)),
    )
    job_id = "asr_fix2_failed_then_cancel_01"
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    day1_ts = "2026-10-28T23:10:00Z"
    failed_attempt_1 = {
        "job_id": job_id,
        "attempt": 1,
        "user_id": "u-analyst-1",
        "workspace_id": "ws-1",
        "thread_id": "th-1",
        "status": "failed",
        "ok": False,
        "duration_seconds": 600.0,
        "submitted_seconds": 600.0,
        "completed_seconds": 0.0,
        "billable_seconds": None,
        "segment_count": 1,
        "completed_segments": 0,
        "error_category": "provider_error",
        "provider_request_ids": ["req-fail-day1"],
        "recorded_at": day1_ts,
    }
    asr_jobs.append_bridge_usage_jsonl(mgr.usage_ledger_path, failed_attempt_1)
    bridge_state = {
        "job_id": job_id,
        "status": "failed",
        "stage": "failed",
        "version": 3,
        "attempt": 1,
        "caller": {
            "user_id": "u-analyst-1",
            "workspace_id": "ws-1",
            "thread_id": "th-1",
        },
        "segment_count": 1,
        "completed_segments": 0,
        "provider_request_ids": ["req-fail-day1"],
        "segments": [
            {
                "index": 0,
                "start_s": 0.0,
                "end_s": 600.0,
                "status": "failed",
                "provider_request_id": "req-fail-day1",
                "attempt": 1,
                "error_category": "provider_error",
            }
        ],
        "usage": {
            "user_id": "u-analyst-1",
            "workspace_id": "ws-1",
            "thread_id": "th-1",
            "attempt": 1,
            "status": "failed",
            "ok": False,
            "duration_seconds": 600.0,
            "submitted_seconds": 600.0,
            "completed_seconds": 0.0,
            "billable_seconds": None,
            "error_category": "provider_error",
            "attempts": [dict(failed_attempt_1)],
        },
        "usage_recorded_attempts": [1],
        "error_category": "provider_error",
        "error_message": "Upstream Seed-ASR failed on day 1",
        "created_at": "2026-10-28T23:00:00Z",
        "updated_at": day1_ts,
        "completed_at": day1_ts,
    }
    (job_dir / "state.json").write_text(
        json.dumps(bridge_state, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    ledger = InMemoryTranscriptionLedger()
    db_row = await ledger.create_job(
        created_by_user_id="u-analyst-1",
        workspace_id="ws-1",
        thread_id="th-1",
        source_object_key=f"transcriptions/ws-1/{job_id}/source/a.m4a",
        source_size_bytes=4096,
        source_sha256="e" * 64,
        idempotency_key="idemp-fix2-item6",
        job_id=job_id,
    )
    monkeypatch.setattr(
        db_transcriptions,
        "get_transcription_job",
        create_autospec(db_transcriptions.get_transcription_job, side_effect=ledger.get_job),
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
        "authorize_job_access",
        create_autospec(svc_transcriptions.authorize_job_access, return_value="internal"),
    )

    # Sync the day-1 failed state into backend DB first
    await svc_transcriptions._sync_job_from_bridge_state(db_row, bridge_state)
    before_day1_summary = db_transcriptions.summarize_transcription_usage(
        [ledger.rows_by_id[job_id]],
        bridge_records=mgr.read_usage_ledger(),
        start_date="2026-10-28",
        end_date="2026-10-28",
    )
    assert before_day1_summary["totals"]["failed_attempts"] == 1
    assert before_day1_summary["totals"]["cancelled_attempts"] == 0

    # Next day (2026-10-29), user explicitly cancels the failed job
    async def _bridge_cancel_next_day(jid: str, action: str) -> tuple[int, dict[str, Any]]:
        assert jid == job_id and action == "cancel"
        return await mgr.cancel_job(jid)

    monkeypatch.setattr(
        svc_transcriptions,
        "call_bridge_action_transcription",
        create_autospec(
            svc_transcriptions.call_bridge_action_transcription,
            side_effect=_bridge_cancel_next_day,
        ),
    )

    cancel_res = await svc_transcriptions.cancel_transcription_job(
        job_id=job_id,
        caller_user_id="u-analyst-1",
    )
    cancelled_job = cancel_res["job"]
    assert cancelled_job["status"] == "cancelled"
    assert isinstance(cancelled_job["usage"].get("cancellation"), dict)

    # 1. Bridge state, bridge JSONL, and backend DB all preserve attempt 1 as `failed` on `2026-10-28`
    bridge_after = mgr.read_state(job_id)
    assert bridge_after is not None
    assert bridge_after["status"] == "cancelled"
    assert len(bridge_after["usage"]["attempts"]) == 1
    assert bridge_after["usage"]["attempts"][0]["status"] == "failed"
    assert bridge_after["usage"]["attempts"][0]["recorded_at"] == day1_ts

    jsonl_after = mgr.read_usage_ledger()
    assert len(jsonl_after) == 1
    assert jsonl_after[0]["status"] == "failed"
    assert jsonl_after[0]["recorded_at"] == day1_ts

    assert len(cancelled_job["usage"]["attempts"]) == 1
    db_att = cancelled_job["usage"]["attempts"][0]
    assert db_att["status"] == "failed"
    assert db_att["ok"] is False
    assert db_att["recorded_at"] == day1_ts
    assert db_att["error_category"] == "provider_error"
    assert cancelled_job["usage"]["summary"]["failed_attempts"] == 1
    assert cancelled_job["usage"]["summary"]["cancelled_attempts"] == 0

    # 2. Daily summaries for Day 1 (2026-10-28) and Day 2 (2026-10-29) are unchanged
    after_day1_summary = db_transcriptions.summarize_transcription_usage(
        [cancelled_job],
        bridge_records=jsonl_after,
        start_date="2026-10-28",
        end_date="2026-10-28",
    )
    assert after_day1_summary["totals"] == before_day1_summary["totals"]
    assert after_day1_summary["by_user_date"] == before_day1_summary["by_user_date"]
    assert after_day1_summary["reconciliation"]["mismatch_count"] == 0

    after_day2_summary = db_transcriptions.summarize_transcription_usage(
        [cancelled_job],
        bridge_records=jsonl_after,
        start_date="2026-10-29",
        end_date="2026-10-29",
    )
    assert after_day2_summary["totals"]["attempt_count"] == 0
    assert after_day2_summary["totals"]["cancelled_attempts"] == 0


