"""Unit tests for company-wiki ingestion across the host bridge (`POST /wiki/ingest`) and Dalton tool (`ingest_company_wiki`)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import psycopg
import pytest
import redis.asyncio as aioredis
from deepagents.backends.protocol import FileDownloadResponse

from ptc_agent.agent.backends.sandbox import SandboxBackend
from ptc_agent.core.sandbox.jobs import Ended, Failed, JobHandle, JobRef, Running
from src.dalton.database import wiki_ingests as db_wiki_ingests
from src.dalton.tools import build_dalton_tools
from src.dalton.tools import wiki_tools
from src.utils.cache.redis_cache import RedisCacheClient

REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_DIR = REPO_ROOT / "scripts" / "dalton"
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import researchdata_bridge as rb  # noqa: E402


@pytest.fixture(autouse=True)
def _wiki_caller_is_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every tool-level test here is an internal team member ingesting wiki docs."""
    monkeypatch.setattr(
        wiki_tools,
        "get_user_tier",
        create_autospec(wiki_tools.get_user_tier, return_value="internal"),
    )


def _make_test_bridge(tmp_path: Path) -> rb.ResearchDataBridge:
    """Create an isolated `ResearchDataBridge` rooted under `tmp_path` without touching `~/.openclaw`."""
    ocw_root = tmp_path / "openclaw_workspace"
    skill_dir = ocw_root / "skills" / "company-wiki"
    skill_dir.mkdir(parents=True, exist_ok=True)
    run_sh = skill_dir / "run.sh"
    run_sh.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    run_sh.chmod(0o755)

    config_json = skill_dir / "config.json"
    config_json.write_text(
        json.dumps(
            {
                "wiki_dir": "wiki",
                "aliases": {
                    "腾讯": "TENCENT",
                    "00700": "TENCENT",
                    "0700.hk": "TENCENT",
                    "affirm": "AFRM",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    (ocw_root / "wiki" / "companies" / "NVDA").mkdir(parents=True, exist_ok=True)
    (ocw_root / "wiki" / "companies" / "TENCENT").mkdir(parents=True, exist_ok=True)
    (ocw_root / "wiki" / "sectors" / "us-semiconductor").mkdir(parents=True, exist_ok=True)

    env_file = tmp_path / "bridge.env"
    env_file.write_text("GEMINI_API_KEY=test_gemini_key_for_unit_test\n", encoding="utf-8")

    cfg = rb.BridgeConfig()
    cfg.env_path = env_file
    cfg.token = "mcp-secret-token"
    cfg.admin_token = "admin-secret-token"
    cfg.ocw_root = ocw_root
    cfg.wiki_run_sh = run_sh
    cfg.wiki_ingest_audit_file = tmp_path / "var" / "dalton" / "wiki_ingest_audit.jsonl"
    cfg.wiki_ingest_temp_dir = tmp_path / "var" / "dalton" / "tmp" / "wiki_ingest"
    cfg.wiki_ingest_timeout_s = 30.0
    return rb.ResearchDataBridge(cfg)


async def _send_raw_http_request(
    bridge: rb.ResearchDataBridge,
    method: str,
    path: str,
    headers: dict[str, str],
    body_obj: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Invoke `_handle_client` over an in-memory StreamReader/StreamWriter pair."""
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


# ---------------------------------------------------------------------------
# 1. Bridge Authentication & Read-Only MCP Route Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_wiki_ingest_auth_admin_passes_mcp_gets_403(tmp_path: Path) -> None:
    bridge = _make_test_bridge(tmp_path)
    req_body = {
        "target_type": "company",
        "target": "NVDA",
        "content_type": "专家访谈",
        "date": "2026-10-25",
        "content": "NVDA Blackwell rack supply chain expert interview takeaways.",
        "ingested_by": {"user_id": "u-1", "email": "analyst@dalton.test"},
        "ingested_via": {"workspace_id": "ws-nvda", "thread_id": "th-1"},
        "dry_run": True,
    }

    # 1. Missing token -> 401
    code_unauth, body_unauth = await _send_raw_http_request(
        bridge, "POST", "/wiki/ingest", {}, req_body
    )
    assert code_unauth == 401
    assert body_unauth["ok"] is False

    # 2. MCP token -> 403 Forbidden
    code_mcp, body_mcp = await _send_raw_http_request(
        bridge,
        "POST",
        "/wiki/ingest",
        {"Authorization": f"Bearer {bridge.cfg.token}"},
        req_body,
    )
    assert code_mcp == 403
    assert body_mcp["ok"] is False

    # 3. Admin token -> 200 OK
    code_admin, body_admin = await _send_raw_http_request(
        bridge,
        "POST",
        "/wiki/ingest",
        {"Authorization": f"Bearer {bridge.cfg.admin_token}"},
        req_body,
    )
    assert code_admin == 200
    assert body_admin["ok"] is True
    assert body_admin["status"] == "dry_run"
    assert body_admin["target"] == "NVDA"


def test_bridge_company_wiki_mcp_route_stays_read_only() -> None:
    spec = rb.ROUTE_CONTRACT_SPECS["company-wiki"]
    assert spec["operations"] == ["search", "read", "list", "topic"]
    _, _, err = rb._normalize_and_validate_call_args(
        "research",
        {"route": "company-wiki", "operation": "add", "query": "NVDA"},
    )
    assert err is not None
    assert err["error_code"] == "invalid_operation"


# ---------------------------------------------------------------------------
# 2. Bridge Validation Tests (slug, alias, content_type, size, ext, full-byte secrets)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_wiki_ingest_slug_and_alias_validations(tmp_path: Path) -> None:
    bridge = _make_test_bridge(tmp_path)
    base_req = {
        "target_type": "company",
        "content_type": "研究笔记",
        "date": "2026-10-25",
        "content": "Valid research note content.",
        "ingested_by": {"user_id": "u-1", "email": "u1@dalton.test"},
        "ingested_via": {"workspace_id": "ws-1", "thread_id": "th-1"},
        "dry_run": True,
    }

    # New valid uppercase company slug -> passes
    code, res = await bridge.handle_wiki_ingest({**base_req, "target": "UNITREE"})
    assert code == 200
    assert res["ok"] is True
    assert res["target"] == "UNITREE"
    assert res["is_existing_slug"] is False

    # Existing alias in config.json ("腾讯" -> "TENCENT") -> passes
    code, res = await bridge.handle_wiki_ingest({**base_req, "target": "腾讯"})
    assert code == 200
    assert res["ok"] is True
    assert res["target"] == "TENCENT"

    # Unmapped Chinese / A-share / HK name -> returns needs_alias without modifying config.json
    cfg_before = (
        bridge.cfg.ocw_root / "skills" / "company-wiki" / "config.json"
    ).read_text(encoding="utf-8")
    for unmapped in ("宇树科技", "600519.SH", "09988.HK", "SH600519"):
        code, res = await bridge.handle_wiki_ingest({**base_req, "target": unmapped})
        assert code == 400
        assert res["ok"] is False
        assert res["status"] == "needs_alias"
    cfg_after = (
        bridge.cfg.ocw_root / "skills" / "company-wiki" / "config.json"
    ).read_text(encoding="utf-8")
    assert cfg_before == cfg_after

    # Pure numeric ticker -> rejected
    code, res = await bridge.handle_wiki_ingest({**base_req, "target": "600519"})
    assert code == 400
    assert res["ok"] is False
    assert res["status"] == "needs_alias"
    assert res["error_code"] == "numeric_ticker_rejected"

    # Reserved temp* -> rejected
    code, res = await bridge.handle_wiki_ingest({**base_req, "target": "TEMP-CO"})
    assert code == 400
    assert res["ok"] is False
    assert res["error_code"] == "reserved_slug"

    # Invalid company regex -> rejected
    code, res = await bridge.handle_wiki_ingest({**base_req, "target": "bad_company!"})
    assert code == 400
    assert res["ok"] is False
    assert res["status"] == "invalid_slug"

    # Valid new sector slug (`^[a-z][a-z0-9-]{0,39}$`) -> passes
    code, res = await bridge.handle_wiki_ingest(
        {**base_req, "target_type": "sector", "target": "china-robotics"}
    )
    assert code == 200
    assert res["ok"] is True
    assert res["target"] == "china-robotics"

    # Invalid sector slug (uppercase) -> rejected
    code, res = await bridge.handle_wiki_ingest(
        {**base_req, "target_type": "sector", "target": "CHINA-ROBOTICS"}
    )
    assert code == 400
    assert res["ok"] is False
    assert res["status"] == "invalid_slug"


@pytest.mark.asyncio
async def test_bridge_wiki_ingest_content_type_and_sell_side_rejection(tmp_path: Path) -> None:
    bridge = _make_test_bridge(tmp_path)
    base_req = {
        "target_type": "company",
        "target": "NVDA",
        "date": "2026-10-25",
        "content": "Notes from call.",
        "ingested_by": {"user_id": "u-1", "email": "u1@dalton.test"},
        "ingested_via": {"workspace_id": "ws-1", "thread_id": "th-1"},
        "dry_run": True,
    }

    for sell_type in ("券商研报", "卖方交流", "sales color", "分析师电话会", "Broker Report"):
        code, res = await bridge.handle_wiki_ingest({**base_req, "content_type": sell_type})
        assert code == 400
        assert res["ok"] is False
        assert res["status"] == "sell_side_rejected"
        assert "sales notes" in res["error"] or "sales notes" in str(res.get("hint", ""))

    code, res = await bridge.handle_wiki_ingest({**base_req, "content_type": "随便写的类型"})
    assert code == 400
    assert res["ok"] is False
    assert res["status"] == "invalid_content_type"


@pytest.mark.asyncio
async def test_bridge_wiki_ingest_size_extension_and_full_byte_secret_validations(
    tmp_path: Path,
) -> None:
    bridge = _make_test_bridge(tmp_path)
    base_req = {
        "target_type": "company",
        "target": "NVDA",
        "content_type": "研究笔记",
        "date": "2026-10-25",
        "ingested_by": {"user_id": "u-1", "email": "u1@dalton.test"},
        "ingested_via": {"workspace_id": "ws-1", "thread_id": "th-1"},
        "dry_run": True,
    }

    # Text > 200k chars rejected
    code, res = await bridge.handle_wiki_ingest(
        {**base_req, "content": "A" * (rb.WIKI_INGEST_MAX_TEXT_CHARS + 1)}
    )
    assert code == 400
    assert res["status"] == "payload_too_large"
    assert res["error_code"] == "text_too_large"

    # Disallowed extension (.gif, .exe) rejected
    code, res = await bridge.handle_wiki_ingest(
        {
            **base_req,
            "file": {
                "filename": "anim.gif",
                "content_b64": base64.b64encode(b"GIF89a...").decode("ascii"),
            },
        }
    )
    assert code == 400
    assert res["status"] == "invalid_extension"

    # File > 20 MB rejected
    big_bytes = b"x" * (rb.WIKI_INGEST_MAX_FILE_BYTES + 1)
    code, res = await bridge.handle_wiki_ingest(
        {
            **base_req,
            "file": {
                "filename": "huge.pdf",
                "bytes": big_bytes,
            },
        }
    )
    assert code == 400
    assert res["status"] == "payload_too_large"
    assert res["error_code"] == "file_too_large"

    # Secret patterns in text rejected
    secret_samples = [
        "Here is an OpenAI key sk-proj-1234567890abcdefghijklmno",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----",
        "Config dump: gemini_api_key = 'AIzaSyD-1234567890abcdefghijklmnopqrstuv'",
    ]
    for sample in secret_samples:
        code, res = await bridge.handle_wiki_ingest({**base_req, "content": sample})
        assert code == 400
        assert res["ok"] is False
        assert res["status"] == "secret_detected"

    # Round 2 Fix 3: Secret embedded past 80 KB inside binary PDF/image with non-printable bytes is detected
    late_secret_pdf = (
        b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n"
        + (b"\x00\xff\x89\xfe" * 22000)  # 88,000 non-printable binary bytes (> 50k truncation point)
        + b"sk-proj-9876543210abcdefghijklmnopqrst"
        + b"\n%%EOF"
    )
    code_late_pdf, res_late_pdf = await bridge.handle_wiki_ingest(
        {
            **base_req,
            "file": {
                "filename": "deep_scan.pdf",
                "content_b64": base64.b64encode(late_secret_pdf).decode("ascii"),
            },
        }
    )
    assert code_late_pdf == 400
    assert res_late_pdf["ok"] is False
    assert res_late_pdf["status"] == "secret_detected"

    # Round 2 Fix 3: Compressed-stream PDF / PNG image without extractable text includes scan_note in preview
    compressed_pdf = b"%PDF-1.4\n1 0 obj << /Filter /FlateDecode /Length 16 >> stream\n\x78\x9c\x03\x00\x00\x00\x00\x01\nendstream\n%%EOF"
    code_preview_pdf, res_preview_pdf = await bridge.handle_wiki_ingest(
        {
            **base_req,
            "file": {
                "filename": "compressed_deck.pdf",
                "content_b64": base64.b64encode(compressed_pdf).decode("ascii"),
            },
        }
    )
    assert code_preview_pdf == 200
    assert res_preview_pdf["ok"] is True
    assert res_preview_pdf["text_extracted"] is False
    assert res_preview_pdf["scan_note"] == "无法提取正文，仅做了字节扫描"


# ---------------------------------------------------------------------------
# 3. Bridge Security Fix 1: `--content @path` Host File Read Prevention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_wiki_ingest_content_starting_with_at_sign_uses_temp_file_not_host_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify `--content` starting with `@/etc/hosts` or `@~/.ssh/id_rsa` is written to a 0600 bridge temp file."""
    bridge = _make_test_bridge(tmp_path)

    fake_proc = create_autospec(asyncio.subprocess.Process, instance=True)
    fake_proc.returncode = 0
    fake_proc.communicate.return_value = (
        json.dumps(
            {
                "ok": True,
                "action": "added",
                "doc_id": 1901,
                "filepath": "wiki/companies/NVDA/2026-10-25-研究笔记.md",
            }
        ).encode("utf-8"),
        b"",
    )

    captured_temp_paths: list[Path] = []
    captured_temp_contents: list[str] = []
    captured_temp_modes: list[int] = []

    async def _on_subproc(*args: Any, **kwargs: Any) -> Any:
        argv = list(args)
        assert "--content" in argv
        content_arg = argv[argv.index("--content") + 1]
        assert content_arg.startswith("@")
        temp_path = Path(content_arg[1:])
        # Must be inside bridge.cfg.wiki_ingest_temp_dir, never /etc/hosts or ~/.ssh/id_rsa
        assert temp_path.resolve().is_relative_to(bridge.cfg.wiki_ingest_temp_dir.resolve())
        assert temp_path.is_file()
        captured_temp_paths.append(temp_path)
        captured_temp_contents.append(temp_path.read_text(encoding="utf-8"))
        captured_temp_modes.append(temp_path.stat().st_mode & 0o777)
        return fake_proc

    mock_subproc = create_autospec(asyncio.create_subprocess_exec, side_effect=_on_subproc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subproc)

    for idx, payload_text in enumerate(
        (
            "@/etc/hosts",
            "@~/.ssh/id_rsa\nAdditional analyst commentary on supply chain.",
        )
    ):
        code, res = await bridge.handle_wiki_ingest(
            {
                "target_type": "company",
                "target": "NVDA",
                "content_type": "研究笔记",
                "date": f"2026-10-2{idx + 1}",
                "content": payload_text,
                "ingested_by": {"user_id": "u-1", "email": "u1@dalton.test"},
                "ingested_via": {"workspace_id": "ws-nvda", "thread_id": "th-1"},
                "dry_run": False,
            }
        )
        assert code == 200
        assert res["ok"] is True
        assert captured_temp_contents[-1] == payload_text
        assert captured_temp_modes[-1] == 0o600
        # Temp file must be cleaned up after CLI completes
        assert not captured_temp_paths[-1].exists()


# ---------------------------------------------------------------------------
# 4. Bridge Execution, Dry-Run, Audit Log, Concurrency, Request-ID Replay & Cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_wiki_ingest_dry_run_does_not_write_or_invoke_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_test_bridge(tmp_path)
    mock_subproc = create_autospec(asyncio.create_subprocess_exec)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subproc)

    code, res = await bridge.handle_wiki_ingest(
        {
            "target_type": "company",
            "target": "NVDA",
            "content_type": "管理层会议纪要",
            "date": "2026-10-25",
            "content": "1x1 meeting with IR on Blackwell rack shipments.",
            "ingested_by": {"user_id": "u-1", "email": "u1@dalton.test"},
            "ingested_via": {"workspace_id": "ws-nvda", "thread_id": "th-1"},
            "dry_run": True,
        }
    )
    assert code == 200
    assert res["ok"] is True
    assert res["status"] == "dry_run"
    assert res["filename"] == "2026-10-25-管理层会议纪要.md"
    assert res["duplicate"] is False
    mock_subproc.assert_not_called()
    assert not bridge.cfg.wiki_ingest_audit_file.exists()


@pytest.mark.asyncio
async def test_bridge_wiki_ingest_executes_add_and_add_file_and_logs_audit_and_detects_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_test_bridge(tmp_path)

    fake_proc = create_autospec(asyncio.subprocess.Process, instance=True)
    fake_proc.returncode = 0
    fake_proc.communicate.return_value = (
        json.dumps(
            {
                "ok": True,
                "action": "added",
                "doc_id": 1888,
                "category_type": "company",
                "category_name": "NVDA",
                "content_type": "专家访谈",
                "date": "2026-10-25",
                "filename": "2026-10-25-专家访谈.md",
                "filepath": "wiki/companies/NVDA/2026-10-25-专家访谈.md",
                "chunks": 3,
                "tags": ["#CoWoS", "#Blackwell"],
                "related_companies": ["TSM"],
                "related_sectors": ["us-semiconductor"],
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        b"",
    )
    mock_subproc = create_autospec(asyncio.create_subprocess_exec, return_value=fake_proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subproc)

    body_text = (
        "访谈纪要：台积电 CoWoS-L 2026 年产能分配与 NVDA GB200/GB300 机柜出货节奏。"
        "专家指出 Q4 机柜良率已明显爬坡，液冷快接头双供格局稳定。"
    )
    code, res = await bridge.handle_wiki_ingest(
        {
            "request_id": "req-uuid-1888",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "专家访谈",
            "date": "2026-10-25",
            "source": "Expert Network",
            "source_url": "https://example.com/call/1888",
            "content": body_text,
            "ingested_by": {"user_id": "u-42", "email": "pm@dalton.test"},
            "ingested_via": {"workspace_id": "ws-nvda", "thread_id": "th-99"},
            "dry_run": False,
        }
    )
    assert code == 200
    assert res["ok"] is True
    assert res["status"] == "ingested"
    assert res["doc_id"] == 1888
    assert res["document_id"] == "wiki:1888"
    assert res["request_id"] == "req-uuid-1888"

    # Verify argv list passed to create_subprocess_exec uses `--content @<temp_file>` (never raw content)
    mock_subproc.assert_called_once()
    call_args = list(mock_subproc.call_args.args)
    call_kwargs = mock_subproc.call_args.kwargs
    assert call_args[:10] == [
        str(bridge.cfg.wiki_run_sh),
        "--json",
        "add",
        "--company",
        "NVDA",
        "--type",
        "专家访谈",
        "--date",
        "2026-10-25",
        "--content",
    ]
    assert call_args[10].startswith("@")
    assert call_args[11:] == [
        "--source",
        "Expert Network",
        "--source-url",
        "https://example.com/call/1888",
    ]
    passed_env = call_kwargs["env"]
    assert passed_env.get("GEMINI_API_KEY") == "test_gemini_key_for_unit_test"
    assert "DALTON_BRIDGE_TOKEN" not in passed_env
    assert "DALTON_BRIDGE_ADMIN_TOKEN" not in passed_env

    # Verify audit JSONL was written with request_id and all required fields
    assert bridge.cfg.wiki_ingest_audit_file.is_file()
    audit_lines = (
        bridge.cfg.wiki_ingest_audit_file.read_text(encoding="utf-8").strip().splitlines()
    )
    assert len(audit_lines) == 1
    audit_entry = json.loads(audit_lines[0])
    assert audit_entry["request_id"] == "req-uuid-1888"
    assert audit_entry["ingested_by"] == {"user_id": "u-42", "email": "pm@dalton.test"}
    assert audit_entry["ingested_via"] == {"workspace_id": "ws-nvda", "thread_id": "th-99"}
    assert audit_entry["target"] == "NVDA"
    assert audit_entry["content_type"] == "专家访谈"
    assert audit_entry["date"] == "2026-10-25"
    assert audit_entry["doc_id"] == 1888
    assert audit_entry["filepath"] == "wiki/companies/NVDA/2026-10-25-专家访谈.md"
    assert audit_entry["result"] == "ok"

    # Round 2 Fix 6: Repeat request with the SAME request_id returns previous result (not duplicate)
    mock_subproc.reset_mock()
    code_replay, res_replay = await bridge.handle_wiki_ingest(
        {
            "request_id": "req-uuid-1888",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "专家访谈",
            "date": "2026-10-25",
            "content": body_text,
            "ingested_by": {"user_id": "u-42", "email": "pm@dalton.test"},
            "ingested_via": {"workspace_id": "ws-nvda", "thread_id": "th-99"},
            "dry_run": False,
        }
    )
    assert code_replay == 200
    assert res_replay["ok"] is True
    assert res_replay["idempotent_replay"] is True
    assert res_replay["doc_id"] == 1888
    mock_subproc.assert_not_called()

    # Duplicate check 1: exact sha256 match with a different request_id -> returns duplicate
    code_dup1, res_dup1 = await bridge.handle_wiki_ingest(
        {
            "request_id": "req-uuid-different",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "专家访谈",
            "date": "2026-10-26",
            "content": body_text,
            "ingested_by": {"user_id": "u-42", "email": "pm@dalton.test"},
            "ingested_via": {"workspace_id": "ws-nvda", "thread_id": "th-99"},
            "dry_run": False,
        }
    )
    assert code_dup1 == 400
    assert res_dup1["ok"] is False
    assert res_dup1["status"] == "duplicate"
    assert res_dup1["duplicate"] is True
    assert res_dup1["duplicate_reason"] == "sha256_match"

    # Duplicate check 2: same slug + date + type and highly similar body -> returns duplicate
    slightly_edited = body_text + "（补充备注）"
    code_dup2, res_dup2 = await bridge.handle_wiki_ingest(
        {
            "target_type": "company",
            "target": "NVDA",
            "content_type": "专家访谈",
            "date": "2026-10-25",
            "content": slightly_edited,
            "ingested_by": {"user_id": "u-42", "email": "pm@dalton.test"},
            "ingested_via": {"workspace_id": "ws-nvda", "thread_id": "th-99"},
            "dry_run": False,
        }
    )
    assert code_dup2 == 400
    assert res_dup2["ok"] is False
    assert res_dup2["status"] == "duplicate"
    assert res_dup2["duplicate"] is True
    assert res_dup2["duplicate_reason"] == "similar_content"

    # File upload execution (`add-file`): verify temp file is passed and cleaned up afterwards
    captured_file_arg: list[Path] = []
    file_existed_during_call: list[bool] = []

    async def _on_subproc(*args: Any, **kwargs: Any) -> Any:
        argv = list(args)
        if "--file" in argv:
            idx = argv.index("--file")
            f_path = Path(argv[idx + 1])
            captured_file_arg.append(f_path)
            file_existed_during_call.append(f_path.is_file())
        return fake_proc

    mock_subproc.reset_mock()
    mock_subproc.side_effect = _on_subproc

    pdf_bytes = b"%PDF-1.4 mock binary content for NDR takeaways"
    code_file, res_file = await bridge.handle_wiki_ingest(
        {
            "target_type": "company",
            "target": "NVDA",
            "content_type": "NDR纪要",
            "date": "2026-10-25",
            "file": {
                "filename": "nvda_ndr.pdf",
                "content_b64": base64.b64encode(pdf_bytes).decode("ascii"),
            },
            "ingested_by": {"user_id": "u-42", "email": "pm@dalton.test"},
            "ingested_via": {"workspace_id": "ws-nvda", "thread_id": "th-99"},
            "dry_run": False,
        }
    )
    assert code_file == 200
    assert res_file["ok"] is True
    assert len(captured_file_arg) == 1
    assert file_existed_during_call == [True]
    assert captured_file_arg[0].name == "nvda_ndr.pdf"
    assert not captured_file_arg[0].exists()


@pytest.mark.asyncio
async def test_bridge_wiki_ingest_concurrent_duplicate_claims_only_one_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 2 Fix 5: Two concurrent identical requests must result in one `ingested` and one `duplicate`."""
    bridge = _make_test_bridge(tmp_path)

    async def _slow_communicate() -> tuple[bytes, bytes]:
        await asyncio.sleep(0.05)
        return (
            json.dumps(
                {
                    "ok": True,
                    "action": "added",
                    "doc_id": 3001,
                    "filepath": "wiki/companies/NVDA/2026-10-25-专家访谈.md",
                }
            ).encode("utf-8"),
            b"",
        )

    fake_proc = create_autospec(asyncio.subprocess.Process, instance=True)
    fake_proc.returncode = 0
    fake_proc.communicate.side_effect = _slow_communicate

    mock_subproc = create_autospec(asyncio.create_subprocess_exec, return_value=fake_proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subproc)

    req_a = {
        "request_id": "req-concurrent-a",
        "target_type": "company",
        "target": "NVDA",
        "content_type": "专家访谈",
        "date": "2026-10-25",
        "content": "Concurrent duplicate test body on NVDA rubin architecture.",
        "ingested_by": {"user_id": "u-1", "email": "u1@dalton.test"},
        "ingested_via": {"workspace_id": "ws-nvda", "thread_id": "th-1"},
        "dry_run": False,
    }
    req_b = {**req_a, "request_id": "req-concurrent-b"}

    (code_a, res_a), (code_b, res_b) = await asyncio.gather(
        bridge.handle_wiki_ingest(req_a),
        bridge.handle_wiki_ingest(req_b),
    )

    statuses = sorted([res_a["status"], res_b["status"]])
    assert statuses == ["duplicate", "ingested"]
    assert sorted([code_a, code_b]) == [200, 400]
    mock_subproc.assert_called_once()


@pytest.mark.asyncio
async def test_bridge_wiki_ingest_audit_write_failure_logs_warning_but_returns_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Round 2 Fix 6: If audit log write fails after CLI succeeds, log warning and still return 200."""
    bridge = _make_test_bridge(tmp_path)

    fake_proc = create_autospec(asyncio.subprocess.Process, instance=True)
    fake_proc.returncode = 0
    fake_proc.communicate.return_value = (
        json.dumps(
            {
                "ok": True,
                "action": "added",
                "doc_id": 3002,
                "filepath": "wiki/companies/NVDA/2026-10-25-研究笔记.md",
            }
        ).encode("utf-8"),
        b"",
    )
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        create_autospec(asyncio.create_subprocess_exec, return_value=fake_proc),
    )
    monkeypatch.setattr(
        bridge,
        "_append_wiki_audit_entry",
        create_autospec(bridge._append_wiki_audit_entry, side_effect=OSError("disk full")),
    )

    with caplog.at_level(logging.WARNING):
        code, res = await bridge.handle_wiki_ingest(
            {
                "request_id": "req-audit-fail-1",
                "target_type": "company",
                "target": "NVDA",
                "content_type": "研究笔记",
                "date": "2026-10-25",
                "content": "Valid note where audit append fails.",
                "ingested_by": {"user_id": "u-1", "email": "u1@dalton.test"},
                "ingested_via": {"workspace_id": "ws-nvda", "thread_id": "th-1"},
                "dry_run": False,
            }
        )
    assert code == 200
    assert res["ok"] is True
    assert res["status"] == "ingested"
    assert res["doc_id"] == 3002
    assert any("Failed writing wiki ingest audit entry" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_bridge_wiki_ingest_cancellation_kills_reaps_cleans_temp_and_logs_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 2 Fix 7: CancelledError kills and reaps CLI subprocess, cleans temp files, writes audit, and re-raises."""
    bridge = _make_test_bridge(tmp_path)
    started_event = asyncio.Event()
    captured_tmp_files: list[Path] = []

    async def _hang_communicate() -> tuple[bytes, bytes]:
        started_event.set()
        await asyncio.sleep(60)
        return b"", b""

    fake_proc = create_autospec(asyncio.subprocess.Process, instance=True)
    fake_proc.returncode = -9
    fake_proc.communicate.side_effect = _hang_communicate
    fake_proc.wait.return_value = -9

    async def _on_subproc(*args: Any, **kwargs: Any) -> Any:
        argv = list(args)
        if "--content" in argv:
            c_arg = argv[argv.index("--content") + 1]
            if c_arg.startswith("@"):
                captured_tmp_files.append(Path(c_arg[1:]))
        return fake_proc

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        create_autospec(asyncio.create_subprocess_exec, side_effect=_on_subproc),
    )

    task = asyncio.create_task(
        bridge.handle_wiki_ingest(
            {
                "request_id": "req-cancel-1",
                "target_type": "company",
                "target": "NVDA",
                "content_type": "研究笔记",
                "date": "2026-10-25",
                "content": "Note that gets cancelled mid-flight.",
                "ingested_by": {"user_id": "u-1", "email": "u1@dalton.test"},
                "ingested_via": {"workspace_id": "ws-nvda", "thread_id": "th-1"},
                "dry_run": False,
            }
        )
    )
    await started_event.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    fake_proc.kill.assert_called_once()
    fake_proc.wait.assert_called_once()
    assert len(captured_tmp_files) == 1
    assert not captured_tmp_files[0].exists()

    audit_entries = bridge._read_wiki_audit_entries()
    assert len(audit_entries) == 1
    assert audit_entries[0]["status"] == "cancelled"
    assert audit_entries[0]["request_id"] == "req-cancel-1"


# ---------------------------------------------------------------------------
# 5. Dalton Tool (`ingest_company_wiki`) Tests
# ---------------------------------------------------------------------------


def test_ingest_company_wiki_bound_to_both_chief_of_staff_and_analyst() -> None:
    cos_tools = {t.name for t in build_dalton_tools("chief_of_staff")}
    analyst_tools = {t.name for t in build_dalton_tools("analyst")}
    assert "ingest_company_wiki" in cos_tools
    assert "ingest_company_wiki" in analyst_tools


@pytest.mark.asyncio
async def test_ingest_company_wiki_two_step_flow_pending_to_done_and_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_verify_access = create_autospec(
        wiki_tools.verify_workspace_access, return_value={"workspace_id": "ws-nvda"}
    )
    mock_resolve_home = create_autospec(
        wiki_tools.resolve_caller_home_workspace_id, return_value="ws-home-u1"
    )
    mock_resolve_email = create_autospec(
        wiki_tools.resolve_user_email, return_value="alice@dalton.test"
    )
    mock_create_pending = create_autospec(
        db_wiki_ingests.create_pending_wiki_ingest,
        return_value={"ingest_id": "req-fixed-uuid", "status": "pending"},
    )
    mock_update_status = create_autospec(
        db_wiki_ingests.update_wiki_ingest_status,
        return_value={"ingest_id": "req-fixed-uuid", "status": "done", "doc_id": "2048"},
    )
    mock_call_bridge = create_autospec(wiki_tools.call_bridge_wiki_ingest)

    monkeypatch.setattr(wiki_tools, "verify_workspace_access", mock_verify_access)
    monkeypatch.setattr(wiki_tools, "resolve_caller_home_workspace_id", mock_resolve_home)
    monkeypatch.setattr(wiki_tools, "resolve_user_email", mock_resolve_email)
    monkeypatch.setattr(wiki_tools, "create_pending_wiki_ingest", mock_create_pending)
    monkeypatch.setattr(wiki_tools, "update_wiki_ingest_status", mock_update_status)
    monkeypatch.setattr(wiki_tools, "call_bridge_wiki_ingest", mock_call_bridge)

    async def _eval_1(script: str, numkeys: int, *keys_and_args: Any) -> int:
        return 1

    mock_redis = create_autospec(aioredis.Redis, instance=True)
    mock_redis.eval.side_effect = _eval_1

    mock_cache = create_autospec(RedisCacheClient, instance=True)
    mock_cache.enabled = True
    mock_cache.client = mock_redis
    monkeypatch.setattr(wiki_tools, "get_cache_client", lambda: mock_cache)

    tool_fn = wiki_tools.build_ingest_company_wiki_tool(allow_home_workspace=True)
    run_config = {
        "configurable": {
            "user_id": "user-alice",
            "workspace_id": "home",
            "thread_id": "thread-cos-1",
        }
    }

    # Step 1: confirm=False -> dry_run=True preview, no DB write, no Redis rate-limit increment
    mock_call_bridge.return_value = (
        200,
        {
            "ok": True,
            "status": "dry_run",
            "dry_run": True,
            "target_type": "company",
            "target": "NVDA",
            "content_type": "专家访谈",
            "date": "2026-10-25",
            "filename": "2026-10-25-专家访谈.md",
            "filepath": "wiki/companies/NVDA/2026-10-25-专家访谈.md",
            "size_bytes": 120,
            "char_count": 60,
            "sha256": "abc123sha",
            "suspected_sell_side": False,
            "duplicate": False,
        },
    )
    preview_raw = await tool_fn.ainvoke(
        {
            "target_type": "company",
            "target": "NVDA",
            "content_type": "专家访谈",
            "date": "2026-10-25",
            "content": "Expert call notes on NVDA networking.",
            "confirm": False,
        },
        config=run_config,
    )
    preview = json.loads(preview_raw)
    assert preview["ok"] is True
    assert preview["status"] == "dry_run"
    assert preview["dry_run"] is True
    mock_create_pending.assert_not_called()
    mock_update_status.assert_not_called()
    mock_redis.eval.assert_not_called()

    sent_preview_req = mock_call_bridge.call_args.args[0]
    assert sent_preview_req["dry_run"] is True
    assert "request_id" not in sent_preview_req
    assert sent_preview_req["ingested_by"] == {
        "user_id": "user-alice",
        "email": "alice@dalton.test",
    }
    assert sent_preview_req["ingested_via"] == {
        "workspace_id": "ws-home-u1",
        "thread_id": "thread-cos-1",
    }

    # Step 2: confirm=True -> creates `pending` row first, passes stable `request_id` to bridge, updates row to `done`
    mock_call_bridge.return_value = (
        200,
        {
            "ok": True,
            "status": "ingested",
            "dry_run": False,
            "doc_id": 2048,
            "document_id": "wiki:2048",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "专家访谈",
            "date": "2026-10-25",
            "filename": "2026-10-25-专家访谈.md",
            "filepath": "wiki/companies/NVDA/2026-10-25-专家访谈.md",
            "sha256": "abc123sha",
            "tags": ["#NVDA"],
        },
    )
    ingest_raw = await tool_fn.ainvoke(
        {
            "target_type": "company",
            "target": "NVDA",
            "content_type": "专家访谈",
            "date": "2026-10-25",
            "content": "Expert call notes on NVDA networking.",
            "confirm": True,
        },
        config=run_config,
    )
    ingest_res = json.loads(ingest_raw)
    assert ingest_res["ok"] is True
    assert ingest_res["status"] == "ingested"
    assert ingest_res["document_id"] == "wiki:2048"
    assert ingest_res["db_status"] == "done"

    mock_create_pending.assert_called_once()
    pending_kwargs = mock_create_pending.call_args.kwargs
    generated_req_id = pending_kwargs["request_id"]
    assert isinstance(generated_req_id, str) and len(generated_req_id) == 36

    sent_commit_req = mock_call_bridge.call_args.args[0]
    assert sent_commit_req["request_id"] == generated_req_id
    assert sent_commit_req["dry_run"] is False

    mock_update_status.assert_called_once_with(
        generated_req_id,
        status="done",
        doc_id=2048,
        filepath="wiki/companies/NVDA/2026-10-25-专家访谈.md",
        sha256="abc123sha",
        target_type="company",
        target="NVDA",
        content_type="专家访谈",
        doc_date="2026-10-25",
    )


@pytest.mark.asyncio
async def test_ingest_company_wiki_remote_sandbox_realpath_symlinks_shell_chars_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 2 Fix 2: Simulated remote sandbox (`workspace_dir` not on local host), shell chars, symlinks to sibling workspace, and check failures."""
    remote_ws_dir = "/remote/nonexistent_host_path/workspaces/nvda"
    assert not Path(remote_ws_dir).exists()

    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = remote_ws_dir
    backend.root_dir = remote_ws_dir

    monkeypatch.setattr(
        wiki_tools,
        "verify_workspace_access",
        create_autospec(wiki_tools.verify_workspace_access, return_value={"workspace_id": "ws-nvda"}),
    )
    mock_call_bridge = create_autospec(
        wiki_tools.call_bridge_wiki_ingest,
        return_value=(200, {"ok": True, "status": "dry_run", "dry_run": True}),
    )
    monkeypatch.setattr(wiki_tools, "call_bridge_wiki_ingest", mock_call_bridge)
    monkeypatch.setattr(
        wiki_tools,
        "resolve_user_email",
        create_autospec(wiki_tools.resolve_user_email, return_value="u@dalton.test"),
    )

    tool_fn = wiki_tools.build_ingest_company_wiki_tool(
        filesystem_backend=backend, allow_home_workspace=False
    )
    cfg = {"configurable": {"user_id": "u-1", "workspace_id": "ws-nvda", "thread_id": "th-1"}}

    # 1. Filenames with `;`, `$(...)`, backticks, quotes, traversal, or `.agents` -> rejected before any sandbox call
    for bad_path in (
        "notes/bad;rm -rf.md",
        "notes/$(id).md",
        "notes/`whoami`.md",
        "notes/'quote'.md",
        "../amd/secret.md",
        "/remote/nonexistent_host_path/workspaces/amd/note.md",
        ".agents/memory/x.md",
    ):
        res = await tool_fn.ainvoke(
            {
                "target_type": "company",
                "target": "NVDA",
                "content_type": "研究笔记",
                "date": "2026-10-25",
                "file_path": bad_path,
                "confirm": False,
            },
            config=cfg,
        )
        assert res.startswith("ERROR:"), f"Expected rejection for {bad_path!r}, got {res!r}"
    backend.start_code_job.assert_not_called()
    mock_call_bridge.assert_not_called()

    def _handle(cid: str = "bg-1") -> JobHandle:
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

    def _make_ended(exit_code: int, stdout_text: str) -> Ended:
        return Ended(
            _handle(),
            exit_code=0,
            output=json.dumps({"exit_code": exit_code, "stdout": stdout_text, "stderr": ""}),
        )

    # 2. Metadata check job failure (`Failed` outcome) -> fail closed (rejected)
    backend.start_code_job.side_effect = None
    backend.start_code_job.return_value = Failed(
        _handle("bg-fail"), reason="transport", message="sandbox error"
    )
    res_cmd_fail = await tool_fn.ainvoke(
        {
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研究笔记",
            "date": "2026-10-25",
            "file_path": "notes/call.md",
            "confirm": False,
        },
        config=cfg,
    )
    assert res_cmd_fail.startswith("ERROR:")
    backend.adownload_files.assert_not_called()

    # 2b. One-shot python itself exited non-zero -> fail closed
    backend.start_code_job.return_value = _make_ended(1, "")
    res_py_fail = await tool_fn.ainvoke(
        {
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研究笔记",
            "date": "2026-10-25",
            "file_path": "notes/call.md",
            "confirm": False,
        },
        config=cfg,
    )
    assert res_py_fail.startswith("ERROR:")
    backend.adownload_files.assert_not_called()

    # 2c. Job outlived its wait (Running) -> stopped, never read as success,
    # and no dangling job is left behind.
    backend.start_code_job.side_effect = None
    backend.start_code_job.return_value = Running(_handle("bg-stuck"))
    res_stuck = await tool_fn.ainvoke(
        {
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研究笔记",
            "date": "2026-10-25",
            "file_path": "notes/call.md",
            "confirm": False,
        },
        config=cfg,
    )
    assert res_stuck.startswith("ERROR:")
    backend.stop_job.assert_called_once()
    _, stop_kwargs = backend.stop_job.call_args
    assert stop_kwargs.get("discard") is True
    backend.adownload_files.assert_not_called()

    # 3. Symlink on intermediate directory or leaf file -> rejected
    backend.start_code_job.side_effect = [
        _make_ended(0, "directory\nsymbolic link\nregular file\n"),
    ]
    res_sym_dir = await tool_fn.ainvoke(
        {
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研究笔记",
            "date": "2026-10-25",
            "file_path": "notes/call.md",
            "confirm": False,
        },
        config=cfg,
    )
    assert res_sym_dir.startswith("ERROR:")
    assert "Symlink" in res_sym_dir
    backend.adownload_files.assert_not_called()

    # 4. Realpath resolving to a sibling workspace (`/remote/.../workspaces/amd/notes/call.md`) -> rejected
    backend.start_code_job.side_effect = [
        _make_ended(0, "directory\ndirectory\nregular file\n"),
        _make_ended(
            0,
            f"{remote_ws_dir}\n/remote/nonexistent_host_path/workspaces/amd/notes/call.md\n",
        ),
    ]
    res_sibling = await tool_fn.ainvoke(
        {
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研究笔记",
            "date": "2026-10-25",
            "file_path": "notes/call.md",
            "confirm": False,
        },
        config=cfg,
    )
    assert res_sibling.startswith("ERROR:")
    assert "escapes caller workspace" in res_sibling
    backend.adownload_files.assert_not_called()

    # 5. Valid remote sandbox file -> stat and realpath pass, and `adownload_files` is called with the resolved realpath
    expected_resolved = f"{remote_ws_dir}/notes/call.md"
    backend.start_code_job.side_effect = [
        _make_ended(0, "directory\ndirectory\nregular file\n"),
        _make_ended(0, f"{remote_ws_dir}\n{expected_resolved}\n"),
    ]
    backend.adownload_files.return_value = [
        FileDownloadResponse(path=expected_resolved, content=b"# Valid remote workspace note")
    ]
    res_ok = await tool_fn.ainvoke(
        {
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研究笔记",
            "date": "2026-10-25",
            "file_path": "notes/call.md",
            "confirm": False,
        },
        config=cfg,
    )
    assert json.loads(res_ok)["ok"] is True
    # Confirm check and read used the exact same resolved realpath
    backend.adownload_files.assert_called_once_with([expected_resolved])
    sent_file = mock_call_bridge.call_args.args[0]["file"]
    assert sent_file["filename"] == "call.md"
    assert base64.b64decode(sent_file["content_b64"]) == b"# Valid remote workspace note"


@pytest.mark.asyncio
async def test_ingest_company_wiki_redis_atomic_rate_limit_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 2 Fix 4 & 10 + Round 3 Fix 4: Atomic single-step Lua `eval` in Redis and fail-closed on `confirm=True` when Redis errors."""
    monkeypatch.setattr(
        wiki_tools,
        "verify_workspace_access",
        create_autospec(wiki_tools.verify_workspace_access, return_value={"workspace_id": "ws-nvda"}),
    )
    mock_call_bridge = create_autospec(
        wiki_tools.call_bridge_wiki_ingest,
        return_value=(200, {"ok": True, "status": "dry_run", "dry_run": True}),
    )
    monkeypatch.setattr(wiki_tools, "call_bridge_wiki_ingest", mock_call_bridge)
    monkeypatch.setattr(
        wiki_tools,
        "resolve_user_email",
        create_autospec(wiki_tools.resolve_user_email, return_value="u@dalton.test"),
    )

    eval_calls: list[tuple[str, int, tuple[Any, ...]]] = []
    mock_redis = create_autospec(aioredis.Redis, instance=True)

    async def _on_eval(script: str, numkeys: int, *keys_and_args: Any) -> int:
        eval_calls.append((script, numkeys, keys_and_args))
        return len(eval_calls)

    mock_redis.eval.side_effect = _on_eval

    mock_cache = create_autospec(RedisCacheClient, instance=True)
    mock_cache.enabled = True
    mock_cache.client = mock_redis
    monkeypatch.setattr(wiki_tools, "get_cache_client", lambda: mock_cache)

    # 1. First atomic increment (count=1) executes single Lua EVAL (INCR + EXPIRE in one step)
    allowed_1, count_1 = await wiki_tools.check_and_increment_wiki_ingest_rate_limit("u-atomic")
    assert allowed_1 is True
    assert count_1 == 1
    assert len(eval_calls) == 1
    assert "redis.call('INCR', KEYS[1])" in eval_calls[0][0]
    assert "redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))" in eval_calls[0][0]
    assert eval_calls[0][1] == 1
    assert eval_calls[0][2] == ("dalton:wiki_ingest:rate:u-atomic", 3600)

    # 2. Second atomic increment (count=2) also executes single Lua EVAL
    allowed_2, count_2 = await wiki_tools.check_and_increment_wiki_ingest_rate_limit("u-atomic")
    assert allowed_2 is True
    assert count_2 == 2
    assert len(eval_calls) == 2

    # 3. Over limit (count = 31) -> rejected with `rate_limited`
    async def _eval_over_limit(script: str, numkeys: int, *keys_and_args: Any) -> int:
        return wiki_tools.WIKI_INGEST_RATE_LIMIT_PER_HOUR + 1

    mock_redis.eval.side_effect = _eval_over_limit

    tool_fn = wiki_tools.build_ingest_company_wiki_tool(allow_home_workspace=False)
    cfg = {"configurable": {"user_id": "u-burst", "workspace_id": "ws-nvda", "thread_id": "th-1"}}

    res_burst = await tool_fn.ainvoke(
        {
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研究笔记",
            "date": "2026-10-25",
            "content": "Burst note.",
            "confirm": True,
        },
        config=cfg,
    )
    parsed_burst = json.loads(res_burst)
    assert parsed_burst["ok"] is False
    assert parsed_burst["status"] == "rate_limited"
    mock_call_bridge.assert_not_called()

    # 4. Redis failure / unavailable: `confirm=True` MUST fail closed, while `confirm=False` preview succeeds
    mock_redis.eval.side_effect = RuntimeError("Redis connection refused")

    res_preview_redis_down = await tool_fn.ainvoke(
        {
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研究笔记",
            "date": "2026-10-25",
            "content": "Preview while Redis is down.",
            "confirm": False,
        },
        config=cfg,
    )
    assert json.loads(res_preview_redis_down)["ok"] is True
    assert mock_call_bridge.call_count == 1

    mock_call_bridge.reset_mock()
    res_commit_redis_down = await tool_fn.ainvoke(
        {
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研究笔记",
            "date": "2026-10-25",
            "content": "Commit while Redis is down.",
            "confirm": True,
        },
        config=cfg,
    )
    parsed_down = json.loads(res_commit_redis_down)
    assert parsed_down["ok"] is False
    assert parsed_down["status"] == "rate_limit_unavailable"
    mock_call_bridge.assert_not_called()


@pytest.mark.asyncio
async def test_pending_and_update_wiki_ingest_db_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    mock_cur.fetchone.side_effect = [
        {
            "ingest_id": "00000000-0000-0000-0000-000000000001",
            "user_id": "u-1",
            "workspace_id": "ws-nvda",
            "thread_id": "th-1",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "专家访谈",
            "doc_date": "2026-10-25",
            "sha256": "deadbeef",
            "doc_id": None,
            "filepath": None,
            "status": "pending",
        },
        {
            "ingest_id": "00000000-0000-0000-0000-000000000001",
            "user_id": "u-1",
            "workspace_id": "ws-nvda",
            "thread_id": "th-1",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "专家访谈",
            "doc_date": "2026-10-25",
            "sha256": "deadbeef",
            "doc_id": "2048",
            "filepath": "wiki/companies/NVDA/2026-10-25-专家访谈.md",
            "status": "done",
        },
        # Third fetchone: UPDATE ... WHERE status IS DISTINCT FROM 'done' returns None when row is already 'done'
        None,
        # Fourth fetchone: fallback SELECT returns the existing 'done' row intact
        {
            "ingest_id": "00000000-0000-0000-0000-000000000001",
            "user_id": "u-1",
            "workspace_id": "ws-nvda",
            "thread_id": "th-1",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "专家访谈",
            "doc_date": "2026-10-25",
            "sha256": "deadbeef",
            "doc_id": "2048",
            "filepath": "wiki/companies/NVDA/2026-10-25-专家访谈.md",
            "status": "done",
        },
    ]

    @asynccontextmanager
    async def _cursor_ctx(*args: Any, **kwargs: Any):
        yield mock_cur

    mock_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    mock_conn.cursor.side_effect = _cursor_ctx

    @asynccontextmanager
    async def _conn_ctx():
        yield mock_conn

    monkeypatch.setattr(db_wiki_ingests, "get_db_connection", _conn_ctx)

    pending_row = await db_wiki_ingests.create_pending_wiki_ingest(
        request_id="00000000-0000-0000-0000-000000000001",
        user_id="u-1",
        workspace_id="ws-nvda",
        thread_id="th-1",
        target_type="company",
        target="NVDA",
        content_type="专家访谈",
        doc_date="2026-10-25",
        sha256="deadbeef",
    )
    assert pending_row["status"] == "pending"
    assert pending_row["doc_id"] is None

    done_row = await db_wiki_ingests.update_wiki_ingest_status(
        "00000000-0000-0000-0000-000000000001",
        status="done",
        doc_id=2048,
        filepath="wiki/companies/NVDA/2026-10-25-专家访谈.md",
    )
    assert done_row["status"] == "done"
    assert done_row["doc_id"] == "2048"

    # Round 3 Fix 1: Attempting `done -> failed` must be blocked by `WHERE status IS DISTINCT FROM 'done'`
    after_failed_attempt = await db_wiki_ingests.update_wiki_ingest_status(
        "00000000-0000-0000-0000-000000000001",
        status="failed",
    )
    assert after_failed_attempt["status"] == "done"
    assert after_failed_attempt["doc_id"] == "2048"

    assert mock_cur.execute.call_count == 4
    insert_sql, _ = mock_cur.execute.call_args_list[0].args
    update_sql, _ = mock_cur.execute.call_args_list[1].args
    assert "INSERT INTO dalton_wiki_ingests" in insert_sql
    assert "UPDATE dalton_wiki_ingests" in update_sql
    assert "status IS DISTINCT FROM 'done'" in update_sql


@pytest.mark.asyncio
async def test_concurrent_same_request_id_in_progress_keeps_pending_in_dalton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 Fix 1: When bridge reports same `request_id` is in-flight (`status='in_progress'`), Dalton keeps DB row `pending` instead of marking `failed`."""
    monkeypatch.setattr(
        wiki_tools,
        "verify_workspace_access",
        create_autospec(wiki_tools.verify_workspace_access, return_value={"workspace_id": "ws-nvda"}),
    )
    monkeypatch.setattr(
        wiki_tools,
        "resolve_user_email",
        create_autospec(wiki_tools.resolve_user_email, return_value="u@dalton.test"),
    )
    mock_create_pending = create_autospec(
        db_wiki_ingests.create_pending_wiki_ingest,
        return_value={"ingest_id": "req-same-1", "status": "pending"},
    )
    mock_update_status = create_autospec(db_wiki_ingests.update_wiki_ingest_status)
    mock_call_bridge = create_autospec(
        wiki_tools.call_bridge_wiki_ingest,
        return_value=(
            409,
            {
                "ok": False,
                "status": "in_progress",
                "error_code": "request_in_progress",
                "request_id": "req-same-1",
                "error": "同一请求正在处理，稍后重试",
            },
        ),
    )
    monkeypatch.setattr(wiki_tools, "create_pending_wiki_ingest", mock_create_pending)
    monkeypatch.setattr(wiki_tools, "update_wiki_ingest_status", mock_update_status)
    monkeypatch.setattr(wiki_tools, "call_bridge_wiki_ingest", mock_call_bridge)

    async def _eval_1(script: str, numkeys: int, *keys_and_args: Any) -> int:
        return 1

    mock_redis = create_autospec(aioredis.Redis, instance=True)
    mock_redis.eval.side_effect = _eval_1
    mock_cache = create_autospec(RedisCacheClient, instance=True)
    mock_cache.enabled = True
    mock_cache.client = mock_redis
    monkeypatch.setattr(wiki_tools, "get_cache_client", lambda: mock_cache)

    tool_fn = wiki_tools.build_ingest_company_wiki_tool(allow_home_workspace=False)
    raw = await tool_fn.ainvoke(
        {
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研究笔记",
            "date": "2026-10-25",
            "content": "Concurrent same request note.",
            "confirm": True,
        },
        config={"configurable": {"user_id": "u-1", "workspace_id": "ws-nvda", "thread_id": "th-1"}},
    )
    parsed = json.loads(raw)
    assert parsed["ok"] is False
    assert parsed["status"] == "in_progress"
    assert parsed["db_status"] == "pending"
    assert "同一请求正在处理，稍后重试" in parsed["error"]
    mock_update_status.assert_not_called()


@pytest.mark.asyncio
async def test_bridge_flock_claim_locks_two_holders_reacquire_and_release_on_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Fix 1: Kernel `fcntl.flock` claim locks with two independent fds on the same path, re-acquisition after holder closes fd, `request_id`-first ordering, and guaranteed release on any exception."""
    import fcntl as _fcntl
    import hashlib as _hashlib
    import os as _os

    bridge = _make_test_bridge(tmp_path)
    claims_dir = bridge.cfg.wiki_ingest_temp_dir / "claims"
    claims_dir.mkdir(parents=True, exist_ok=True)

    # 1. Two independent fds on the same lock path: first holder acquires, second cannot acquire until first closes fd
    req_id = "req-flock-holder-1"
    req_hash = _hashlib.sha256(req_id.encode("utf-8")).hexdigest()[:32]
    req_lock_path = claims_dir / f"req_{req_hash}.lock"

    external_fd = _os.open(str(req_lock_path), _os.O_RDWR | _os.O_CREAT, 0o600)
    try:
        _fcntl.flock(external_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        # Second holder attempting same `request_id` (even with different sha/slot and empty lock file)
        # must fail on `req_<hash>.lock` first and return `same_request_inflight`
        fds_blocked, err_blocked = bridge._try_acquire_wiki_claim_locks(
            content_sha256="sha_other",
            slot_key="slot_other",
            request_id=req_id,
        )
        assert fds_blocked is None
        assert err_blocked == "same_request_inflight"
    finally:
        # Holder closes fd -> kernel releases flock automatically
        _os.close(external_fd)

    # Lock file is never deleted, and second holder can now acquire immediately
    assert req_lock_path.is_file()
    fds_acquired, err_acquired = bridge._try_acquire_wiki_claim_locks(
        content_sha256="sha_holder",
        slot_key="slot_holder",
        request_id=req_id,
    )
    assert err_acquired is None
    assert fds_acquired is not None and len(fds_acquired) == 3

    # While `fds_acquired` is held:
    # - A different `request_id` with the same `content_sha256` acquires its own `req_` lock,
    #   fails on `sha256_`, releases its `req_` lock, and returns `concurrent_inflight`.
    fds_sha_conflict, err_sha_conflict = bridge._try_acquire_wiki_claim_locks(
        content_sha256="sha_holder",
        slot_key="slot_different",
        request_id="req-flock-holder-2",
    )
    assert fds_sha_conflict is None
    assert err_sha_conflict == "concurrent_inflight"

    # Release `fds_acquired` -> lock files remain on disk, and locks are unlocked & closed
    bridge._release_wiki_claim_locks(fds_acquired)
    assert (claims_dir / "sha256_sha_holder.lock").is_file()
    assert (claims_dir / "slot_slot_holder.lock").is_file()

    fds_reacquired, err_reacquired = bridge._try_acquire_wiki_claim_locks(
        content_sha256="sha_holder",
        slot_key="slot_holder",
        request_id="req-flock-holder-2",
    )
    assert err_reacquired is None
    assert fds_reacquired is not None and len(fds_reacquired) == 3
    bridge._release_wiki_claim_locks(fds_reacquired)

    # 2. Any exception during acquisition (e.g., `os.write` raising `OSError` on the 2nd lock)
    # must release all locks already acquired in that call so another caller can acquire them!
    real_os_write = _os.write
    write_call_count = 0

    def _failing_os_write(fd: int, data: bytes) -> int:
        nonlocal write_call_count
        write_call_count += 1
        if write_call_count == 2:
            raise OSError(28, "No space left on device")
        return real_os_write(fd, data)

    monkeypatch.setattr(
        _os,
        "write",
        create_autospec(_os.write, side_effect=_failing_os_write),
    )
    with pytest.raises(OSError, match="No space left on device"):
        bridge._try_acquire_wiki_claim_locks(
            content_sha256="diskfull_sha",
            slot_key="diskfull_slot",
            request_id="req-diskfull",
        )

    # Restore `os.write` and verify none of the locks from the failed call remain held
    monkeypatch.setattr(_os, "write", real_os_write)
    fds_after_err, err_after_err = bridge._try_acquire_wiki_claim_locks(
        content_sha256="diskfull_sha",
        slot_key="diskfull_slot",
        request_id="req-diskfull",
    )
    assert err_after_err is None
    assert fds_after_err is not None and len(fds_after_err) == 3
    bridge._release_wiki_claim_locks(fds_after_err)


@pytest.mark.asyncio
async def test_bridge_idempotency_store_mid_write_failure_and_corrupted_file_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Round 4 Fix 2: Mid-write failure leaves existing idempotency file intact; corrupted file logs error and is never overwritten on save."""
    import os as _os

    bridge = _make_test_bridge(tmp_path)
    idemp_path = bridge._wiki_idempotency_file()

    # 1. Save initial valid record
    initial_rec = {
        "request_id": "req-init-1",
        "doc_id": 5001,
        "filepath": "wiki/companies/NVDA/2026-10-25-研究笔记.md",
        "sha256": "sha_init_1",
        "result": "ok",
        "status": "ingested",
    }
    bridge._save_wiki_idempotent_result("req-init-1", initial_rec)
    assert idemp_path.is_file()
    original_bytes = idemp_path.read_bytes()
    assert bridge._read_wiki_idempotent_results()["req-init-1"]["doc_id"] == 5001

    # 2. Simulate failure mid-write (e.g. `os.fsync` raises `OSError("I/O error")`) -> old file must remain untouched
    def _failing_fsync(fd: int) -> None:
        raise OSError("Simulated fsync I/O failure")

    monkeypatch.setattr(
        _os,
        "fsync",
        create_autospec(_os.fsync, side_effect=_failing_fsync),
    )
    with pytest.raises(OSError, match="Simulated fsync I/O failure"):
        bridge._save_wiki_idempotent_result(
            "req-second-2",
            {"request_id": "req-second-2", "doc_id": 5002, "status": "ingested"},
        )

    assert idemp_path.read_bytes() == original_bytes
    assert list(idemp_path.parent.glob(".wiki_idemp_*.tmp")) == []

    # 3. Corrupted idempotency file:
    # - Read path logs ERROR and returns `{}` (treats as not found)
    # - Save path logs ERROR, refuses to save, and does NOT overwrite the corrupted file
    corrupted_bytes = b"{corrupted json -- leave for operator inspection"
    idemp_path.write_bytes(corrupted_bytes)

    with caplog.at_level(logging.ERROR):
        read_res = bridge._read_wiki_idempotent_results()
        assert read_res == {}
        assert any("Failed reading/parsing wiki idempotency store" in r.message for r in caplog.records)

        caplog.clear()
        bridge._save_wiki_idempotent_result(
            "req-should-not-overwrite",
            {"request_id": "req-should-not-overwrite", "doc_id": 5003, "status": "ingested"},
        )
        assert any("Refusing to overwrite corrupted wiki idempotency store" in r.message for r in caplog.records)

    assert idemp_path.read_bytes() == corrupted_bytes


@pytest.mark.asyncio
async def test_bridge_idempotency_persisted_before_audit_and_binary_pdf_dedup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 Fix 3 & Round 4 Fix 3: CLI success + audit failure persists idempotency result so same-`request_id` retry does not re-run CLI; binary PDF deduplicates via idempotency/audit sha256 without scanning the slug directory."""
    bridge = _make_test_bridge(tmp_path)
    ocw_root = bridge.cfg.ocw_root
    cli_calls = 0

    async def _fake_cli(cmd: list[str], *, env: dict[str, str], timeout_s: float) -> dict[str, Any]:
        nonlocal cli_calls
        cli_calls += 1
        return {
            "ok": True,
            "status": "added",
            "doc_id": 7000 + cli_calls,
            "filepath": "wiki/companies/NVDA/2026-10-25-研究笔记.md",
            "filename": "2026-10-25-研究笔记.md",
            "chunks": 1,
            "tags": ["#NVDA"],
        }

    monkeypatch.setattr(
        bridge,
        "_run_company_wiki_ingest_cli",
        create_autospec(bridge._run_company_wiki_ingest_cli, side_effect=_fake_cli),
    )

    real_append_audit = bridge._append_wiki_audit_entry
    audit_should_fail = True

    def _flaky_append_audit(record: dict[str, Any]) -> None:
        if audit_should_fail:
            raise OSError("Simulated audit disk write failure")
        real_append_audit(record)

    monkeypatch.setattr(
        bridge,
        "_append_wiki_audit_entry",
        create_autospec(bridge._append_wiki_audit_entry, side_effect=_flaky_append_audit),
    )

    req_body = {
        "request_id": "req-idemp-audit-fail-1",
        "target_type": "company",
        "target": "NVDA",
        "content_type": "研究笔记",
        "date": "2026-10-25",
        "content": "Idempotency store survives audit log write failure.",
        "ingested_by": {"user_id": "u-1", "email": "u1@dalton.test"},
        "ingested_via": {"workspace_id": "ws-nvda", "thread_id": "th-1"},
        "dry_run": False,
    }

    # Attempt 1: CLI succeeds, idempotency store succeeds, audit log write raises OSError (logged as warning)
    code_1, resp_1 = await bridge.handle_wiki_ingest(req_body)
    assert code_1 == 200
    assert resp_1["ok"] is True
    assert resp_1["doc_id"] == 7001
    assert cli_calls == 1
    assert bridge._read_wiki_audit_entries() == []
    assert "req-idemp-audit-fail-1" in bridge._read_wiki_idempotent_results()

    # Attempt 2 (retry with same `request_id` after audit log recovers):
    # Must read from idempotency store, NOT re-run CLI (`cli_calls` stays 1), and backfill the audit log!
    audit_should_fail = False
    code_2, resp_2 = await bridge.handle_wiki_ingest(req_body)
    assert code_2 == 200
    assert resp_2["ok"] is True
    assert resp_2["idempotent_replay"] is True
    assert resp_2["doc_id"] == 7001
    assert cli_calls == 1
    audit_after_retry = bridge._read_wiki_audit_entries()
    assert len(audit_after_retry) == 1
    assert audit_after_retry[0]["request_id"] == "req-idemp-audit-fail-1"
    assert audit_after_retry[0]["doc_id"] == 7001

    # Binary PDF/image without extracted text (`norm_text == ""`):
    # Deduplication uses sha256 in idempotency store / audit log (and only checks the single target file on disk, never rglobbing the directory)
    binary_pdf_bytes = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Filter /FlateDecode >>\nstream\n\x9c\x01\x02\x03\x04\nendstream\n%%EOF"
    unrelated_disk_pdf = ocw_root / "wiki" / "companies" / "NVDA" / "2020-01-01-unrelated.pdf"
    unrelated_disk_pdf.parent.mkdir(parents=True, exist_ok=True)
    unrelated_disk_pdf.write_bytes(binary_pdf_bytes)

    # First ingest of `binary_pdf_bytes` succeeds (unrelated file in directory is NOT scanned)
    code_pdf_1, resp_pdf_1 = await bridge.handle_wiki_ingest(
        {
            "request_id": "req-binary-pdf-1",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研究笔记",
            "date": "2026-10-26",
            "file": {
                "filename": "binary.pdf",
                "content_b64": base64.b64encode(binary_pdf_bytes).decode("ascii"),
            },
            "ingested_by": {"user_id": "u-1", "email": "u1@dalton.test"},
            "ingested_via": {"workspace_id": "ws-nvda", "thread_id": "th-1"},
            "dry_run": False,
        }
    )
    assert code_pdf_1 == 200
    assert resp_pdf_1["ok"] is True
    assert cli_calls == 2

    # Second ingest of the same binary PDF with a new `request_id` is rejected via idempotency/audit sha256 match
    code_pdf_dup, resp_pdf_dup = await bridge.handle_wiki_ingest(
        {
            "request_id": "req-binary-pdf-dup",
            "target_type": "company",
            "target": "NVDA",
            "content_type": "研究笔记",
            "date": "2026-10-27",
            "file": {
                "filename": "binary.pdf",
                "content_b64": base64.b64encode(binary_pdf_bytes).decode("ascii"),
            },
            "ingested_by": {"user_id": "u-1", "email": "u1@dalton.test"},
            "ingested_via": {"workspace_id": "ws-nvda", "thread_id": "th-1"},
            "dry_run": False,
        }
    )
    assert code_pdf_dup == 400
    assert resp_pdf_dup["ok"] is False
    assert resp_pdf_dup["status"] == "duplicate"
    assert resp_pdf_dup["duplicate_reason"] == "sha256_match"
    assert cli_calls == 2


@pytest.mark.asyncio
async def test_bridge_wiki_ingest_env_build_failure_leaves_no_claim_lock_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The subprocess env is built before any claim lock: if building it raises,
    the same request can claim its locks again instead of being stuck behind a
    lock no finally will release."""
    bridge = _make_test_bridge(tmp_path)
    monkeypatch.setattr(
        bridge,
        "_build_wiki_subprocess_env",
        create_autospec(bridge._build_wiki_subprocess_env, side_effect=PermissionError("env dir")),
    )
    req = {
        "request_id": "req-env-fail-1",
        "target_type": "company",
        "target": "NVDA",
        "content_type": "研究笔记",
        "date": "2026-10-25",
        "content": "Note whose ingest fails while preparing the CLI env.",
        "ingested_by": {"user_id": "u-1", "email": "u1@dalton.test"},
        "ingested_via": {"workspace_id": "ws-nvda", "thread_id": "th-1"},
        "dry_run": False,
    }
    with pytest.raises(PermissionError):
        await bridge.handle_wiki_ingest(req)

    fds, err = bridge._try_acquire_wiki_claim_locks(
        content_sha256=hashlib.sha256(b"probe").hexdigest(),
        slot_key=hashlib.sha256(b"probe-slot").hexdigest(),
        request_id="req-env-fail-1",
    )
    assert err is None
    assert fds is not None
    bridge._release_wiki_claim_locks(fds)
