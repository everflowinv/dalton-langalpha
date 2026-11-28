"""Unit tests for legacy `researchdata_bridge` transparent forwarding to `dalton-research-host` (:8951)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import httpx
import pytest

from src.dalton.capabilities import CapabilityId
from src.dalton.profile import DeploymentProfile
from src.dalton.research_host.app import ResearchHostConfig, ResearchHostRuntime, create_app
from src.dalton.research_host.connector_registry import ConnectorRegistry
from src.dalton.research_host.federated import FederatedResearchConnector
from src.dalton.research_host.wiki_store import OpenWikiStore
from src.dalton.sandbox_caller_identity import sign_caller_token

REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_PATH = REPO_ROOT / "scripts" / "dalton" / "researchdata_bridge.py"


def _load_bridge_module() -> Any:
    spec = importlib.util.spec_from_file_location("_test_bridge_rh_fwd", BRIDGE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(pytest.MonkeyPatch.__dict__):  # no-op guard
        _ = key
    for key in (
        "APP_ENV",
        "DALTON_PROFILE",
        "DALTON_HOST_PLATFORM",
        "DALTON_ENABLED",
        "DALTON_RESEARCH_HOST_URL",
        "DALTON_RESEARCH_HOST_BASE_URL",
        "DALTON_RESEARCH_HOST_TOKEN",
        "DALTON_RESEARCH_ADMIN_TOKEN",
        "DALTON_BRIDGE_TOKEN",
        "DALTON_BRIDGE_ADMIN_TOKEN",
        "DALTON_BRIDGE_ENV_FILE",
        "DALTON_DOTENV_PATH",
        "DALTON_REPO_ENV_FILE",
        "DALTON_CAP_SALES_NOTES",
        "DALTON_CAP_GUIDEPOINT",
        "DALTON_CAP_COMPANY_WIKI",
        "DALTON_CAP_TMTB",
        "DALTON_CAP_FUNDA_AI",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "0")


def _make_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    repo_env_lines: list[str],
    bridge_env_lines: list[str] | None = None,
) -> tuple[Any, Any]:
    _clean_env(monkeypatch)
    bridge_mod = _load_bridge_module()
    repo_env = tmp_path / "repo.env"
    bridge_env = tmp_path / "researchdata-bridge.env"
    repo_env.write_text("\n".join(repo_env_lines) + "\n", encoding="utf-8")
    bridge_env.write_text(
        "\n".join(
            bridge_env_lines
            if bridge_env_lines is not None
            else [
                "DALTON_BRIDGE_TOKEN=br-mcp-tok",
                "DALTON_BRIDGE_ADMIN_TOKEN=br-adm-tok",
                "DALTON_RESEARCH_HOST_TOKEN=rh-mcp-tok",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DALTON_DOTENV_PATH", str(repo_env))
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(bridge_env))
    cfg = bridge_mod.BridgeConfig()
    bridge = bridge_mod.ResearchDataBridge(cfg)
    return bridge_mod, bridge


def _mcp_ok_response(payload: dict[str, Any], *, req_id: int = 1) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "structuredContent": payload,
                "isError": False,
            },
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "operation", "extra_args", "expected_connector", "expected_tool"),
    [
        ("guidepoint", "search_library", {"query": "NVDA rack demand", "limit": 10}, "guidepoint", "search_library"),
        ("company-wiki", "search", {"query": "META", "params": {"company": "META"}}, "company-wiki", "search"),
        ("company-wiki", "read", {"document": "wiki:42"}, "company-wiki", "read"),
        ("company-wiki", "topic", {"query": "AI"}, "company-wiki", "topic"),
        ("sales-notes", "search", {"query": "TSMC CoWoS", "limit": 5}, "sales-notes", "search"),
        ("tmtb", "read", {"document": "tmtb:99", "params": {"offset": 0, "max_chars": 8000}}, "tmtb", "read"),
        ("funda-ai", "list", {"limit": 15}, "funda-ai", "list"),
        (
            "all",
            "search",
            {"query": "AI capex", "limit": 10, "params": {"keywords": ["NVDA"], "fresh": True}},
            "federated",
            "search",
        ),
        ("all", "read", {"document": "wiki:7"}, "federated", "read"),
        ("sales-mail", "search", {"query": "bogey", "limit": 8}, "federated", "sales_mail"),
    ],
)
async def test_migrated_routes_forward_to_research_host_with_caller_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    operation: str,
    extra_args: dict[str, Any],
    expected_connector: str,
    expected_tool: str,
) -> None:
    """Each migrated route forwards to `/mcp/<connector>` with host + caller tokens and preserves response shape."""
    _mod, bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        repo_env_lines=[
            "APP_ENV=dalton",
            "DALTON_PROFILE=everflow",
            "DALTON_HOST_PLATFORM=macos",
            "DALTON_RESEARCH_HOST_URL=http://host.docker.internal:8951",
            "DALTON_RESEARCH_HOST_TOKEN=rh-mcp-tok",
            "DALTON_RESEARCH_ADMIN_TOKEN=rh-adm-tok",
        ],
    )
    mock_http = create_autospec(httpx.AsyncClient, instance=True, spec_set=True)
    upstream_sources = [
        {
            "document_id": f"{expected_connector}:1",
            "identifier": f"{expected_connector}:1",
            "route": route,
            "source_type": "research_doc",
            "identity": "expert" if route == "guidepoint" else "internal_note",
            "publisher": "ResearchHost",
            "title": "Sample Hit",
            "as_of": "2026-11-28",
        }
    ]
    if route == "guidepoint":
        upstream_payload: dict[str, Any] = {
            "ok": True,
            "status": "ok",
            "data": {
                "structuredContent": {
                    "data": [{"id": "library_ts:gp-1", "transcript_name": "NVDA Rack Demand"}]
                }
            },
            "results": [{"id": "library_ts:gp-1", "transcript_name": "NVDA Rack Demand"}],
            "count": 1,
            "_sources": upstream_sources,
        }
    elif operation == "read":
        upstream_payload = {
            "ok": True,
            "status": "ok",
            "document": {
                "document_id": extra_args["document"],
                "source": route,
                "title": "Sample Doc",
                "content": "Full body text.",
            },
            "warnings": [],
            "_sources": upstream_sources,
        }
    else:
        upstream_payload = {
            "ok": True,
            "status": "ok",
            "results": [{"document_id": f"{expected_connector}:1", "title": "Sample Hit"}],
            "result_count": 1,
            "_sources": upstream_sources,
        }
    mock_http.post.return_value = _mcp_ok_response(upstream_payload, req_id=17)
    bridge._research_host_http = mock_http

    stdio_spy = create_autospec(bridge._run_stdio_rpc, spec_set=True)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", stdio_spy)

    caller_tok = sign_caller_token(
        {"user": "alice", "workspace": "ws-1", "thread": "th-1"},
        admin_token="br-adm-tok",
    )
    assert caller_tok is not None
    reply = await bridge.handle_tools_call(
        17,
        {"name": "research", "arguments": {"route": route, "operation": operation, **extra_args}},
        raw_caller_token=caller_tok,
    )

    assert stdio_spy.await_count == 0
    assert mock_http.post.await_count == 1
    call = mock_http.post.call_args
    posted_url = call.args[0]
    assert posted_url == f"http://127.0.0.1:8951/mcp/{expected_connector}"
    sent_headers = call.kwargs["headers"]
    assert sent_headers["Authorization"] == "Bearer rh-mcp-tok"
    assert sent_headers["X-Dalton-Caller-Scope"] == "per-request"
    assert sent_headers["X-Dalton-Caller-Token"] == caller_tok
    assert int(sent_headers["X-Dalton-Request-Timeout-Ms"]) > 0

    sent_rpc = call.kwargs["json"]
    assert sent_rpc["method"] == "tools/call"
    assert sent_rpc["params"]["name"] == expected_tool
    sent_args = sent_rpc["params"]["arguments"]
    assert "route" not in sent_args
    assert "mode" not in sent_args
    if route == "sales-mail":
        assert sent_args["operation"] == operation
    else:
        assert "operation" not in sent_args
    if route == "all" and operation == "search":
        # Federated must keep Guidepoint/AlphaEngine nested params (not stripped by legacy local-all filter)
        assert sent_args["params"]["keywords"] == ["NVDA"]
        assert sent_args["params"]["fresh"] is True

    assert reply["id"] == 17
    assert reply["result"]["isError"] is False
    body = json.loads(reply["result"]["content"][0]["text"])
    assert body["ok"] is True
    assert body["executed"] is True
    assert "results" not in body
    assert "document" not in body
    if route == "guidepoint":
        assert body["data"]["structuredContent"]["data"] == upstream_payload["data"]["structuredContent"]["data"]
    elif operation == "read":
        assert body["data"]["document"] == upstream_payload["document"]
    else:
        assert body["data"]["results"] == upstream_payload["results"]
    if route == "sales-mail":
        assert body["migration"]["deprecated_route"] == "sales-mail"
    assert body["_sources"] == upstream_sources
    assert body["_declared_sources"] == upstream_sources
    assert "normalized_params" in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "expected_connector", "failure_kind"),
    [
        ("guidepoint", "guidepoint", "http_500"),
        ("company-wiki", "company-wiki", "http_502"),
        ("guidepoint", "guidepoint", "http_503_lock"),
        ("all", "federated", "timeout"),
        ("sales-notes", "sales-notes", "connect_error"),
        ("tmtb", "tmtb", "http_401"),
    ],
)
async def test_research_host_errors_return_clear_message_without_local_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    expected_connector: str,
    failure_kind: str,
) -> None:
    """5xx, timeout, connect error, and 401 return clear errors naming the connector and NEVER fall back locally."""
    _mod, bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        repo_env_lines=[
            "APP_ENV=dalton",
            "DALTON_PROFILE=everflow",
            "DALTON_RESEARCH_HOST_URL=http://127.0.0.1:8951",
            "DALTON_RESEARCH_HOST_TOKEN=rh-mcp-tok",
        ],
    )
    mock_http = create_autospec(httpx.AsyncClient, instance=True, spec_set=True)
    if failure_kind == "http_500":
        mock_http.post.return_value = httpx.Response(500, json={"ok": False, "error": "boom"})
    elif failure_kind == "http_502":
        mock_http.post.return_value = httpx.Response(502, text="Bad Gateway")
    elif failure_kind == "http_503_lock":
        mock_http.post.return_value = httpx.Response(
            503,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "ok": False,
                                    "status": "standby",
                                    "error_code": "lock_not_acquired",
                                    "error": "Guidepoint connector is in standby.",
                                }
                            ),
                        }
                    ],
                    "isError": True,
                },
            },
        )
    elif failure_kind == "timeout":
        mock_http.post.side_effect = httpx.ReadTimeout("read timed out")
    elif failure_kind == "connect_error":
        mock_http.post.side_effect = httpx.ConnectError("connection refused")
    elif failure_kind == "http_401":
        mock_http.post.return_value = httpx.Response(401, json={"ok": False, "error": "Invalid token"})
    bridge._research_host_http = mock_http

    stdio_spy = create_autospec(bridge._run_stdio_rpc, spec_set=True)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", stdio_spy)

    op = "search_library" if route == "guidepoint" else "search"
    reply = await bridge.handle_tools_call(
        1,
        {"name": "research", "arguments": {"route": route, "operation": op, "query": "NVDA"}},
    )

    assert stdio_spy.await_count == 0
    assert reply["result"]["isError"] is True
    body = json.loads(reply["result"]["content"][0]["text"])
    assert body["ok"] is False
    assert expected_connector in body["error"]
    assert body.get("connector") == expected_connector
    if failure_kind == "timeout":
        assert body["error_code"] == "research_host_timeout"
        assert body["status"] == "timeout"
    elif failure_kind == "connect_error":
        assert body["error_code"] == "research_host_unavailable"
    elif failure_kind == "http_503_lock":
        assert body["error_code"] == "lock_not_acquired"
        assert body["http_status"] == 503
    elif failure_kind == "http_401":
        assert body["error_code"] == "research_host_unauthorized"
    else:
        assert body["error_code"] == "research_host_http_error"


@pytest.mark.asyncio
async def test_pre_cutover_and_core_profile_do_not_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `DALTON_RESEARCH_HOST_URL` (pre-cutover) or in Core profile, forwarding stays inactive."""
    # 1. Pre-cutover Everflow (no DALTON_RESEARCH_HOST_URL) -> calls local stdio RPC, never HTTP
    pre_dir = tmp_path / "pre"
    pre_dir.mkdir()
    _mod, pre_bridge = _make_bridge(
        pre_dir,
        monkeypatch,
        repo_env_lines=[
            "APP_ENV=dalton",
            "DALTON_PROFILE=everflow",
            "DALTON_HOST_PLATFORM=macos",
        ],
    )
    mock_http = create_autospec(httpx.AsyncClient, instance=True, spec_set=True)
    pre_bridge._research_host_http = mock_http

    local_reply = {
        "jsonrpc": "2.0",
        "id": 9,
        "result": {
            "content": [{"type": "text", "text": json.dumps({"ok": True, "status": "ok", "results": [{"id": "local-1"}]})}],
            "isError": False,
        },
    }
    stdio_stub = create_autospec(pre_bridge._run_stdio_rpc, spec_set=True)
    stdio_stub.return_value = [local_reply]
    monkeypatch.setattr(pre_bridge, "_run_stdio_rpc", stdio_stub)

    resp = await pre_bridge.handle_tools_call(
        9,
        {"name": "research", "arguments": {"route": "company-wiki", "operation": "search", "query": "NVDA"}},
    )
    assert mock_http.post.await_count == 0
    assert stdio_stub.await_count == 1
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["ok"] is True
    assert body["results"] == [{"id": "local-1"}]

    # 2. Core profile -> never forwards even if DALTON_RESEARCH_HOST_URL is present
    (tmp_path / "core").mkdir(exist_ok=True)
    _mod2, core_bridge = _make_bridge(
        tmp_path / "core",
        monkeypatch,
        repo_env_lines=[
            "APP_ENV=core",
            "DALTON_PROFILE=core",
            "DALTON_RESEARCH_HOST_URL=http://research-host:8951",
        ],
    )
    assert core_bridge._is_research_host_forwarding_active() is False
    assert core_bridge._should_forward_route_to_research_host("research", "company-wiki") is False


@pytest.mark.asyncio
async def test_ingest_endpoints_forward_post_cutover_and_handle_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/wiki/ingest` and `/sales-notes/ingest` forward to `:8951/ingest/*` with admin token and never fall back."""
    _mod, bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        repo_env_lines=[
            "APP_ENV=dalton",
            "DALTON_PROFILE=everflow",
            "DALTON_RESEARCH_HOST_URL=http://host.docker.internal:8951",
            "DALTON_RESEARCH_HOST_TOKEN=rh-mcp-tok",
            "DALTON_RESEARCH_ADMIN_TOKEN=rh-adm-tok",
        ],
    )
    mock_http = create_autospec(httpx.AsyncClient, instance=True, spec_set=True)
    mock_http.post.side_effect = [
        httpx.Response(200, json={"ok": True, "status": "ingested", "doc_id": "wiki:9"}),
        httpx.Response(200, json={"ok": True, "status": "ingested", "doc_id": "mail:7"}),
        httpx.ReadTimeout("timed out"),
        httpx.Response(503, json={"ok": False, "status": "writes_paused", "error_code": "wiki_writes_paused", "error": "paused"}),
    ]
    bridge._research_host_http = mock_http

    local_wiki_spy = create_autospec(bridge._handle_wiki_ingest_unlocked, spec_set=True)
    local_sn_spy = create_autospec(bridge._handle_sales_notes_ingest_body, spec_set=True)
    monkeypatch.setattr(bridge, "_handle_wiki_ingest_unlocked", local_wiki_spy)
    monkeypatch.setattr(bridge, "_handle_sales_notes_ingest_body", local_sn_spy)

    wiki_req = {"target_type": "company", "target": "NVDA", "content_type": "note", "content": "hello"}
    w_code, w_body = await bridge.handle_wiki_ingest(wiki_req)
    assert w_code == 200 and w_body["doc_id"] == "wiki:9"
    c0 = mock_http.post.call_args_list[0]
    assert c0.args[0] == "http://127.0.0.1:8951/ingest/wiki"
    assert c0.kwargs["headers"]["Authorization"] == "Bearer rh-adm-tok"
    assert c0.kwargs["headers"]["X-Dalton-Research-Admin-Token"] == "rh-adm-tok"
    assert c0.kwargs["json"] == wiki_req

    sn_req = {"institution": "MS", "title": "NVDA Update", "content": "note body"}
    s_code, s_body = await bridge.handle_sales_notes_ingest(sn_req)
    assert s_code == 200 and s_body["doc_id"] == "mail:7"
    c1 = mock_http.post.call_args_list[1]
    assert c1.args[0] == "http://127.0.0.1:8951/ingest/sales-notes"
    assert c1.kwargs["headers"]["Authorization"] == "Bearer rh-adm-tok"
    assert c1.kwargs["json"] == sn_req

    t_code, t_body = await bridge.handle_sales_notes_ingest(sn_req)
    assert t_code == 504
    assert t_body["error_code"] == "research_host_timeout"
    assert "sales-notes" in t_body["error"]

    p_code, p_body = await bridge.handle_wiki_ingest(wiki_req)
    assert p_code == 503
    assert p_body["error_code"] == "wiki_writes_paused"
    assert "wiki" in p_body["error"]

    assert local_wiki_spy.await_count == 0
    assert local_sn_spy.await_count == 0


@pytest.mark.asyncio
async def test_tools_list_lists_migrated_routes_and_annotates_only_after_cutover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tools/list` keeps all migrated routes and adds the research-host note only when cutover is active."""
    raw_tools_reply = [
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "tools": [
                    {
                        "name": "research",
                        "description": "Search research sources.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "route": {
                                    "type": "string",
                                    "enum": [
                                        "all",
                                        "alphaengine",
                                        "guidepoint",
                                        "company-wiki",
                                        "sales-notes",
                                        "tmtb",
                                        "funda-ai",
                                    ],
                                }
                            },
                        },
                    }
                ]
            },
        }
    ]

    # Pre-cutover: no research-host note
    (tmp_path / "pre").mkdir()
    _mod, pre_bridge = _make_bridge(
        tmp_path / "pre",
        monkeypatch,
        repo_env_lines=["APP_ENV=dalton", "DALTON_PROFILE=everflow"],
    )
    init_stub = create_autospec(pre_bridge.ensure_initialized, spec_set=True)
    init_stub.return_value = {"serverInfo": {"version": "1.0"}}
    stdio_stub = create_autospec(pre_bridge._run_stdio_rpc, spec_set=True)
    stdio_stub.return_value = raw_tools_reply
    monkeypatch.setattr(pre_bridge, "ensure_initialized", init_stub)
    monkeypatch.setattr(pre_bridge, "_run_stdio_rpc", stdio_stub)

    pre_tools = await pre_bridge.get_filtered_tools()
    pre_research = next(t for t in pre_tools if t["name"] == "research")
    assert "已由研究宿主服务承载" not in pre_research["description"]

    # Post-cutover: keeps all routes in enum AND annotates description
    (tmp_path / "post").mkdir()
    _mod2, post_bridge = _make_bridge(
        tmp_path / "post",
        monkeypatch,
        repo_env_lines=[
            "APP_ENV=dalton",
            "DALTON_PROFILE=everflow",
            "DALTON_RESEARCH_HOST_URL=http://host.docker.internal:8951",
        ],
    )
    init_stub2 = create_autospec(post_bridge.ensure_initialized, spec_set=True)
    init_stub2.return_value = {"serverInfo": {"version": "1.0"}}
    stdio_stub2 = create_autospec(post_bridge._run_stdio_rpc, spec_set=True)
    stdio_stub2.return_value = raw_tools_reply
    monkeypatch.setattr(post_bridge, "ensure_initialized", init_stub2)
    monkeypatch.setattr(post_bridge, "_run_stdio_rpc", stdio_stub2)

    post_tools = await post_bridge.get_filtered_tools()
    post_research = next(t for t in post_tools if t["name"] == "research")
    enum_routes = set(post_research["inputSchema"]["properties"]["route"]["enum"])
    for expected_r in ("all", "guidepoint", "company-wiki", "sales-notes", "tmtb", "funda-ai"):
        assert expected_r in enum_routes
    assert "已由研究宿主服务承载" in post_research["description"]


@pytest.mark.asyncio
async def test_end_to_end_forward_against_real_research_host_asgi_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real `dalton-research-host` ASGI app accepts forwarded `/mcp/company-wiki`, `/mcp/federated`, and `/ingest/wiki`."""
    data_root = tmp_path / "research_data"
    (data_root / "wiki").mkdir(parents=True)
    _mod, bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        repo_env_lines=[
            "APP_ENV=dalton",
            "DALTON_PROFILE=everflow",
            "DALTON_HOST_PLATFORM=macos",
            "DALTON_RESEARCH_HOST_URL=http://127.0.0.1:8951",
            "DALTON_RESEARCH_HOST_TOKEN=rh-mcp-tok",
            "DALTON_RESEARCH_ADMIN_TOKEN=rh-adm-tok",
        ],
    )
    rt = ResearchHostRuntime(
        ResearchHostConfig(
            data_root=data_root,
            mcp_token="rh-mcp-tok",
            admin_token="rh-adm-tok",
            extra_caller_admin_secrets=("br-adm-tok",),
            profile=DeploymentProfile.EVERFLOW,
            environ={"APP_ENV": "dalton", "DALTON_PROFILE": "everflow"},
        )
    )
    app = create_app(runtime=rt)
    caller_tok = sign_caller_token(
        {"user": "richard", "workspace": "ws-main", "thread": "th-main"},
        admin_token="br-adm-tok",
    )
    assert caller_tok is not None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8951",
        ) as asgi_client:
            bridge._research_host_http = asgi_client

            # 1. Forwarded /wiki/ingest (dry_run preview) reaches real /ingest/wiki
            w_code, w_body = await bridge.handle_wiki_ingest(
                {
                    "target_type": "company",
                    "target": "NVDA",
                    "content_type": "note",
                    "date": "2026-11-28",
                    "title": "Forward Test",
                    "content": "# NVDA\n\nGPU demand note.",
                    "dry_run": True,
                }
            )
            assert w_code == 200
            assert w_body["ok"] is True

            # 2. Forwarded company-wiki list reaches real /mcp/company-wiki
            wiki_reply = await bridge.handle_tools_call(
                10,
                {"name": "research", "arguments": {"route": "company-wiki", "operation": "list", "limit": 5}},
                raw_caller_token=caller_tok,
            )
            assert wiki_reply["result"]["isError"] is False
            wiki_payload = json.loads(wiki_reply["result"]["content"][0]["text"])
            assert wiki_payload["ok"] is True

            # 3. Forwarded `all` reaches real /mcp/federated
            all_reply = await bridge.handle_tools_call(
                11,
                {"name": "research", "arguments": {"route": "all", "operation": "list", "limit": 5}},
                raw_caller_token=caller_tok,
            )
            assert all_reply["result"]["isError"] is False
            all_payload = json.loads(all_reply["result"]["content"][0]["text"])
            assert all_payload["ok"] is True
    finally:
        await rt.shutdown()


@pytest.mark.asyncio
async def test_all_route_callback_to_local_alphaengine_does_not_deadlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forwarding `route='all'` uses its own concurrency limiter so federated->bridge alphaengine callbacks never deadlock."""
    _mod, bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        repo_env_lines=[
            "APP_ENV=dalton",
            "DALTON_PROFILE=everflow",
            "DALTON_RESEARCH_HOST_URL=http://127.0.0.1:8951",
            "DALTON_RESEARCH_HOST_TOKEN=rh-mcp-tok",
        ],
    )
    # Tightest unsigned limit (=1): previously holding the same limiter during
    # `route='all'` forwarding would deadlock when `/mcp/federated` calls back
    # into `:8960` for `route='alphaengine'`.
    bridge.cfg.max_inflight_unsigned = 1

    ae_callback_ran = asyncio.Event()

    stdio_stub = create_autospec(bridge._run_stdio_rpc, spec_set=True)
    stdio_stub.return_value = [
        {
            "jsonrpc": "2.0",
            "id": 99,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "ok": True,
                                "status": "ok",
                                "data": {"structuredContent": {"results": [{"doc_id": "ae-1"}]}},
                            }
                        ),
                    }
                ],
                "isError": False,
            },
        }
    ]
    monkeypatch.setattr(bridge, "_run_stdio_rpc", stdio_stub)

    async def _federated_side_effect(*_args: Any, **_kwargs: Any) -> httpx.Response:
        # Simulate :8951/mcp/federated calling back into :8960 for alphaengine
        ae_resp = await asyncio.wait_for(
            bridge.handle_tools_call(
                99,
                {
                    "name": "research",
                    "arguments": {
                        "route": "alphaengine",
                        "operation": "search_library",
                        "query": "NVDA",
                        "params": {"fresh": True},
                    },
                },
            ),
            timeout=2.0,
        )
        assert ae_resp["result"]["isError"] is False
        ae_callback_ran.set()
        return _mcp_ok_response(
            {
                "ok": True,
                "status": "ok",
                "results": [],
                "alphaengine": {"ok": True, "status": "ok", "count": 1, "results": [{"doc_id": "ae-1"}]},
            },
            req_id=21,
        )

    mock_http = create_autospec(httpx.AsyncClient, instance=True, spec_set=True)
    mock_http.post.side_effect = _federated_side_effect
    bridge._research_host_http = mock_http

    reply = await asyncio.wait_for(
        bridge.handle_tools_call(
            21,
            {"name": "research", "arguments": {"route": "all", "operation": "search", "query": "NVDA"}},
        ),
        timeout=3.0,
    )
    assert ae_callback_ran.is_set()
    assert reply["result"]["isError"] is False
    payload = json.loads(reply["result"]["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["data"]["results"] == []
    assert "results" not in payload
    assert payload["alphaengine"]["count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "operation", "call_args", "legacy_inner", "rh_payload", "check_kind"),
    [
        (
            "company-wiki",
            "read",
            {"document": "wiki:42"},
            {
                "ok": True,
                "status": "ok",
                "document": {
                    "document_id": "wiki:42",
                    "source": "company-wiki",
                    "title": "NVDA Blackwell Note",
                    "content": "Blackwell GB200 rack yield notes.",
                },
                "warnings": [],
                "retrieved_at": "2026-11-28T00:00:00+00:00",
                "local_only": True,
            },
            {
                "ok": True,
                "status": "ok",
                "document": {
                    "document_id": "wiki:42",
                    "source": "company-wiki",
                    "title": "NVDA Blackwell Note",
                    "content": "Blackwell GB200 rack yield notes.",
                },
                "warnings": [],
                "retrieved_at": "2026-11-28T00:00:00+00:00",
                "local_only": True,
            },
            "document",
        ),
        (
            "sales-notes",
            "search",
            {"query": "TSMC CoWoS", "limit": 5},
            {
                "ok": True,
                "status": "ok",
                "results": [
                    {
                        "document_id": "mail:101",
                        "source": "sales-notes",
                        "title": "MS CoWoS Check",
                        "snippet": "CoWoS capacity up 40%.",
                    }
                ],
                "result_count": 1,
                "warnings": [],
                "retrieved_at": "2026-11-28T00:00:00+00:00",
                "local_only": True,
            },
            {
                "ok": True,
                "status": "ok",
                "results": [
                    {
                        "document_id": "mail:101",
                        "source": "sales-notes",
                        "title": "MS CoWoS Check",
                        "snippet": "CoWoS capacity up 40%.",
                    }
                ],
                "result_count": 1,
                "warnings": [],
                "retrieved_at": "2026-11-28T00:00:00+00:00",
                "local_only": True,
            },
            "results",
        ),
        (
            "tmtb",
            "read",
            {"document": "tmtb:9901"},
            {
                "ok": True,
                "status": "ok",
                "document": {
                    "document_id": "tmtb:9901",
                    "source": "tmtb",
                    "title": "TMTB Semicap Note",
                    "content": "Wafer fab equipment backlog.",
                },
                "warnings": [],
                "retrieved_at": "2026-11-28T00:00:00+00:00",
                "local_only": True,
            },
            {
                "ok": True,
                "status": "ok",
                "document": {
                    "document_id": "tmtb:9901",
                    "source": "tmtb",
                    "title": "TMTB Semicap Note",
                    "content": "Wafer fab equipment backlog.",
                },
                "warnings": [],
                "retrieved_at": "2026-11-28T00:00:00+00:00",
                "local_only": True,
            },
            "document",
        ),
        (
            "funda-ai",
            "list",
            {"limit": 5},
            {
                "ok": True,
                "status": "ok",
                "results": [
                    {
                        "document_id": "funda:12",
                        "source": "funda-ai",
                        "title": "DeepSeek Inference Economics",
                    }
                ],
                "result_count": 1,
                "warnings": [],
                "retrieved_at": "2026-11-28T00:00:00+00:00",
                "local_only": True,
            },
            {
                "ok": True,
                "status": "ok",
                "results": [
                    {
                        "document_id": "funda:12",
                        "source": "funda-ai",
                        "title": "DeepSeek Inference Economics",
                    }
                ],
                "result_count": 1,
                "warnings": [],
                "retrieved_at": "2026-11-28T00:00:00+00:00",
                "local_only": True,
            },
            "results",
        ),
        (
            "sales-mail",
            "list",
            {"limit": 5, "params": {"sources": ["tmtb"], "media": "text"}},
            {
                "ok": True,
                "status": "ok",
                "results": [
                    {
                        "document_id": "tmtb:9",
                        "source": "tmtb",
                        "title": "TMTB Daily",
                    }
                ],
                "result_count": 1,
                "warnings": [],
                "retrieved_at": "2026-11-28T00:00:00+00:00",
                "local_only": True,
            },
            {
                "ok": True,
                "status": "ok",
                "results": [
                    {
                        "document_id": "tmtb:9",
                        "source": "tmtb",
                        "title": "TMTB Daily",
                    }
                ],
                "result_count": 1,
                "warnings": [],
                "retrieved_at": "2026-11-28T00:00:00+00:00",
                "local_only": True,
            },
            "sales_mail",
        ),
        (
            "guidepoint",
            "search_library",
            {"query": "GPU data center rack demand", "limit": 5},
            {
                "structuredContent": {
                    "data": [
                        {
                            "id": "library_ts:gp-42",
                            "transcript_name": "GPU Rack Power Architecture",
                            "date": "2026-11-20",
                        }
                    ]
                }
            },
            {
                "ok": True,
                "status": "ok",
                "data": {
                    "structuredContent": {
                        "data": [
                            {
                                "id": "library_ts:gp-42",
                                "transcript_name": "GPU Rack Power Architecture",
                                "date": "2026-11-20",
                            }
                        ]
                    }
                },
                "results": [
                    {
                        "id": "library_ts:gp-42",
                        "transcript_name": "GPU Rack Power Architecture",
                        "date": "2026-11-20",
                    }
                ],
                "count": 1,
            },
            "guidepoint",
        ),
        (
            "all",
            "search",
            {"query": "NVDA rack", "limit": 5},
            {
                "ok": True,
                "status": "ok",
                "results": [
                    {
                        "document_id": "wiki:42",
                        "canonical_id": "wiki:42",
                        "source": "company-wiki",
                        "title": "NVDA Rack Note",
                    }
                ],
                "result_count": 1,
                "source_status": {"company-wiki": {"ok": True, "status": "ok", "count": 1}},
                "retrieved_at": "2026-11-28T00:00:00+00:00",
                "local_only": True,
            },
            {
                "ok": True,
                "status": "ok",
                "results": [
                    {
                        "document_id": "wiki:42",
                        "canonical_id": "wiki:42",
                        "source": "company-wiki",
                        "title": "NVDA Rack Note",
                    }
                ],
                "result_count": 1,
                "source_status": {
                    "company-wiki": {"ok": True, "status": "ok", "count": 1},
                    "alphaengine": {"ok": True, "status": "ok", "count": 1},
                    "guidepoint": {"ok": True, "status": "ok", "count": 1},
                },
                "alphaengine": {
                    "ok": True,
                    "status": "ok",
                    "count": 1,
                    "results": [{"doc_id": "ae-9", "title": "AE NVDA Note"}],
                    "cursor": "cur-next-1",
                    "has_more": True,
                    "total_upstream": 25,
                },
                "guidepoint": {
                    "ok": True,
                    "status": "ok",
                    "count": 1,
                    "results": [{"id": "library_ts:gp-42", "transcript_name": "GP NVDA"}],
                },
                "retrieved_at": "2026-11-28T00:00:00+00:00",
            },
            "all_search",
        ),
        (
            "all",
            "read",
            {"document": "wiki:42"},
            {
                "ok": True,
                "status": "ok",
                "document": {
                    "document_id": "wiki:42",
                    "source": "company-wiki",
                    "title": "NVDA Rack Note",
                    "content": "Rack power density.",
                },
                "warnings": [],
                "retrieved_at": "2026-11-28T00:00:00+00:00",
                "local_only": True,
            },
            {
                "ok": True,
                "status": "ok",
                "document": {
                    "document_id": "wiki:42",
                    "source": "company-wiki",
                    "title": "NVDA Rack Note",
                    "content": "Rack power density.",
                },
                "warnings": [],
                "retrieved_at": "2026-11-28T00:00:00+00:00",
                "local_only": True,
            },
            "document",
        ),
    ],
)
async def test_r2_comparative_legacy_vs_forwarded_response_shape_all_migrated_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    operation: str,
    call_args: dict[str, Any],
    legacy_inner: dict[str, Any],
    rh_payload: dict[str, Any],
    check_kind: str,
) -> None:
    """Forwarded responses match pre-cutover `data_gateway` shapes (keeping `data.document`/`data.results` nested)."""
    # 1. Pre-cutover bridge with realistic legacy `data_gateway` envelope from `_run_stdio_rpc`
    pre_dir = tmp_path / "pre"
    pre_dir.mkdir()
    _mod_pre, pre_bridge = _make_bridge(
        pre_dir,
        monkeypatch,
        repo_env_lines=["APP_ENV=dalton", "DALTON_PROFILE=everflow"],
    )

    async def _legacy_stdio(reqs: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        _ = timeout_s
        req = reqs[0]
        req_args = req["params"]["arguments"]
        req_route = req_args["route"]
        if req_route == "alphaengine":
            env_payload: dict[str, Any] = {
                "ok": True,
                "status": "ok",
                "executed": True,
                "operation": "search_library",
                "data": {
                    "structuredContent": {
                        "results": [{"doc_id": "ae-9", "title": "AE NVDA Note"}],
                        "cursor": "cur-next-1",
                        "has_more": True,
                        "total_upstream": 25,
                    }
                },
            }
        elif req_route == "guidepoint" and route == "all":
            env_payload = {
                "ok": True,
                "status": "ok",
                "executed": True,
                "operation": "search_library",
                "data": {
                    "structuredContent": {
                        "data": [{"id": "library_ts:gp-42", "transcript_name": "GP NVDA"}]
                    }
                },
            }
        else:
            env_payload = {
                "ok": True,
                "status": "ok",
                "executed": True,
                "route": pre_bridge._load_legacy_route_entry(req_route),
                "request": {"route": req_route, "operation": operation},
                "data": legacy_inner,
                "gateway_at": "2026-11-28T00:00:00+00:00",
            }
            if req_route == "guidepoint":
                env_payload["argv"] = None
                env_payload["operation"] = "search_library"
                env_payload["parameters"] = {"query": call_args["query"], "size": 5}
            if req_route == "sales-mail":
                env_payload["migration"] = {
                    "deprecated_route": "sales-mail",
                    "engine": "research_library",
                    "result_format": "document_v1",
                    "replacement": "sales-notes / tmtb / all",
                    "note": "旧名称只作转发；返回 document_id/snippet，用 read 读取正文，不再返回旧分块 JSON。",
                }
        return [
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {
                    "content": [{"type": "text", "text": json.dumps(env_payload, ensure_ascii=False)}],
                    "isError": False,
                },
            }
        ]

    stdio_double = create_autospec(pre_bridge._run_stdio_rpc, spec_set=True, side_effect=_legacy_stdio)
    monkeypatch.setattr(pre_bridge, "_run_stdio_rpc", stdio_double)
    legacy_reply = await pre_bridge.handle_tools_call(
        101,
        {"name": "research", "arguments": {"route": route, "operation": operation, **call_args}},
    )
    legacy_body = json.loads(legacy_reply["result"]["content"][0]["text"])

    # 2. Post-cutover bridge forwarding to research-host
    post_dir = tmp_path / "post"
    post_dir.mkdir()
    _mod_post, post_bridge = _make_bridge(
        post_dir,
        monkeypatch,
        repo_env_lines=[
            "APP_ENV=dalton",
            "DALTON_PROFILE=everflow",
            "DALTON_RESEARCH_HOST_URL=http://127.0.0.1:8951",
            "DALTON_RESEARCH_HOST_TOKEN=rh-mcp-tok",
        ],
    )
    mock_http = create_autospec(httpx.AsyncClient, instance=True, spec_set=True)
    mock_http.post.return_value = _mcp_ok_response(rh_payload, req_id=101)
    post_bridge._research_host_http = mock_http
    stdio_spy = create_autospec(post_bridge._run_stdio_rpc, spec_set=True)
    monkeypatch.setattr(post_bridge, "_run_stdio_rpc", stdio_spy)

    fwd_reply = await post_bridge.handle_tools_call(
        101,
        {"name": "research", "arguments": {"route": route, "operation": operation, **call_args}},
    )
    fwd_body = json.loads(fwd_reply["result"]["content"][0]["text"])

    assert stdio_spy.await_count == 0
    assert fwd_reply["result"]["isError"] is False
    assert legacy_body["ok"] is True and fwd_body["ok"] is True
    assert fwd_body["executed"] == legacy_body["executed"] is True
    assert fwd_body["route"]["id"] == legacy_body["route"]["id"] == route
    # Never hoist `results` or `document` out of `data`
    assert ("results" in fwd_body) == ("results" in legacy_body) is False
    assert ("document" in fwd_body) == ("document" in legacy_body) is False

    if check_kind == "document":
        assert fwd_body["data"]["document"] == legacy_body["data"]["document"]
    elif check_kind == "results":
        assert fwd_body["data"]["results"] == legacy_body["data"]["results"]
    elif check_kind == "sales_mail":
        assert fwd_body["data"]["results"] == legacy_body["data"]["results"]
        assert fwd_body["migration"] == legacy_body["migration"]
    elif check_kind == "guidepoint":
        assert (
            fwd_body["data"]["structuredContent"]["data"]
            == legacy_body["data"]["structuredContent"]["data"]
        )
        assert fwd_body["operation"] == legacy_body["operation"] == "search_library"
    elif check_kind == "all_search":
        assert fwd_body["data"]["results"] == legacy_body["data"]["results"]
        assert fwd_body["alphaengine"]["ok"] is True
        assert fwd_body["alphaengine"]["route"] == legacy_body["alphaengine"]["route"] == "alphaengine"
        assert fwd_body["alphaengine"]["cursor"] == legacy_body["alphaengine"]["cursor"] == "cur-next-1"
        assert fwd_body["alphaengine"]["has_more"] == legacy_body["alphaengine"]["has_more"] is True
        assert fwd_body["alphaengine"]["total_upstream"] == legacy_body["alphaengine"]["total_upstream"] == 25
        assert fwd_body["guidepoint"]["ok"] is True
        assert fwd_body["guidepoint"]["route"] == legacy_body["guidepoint"]["route"] == "guidepoint"
        assert "alphaengine" in fwd_body["source_status"]
        assert "guidepoint" in fwd_body["source_status"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["company-wiki", "all"])
async def test_r2_image_read_preserves_mcp_image_block_and_enforces_size_and_path_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    """Image reads keep `data.document`, emit MCP `image` content block, and enforce path containment + 5 MiB limit."""
    research_home = tmp_path / "research_home"
    assets_dir = research_home / "state" / "research-images" / "assets"
    assets_dir.mkdir(parents=True)
    img_bytes = b"\xff\xd8\xff\xe0" + b"JPEG_PREVIEW_BYTES_1234"
    preview_file = assets_dir / "img_1_preview.jpg"
    preview_file.write_bytes(img_bytes)

    _mod, bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        repo_env_lines=[
            "APP_ENV=dalton",
            "DALTON_PROFILE=everflow",
            "DALTON_RESEARCH_HOST_URL=http://127.0.0.1:8951",
            "DALTON_RESEARCH_HOST_TOKEN=rh-mcp-tok",
        ],
    )
    bridge.cfg.research_home = research_home

    image_doc = {
        "document_id": "image:1",
        "source": "company-wiki",
        "media_type": "image",
        "title": "NVDA Architecture Slide",
        "preview_path": str(preview_file),
        "preview_mime": "image/jpeg",
    }
    rh_payload = {"ok": True, "status": "ok", "document": image_doc, "local_only": True}

    # 1. Research-host returns only host file path -> bridge reads file and attaches MCP image block
    mock_http = create_autospec(httpx.AsyncClient, instance=True, spec_set=True)
    mock_http.post.return_value = _mcp_ok_response(rh_payload, req_id=31)
    bridge._research_host_http = mock_http

    reply = await bridge.handle_tools_call(
        31,
        {"name": "research", "arguments": {"route": route, "operation": "read", "document": "image:1"}},
    )
    assert reply["result"]["isError"] is False
    blocks = reply["result"]["content"]
    assert len(blocks) == 2
    text_body = json.loads(blocks[0]["text"])
    assert "document" not in text_body
    assert text_body["data"]["document"]["document_id"] == "image:1"
    assert blocks[1] == {
        "type": "image",
        "mimeType": "image/jpeg",
        "data": base64.b64encode(img_bytes).decode("ascii"),
    }

    # 2. Research-host already provides MCP image block -> bridge passes it through verbatim
    upstream_b64 = base64.b64encode(b"UPSTREAM_IMAGE").decode("ascii")
    mock_http.post.return_value = httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": 32,
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps(rh_payload, ensure_ascii=False)},
                    {"type": "image", "mimeType": "image/png", "data": upstream_b64},
                ],
                "structuredContent": rh_payload,
                "isError": False,
            },
        },
    )
    reply_pass = await bridge.handle_tools_call(
        32,
        {"name": "research", "arguments": {"route": route, "operation": "read", "document": "image:1"}},
    )
    assert reply_pass["result"]["isError"] is False
    assert len(reply_pass["result"]["content"]) == 2
    assert reply_pass["result"]["content"][1] == {
        "type": "image",
        "mimeType": "image/png",
        "data": upstream_b64,
    }

    # 3. Preview exceeding 5 MiB limit -> rejected cleanly
    big_file = assets_dir / "too_big.jpg"
    big_file.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    big_payload = {
        "ok": True,
        "status": "ok",
        "document": {**image_doc, "preview_path": str(big_file)},
    }
    mock_http.post.return_value = _mcp_ok_response(big_payload, req_id=33)
    reply_big = await bridge.handle_tools_call(
        33,
        {"name": "research", "arguments": {"route": route, "operation": "read", "document": "image:1"}},
    )
    assert reply_big["result"]["isError"] is True
    assert len(reply_big["result"]["content"]) == 1
    big_body = json.loads(reply_big["result"]["content"][0]["text"])
    assert big_body["ok"] is False
    assert "too large" in big_body["error"]

    # 4. Path outside <research_home>/state/research-images/assets -> rejected cleanly
    escaped_file = tmp_path / "secret.jpg"
    escaped_file.write_bytes(b"SECRET")
    escaped_payload = {
        "ok": True,
        "status": "ok",
        "document": {**image_doc, "preview_path": str(escaped_file)},
    }
    mock_http.post.return_value = _mcp_ok_response(escaped_payload, req_id=34)
    reply_esc = await bridge.handle_tools_call(
        34,
        {"name": "research", "arguments": {"route": route, "operation": "read", "document": "image:1"}},
    )
    assert reply_esc["result"]["isError"] is True
    assert len(reply_esc["result"]["content"]) == 1
    esc_body = json.loads(reply_esc["result"]["content"][0]["text"])
    assert esc_body["ok"] is False
    assert "invalid image asset path" in esc_body["error"]


@pytest.mark.asyncio
async def test_r2_end_to_end_real_research_host_image_read_returns_mcp_image_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real `dalton-research-host` `/mcp/company-wiki` and `/mcp/federated` `image:1` reads yield MCP `image` blocks via bridge."""
    data_root = tmp_path / "research_data"
    assets_dir = data_root / "state" / "research-images" / "assets"
    assets_dir.mkdir(parents=True)
    img_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF_E2E_TEST"
    sha = hashlib.sha256(img_bytes).hexdigest()
    orig_rel = "state/research-images/assets/orig.jpg"
    prev_rel = "state/research-images/assets/prev.jpg"
    (data_root / orig_rel).write_bytes(img_bytes)
    (data_root / prev_rel).write_bytes(img_bytes)

    rt = ResearchHostRuntime(
        ResearchHostConfig(
            data_root=data_root,
            mcp_token="rh-mcp-tok",
            admin_token="rh-adm-tok",
            extra_caller_admin_secrets=("br-adm-tok",),
            profile=DeploymentProfile.EVERFLOW,
            environ={"APP_ENV": "dalton", "DALTON_PROFILE": "everflow"},
        )
    )
    rt.wiki_store.ensure_images_schema()
    with sqlite3.connect(rt.wiki_store.images_db) as conn:
        conn.execute(
            "INSERT INTO assets (sha, path, preview_path, width, height, mime, size_bytes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sha, orig_rel, prev_rel, 800, 600, "image/jpeg", len(img_bytes), "2026-11-28T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO refs (id, asset_sha, source, doc_key, title, date, metadata, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, sha, "company-wiki", "wiki:1", "NVDA Chart", "2026-11-28", "{}", "2026-11-28T00:00:00+00:00"),
        )
        conn.commit()

    _mod, bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        repo_env_lines=[
            "APP_ENV=dalton",
            "DALTON_PROFILE=everflow",
            "DALTON_RESEARCH_HOST_URL=http://127.0.0.1:8951",
            "DALTON_RESEARCH_HOST_TOKEN=rh-mcp-tok",
        ],
    )
    bridge.cfg.research_home = data_root
    app = create_app(runtime=rt)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8951",
        ) as asgi_client:
            bridge._research_host_http = asgi_client
            for route in ("company-wiki", "all"):
                reply = await bridge.handle_tools_call(
                    41,
                    {"name": "research", "arguments": {"route": route, "operation": "read", "document": "image:1"}},
                )
                assert reply["result"]["isError"] is False
                blocks = reply["result"]["content"]
                assert len(blocks) == 2
                body = json.loads(blocks[0]["text"])
                assert "document" not in body
                assert body["data"]["document"]["document_id"] == "image:1"
                assert blocks[1]["type"] == "image"
                assert blocks[1]["mimeType"] == "image/jpeg"
                assert base64.b64decode(blocks[1]["data"]) == img_bytes
    finally:
        await rt.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "operation", "extra_args"),
    [
        ("guidepoint", "search_library", {"query": "NVDA rack demand", "limit": 10}),
        ("company-wiki", "search", {"query": "META", "limit": 5, "params": {"company": "META"}}),
        ("sales-notes", "search", {"query": "TSMC CoWoS", "limit": 5}),
        ("tmtb", "read", {"document": "tmtb:99"}),
        ("funda-ai", "list", {"limit": 10}),
        ("all", "search", {"query": "AI capex", "limit": 5}),
        ("sales-mail", "search", {"query": "bogey", "limit": 5}),
    ],
)
async def test_r2_mode_plan_returns_legacy_preview_without_calling_research_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    operation: str,
    extra_args: dict[str, Any],
) -> None:
    """`mode='plan'` on migrated routes returns legacy `executed: false` preview without touching research-host."""
    _mod, bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        repo_env_lines=[
            "APP_ENV=dalton",
            "DALTON_PROFILE=everflow",
            "DALTON_RESEARCH_HOST_URL=http://127.0.0.1:8951",
            "DALTON_RESEARCH_HOST_TOKEN=rh-mcp-tok",
        ],
    )
    mock_http = create_autospec(httpx.AsyncClient, instance=True, spec_set=True)
    bridge._research_host_http = mock_http
    stdio_spy = create_autospec(bridge._run_stdio_rpc, spec_set=True)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", stdio_spy)

    reply = await bridge.handle_tools_call(
        51,
        {
            "name": "research",
            "arguments": {"route": route, "operation": operation, "mode": "plan", **extra_args},
        },
    )

    assert mock_http.post.await_count == 0
    assert stdio_spy.await_count == 0
    assert reply["result"]["isError"] is False
    body = json.loads(reply["result"]["content"][0]["text"])
    assert body["ok"] is True
    assert body["status"] == "planned"
    assert body["executed"] is False
    assert body["route"]["id"] == route
    assert body["request"]["route"] == route

    # Cross-check key fields against real `data_gateway.py plan`
    dg_cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "dalton" / "research_runtime" / "scripts" / "data_gateway.py"),
        "plan",
        route,
        f"--operation={operation}",
    ]
    if "query" in extra_args:
        dg_cmd.append(f"--query={extra_args['query']}")
    if "document" in extra_args:
        dg_cmd.append(f"--document={extra_args['document']}")
    if "limit" in extra_args:
        dg_cmd.append(f"--limit={extra_args['limit']}")
    if "params" in extra_args:
        dg_cmd.append("--params-json=" + json.dumps(extra_args["params"], ensure_ascii=False))
    dg_proc = subprocess.run(dg_cmd, capture_output=True, text=True, timeout=15, check=False)
    assert dg_proc.returncode == 0, dg_proc.stdout + dg_proc.stderr
    dg_plan = json.loads(dg_proc.stdout)

    assert body["executed"] == dg_plan["executed"] is False
    assert body["status"] == dg_plan["status"] == "planned"
    assert body["route"]["id"] == dg_plan["route"]["id"]
    if route == "guidepoint":
        assert body["argv"] is None and dg_plan["argv"] is None
        assert body["operation"] == dg_plan["operation"]
        assert body["parameters"] == dg_plan["parameters"]
    else:
        assert isinstance(body["argv"], list) and isinstance(dg_plan["argv"], list)
        assert body["argv"][:3] == dg_plan["argv"][:3]
    if route == "sales-mail":
        assert body["migration"] == dg_plan["migration"]


@pytest.mark.asyncio
async def test_r2_base_url_alone_does_not_activate_forwarding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only `DALTON_RESEARCH_HOST_URL` enables forwarding; `DALTON_RESEARCH_HOST_BASE_URL` alone returns `route_migrated`."""
    _mod, bridge = _make_bridge(
        tmp_path,
        monkeypatch,
        repo_env_lines=[
            "APP_ENV=dalton",
            "DALTON_PROFILE=everflow",
            "DALTON_RESEARCH_HOST_BASE_URL=http://127.0.0.1:8951",
            "DALTON_RESEARCH_HOST_TOKEN=rh-mcp-tok",
            "DALTON_RESEARCH_ADMIN_TOKEN=rh-adm-tok",
        ],
    )
    assert bridge._resolve_research_host_url() == ""
    assert bridge._is_research_host_forwarding_active() is False

    mock_http = create_autospec(httpx.AsyncClient, instance=True, spec_set=True)
    bridge._research_host_http = mock_http
    stdio_spy = create_autospec(bridge._run_stdio_rpc, spec_set=True)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", stdio_spy)

    for route, op, extra in (
        ("company-wiki", "search", {"query": "NVDA"}),
        ("guidepoint", "search_library", {"query": "NVDA"}),
        ("all", "search", {"query": "NVDA"}),
    ):
        resp = await bridge.handle_tools_call(
            61,
            {"name": "research", "arguments": {"route": route, "operation": op, **extra}},
        )
        assert resp["result"]["isError"] is True
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["error_code"] == "route_migrated"

    w_code, w_body = await bridge.handle_wiki_ingest(
        {"target_type": "company", "target": "NVDA", "content_type": "note", "content": "hi"}
    )
    assert w_code == 410 and w_body["error_code"] == "route_migrated"

    sn_code, sn_body = await bridge.handle_sales_notes_ingest(
        {"institution": "MS", "title": "Note", "content": "hi"}
    )
    assert sn_code == 410 and sn_body["error_code"] == "route_migrated"

    assert mock_http.post.await_count == 0
    assert stdio_spy.await_count == 0


@pytest.mark.asyncio
async def test_r2_all_search_sources_filter_and_alphaengine_pagination_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`all.search` keeps local `params.sources` scoped to local libs (appending enabled online legs) and preserves AlphaEngine pagination."""
    # 1. With both alphaengine and guidepoint enabled, local-only sources list expands to include them on `all.search`, NOT `all.list`
    d1 = tmp_path / "both_online"
    d1.mkdir()
    _mod, bridge = _make_bridge(
        d1,
        monkeypatch,
        repo_env_lines=[
            "APP_ENV=dalton",
            "DALTON_PROFILE=everflow",
            "DALTON_RESEARCH_HOST_URL=http://127.0.0.1:8951",
            "DALTON_RESEARCH_HOST_TOKEN=rh-mcp-tok",
        ],
    )
    mock_http = create_autospec(httpx.AsyncClient, instance=True, spec_set=True)
    mock_http.post.return_value = _mcp_ok_response(
        {"ok": True, "status": "ok", "results": [], "result_count": 0},
        req_id=71,
    )
    bridge._research_host_http = mock_http

    await bridge.handle_tools_call(
        71,
        {
            "name": "research",
            "arguments": {
                "route": "all",
                "operation": "search",
                "query": "NVDA",
                "params": {"sources": ["sales-notes"]},
            },
        },
    )
    search_sent = mock_http.post.call_args_list[0].kwargs["json"]["params"]["arguments"]
    assert search_sent["params"]["sources"] == ["sales-notes", "alphaengine", "guidepoint"]

    await bridge.handle_tools_call(
        72,
        {
            "name": "research",
            "arguments": {
                "route": "all",
                "operation": "list",
                "params": {"sources": ["sales-notes"]},
            },
        },
    )
    list_sent = mock_http.post.call_args_list[1].kwargs["json"]["params"]["arguments"]
    assert list_sent["params"]["sources"] == ["sales-notes"]

    # 2. When guidepoint capability is disabled, only enabled online leg (`alphaengine`) is appended
    d2 = tmp_path / "no_gp"
    d2.mkdir()
    _mod2, bridge_no_gp = _make_bridge(
        d2,
        monkeypatch,
        repo_env_lines=[
            "APP_ENV=dalton",
            "DALTON_PROFILE=everflow",
            "DALTON_CAP_GUIDEPOINT=false",
            "DALTON_RESEARCH_HOST_URL=http://127.0.0.1:8951",
            "DALTON_RESEARCH_HOST_TOKEN=rh-mcp-tok",
        ],
    )
    mock_http2 = create_autospec(httpx.AsyncClient, instance=True, spec_set=True)
    mock_http2.post.return_value = _mcp_ok_response(
        {"ok": True, "status": "ok", "results": [], "result_count": 0},
        req_id=73,
    )
    bridge_no_gp._research_host_http = mock_http2
    await bridge_no_gp.handle_tools_call(
        73,
        {
            "name": "research",
            "arguments": {
                "route": "all",
                "operation": "search",
                "query": "NVDA",
                "params": {"sources": ["company-wiki"]},
            },
        },
    )
    no_gp_sent = mock_http2.post.call_args.kwargs["json"]["params"]["arguments"]
    assert no_gp_sent["params"]["sources"] == ["company-wiki", "alphaengine"]

    # 3. Real FederatedResearchConnector preserves `cursor`, `has_more`, and `total_upstream` under `alphaengine`
    reg = ConnectorRegistry()
    wiki_store = OpenWikiStore(tmp_path / "fed_data")

    async def _fake_ae_leg(
        query: str,
        start: str | None,
        end: str | None,
        limit: int,
        params: dict[str, Any],
        caller_key: str,
    ) -> dict[str, Any]:
        _ = (query, start, end, limit, params, caller_key)
        return {
            "ok": True,
            "status": "ok",
            "results": [{"doc_id": "ae-100", "title": "Page 1 Note"}],
            "count": 1,
            "cursor": "cur-page-2",
            "has_more": True,
            "total_upstream": 42,
        }

    reg.register_federated_source(
        "alphaengine",
        CapabilityId.ALPHAENGINE,
        search_fn=_fake_ae_leg,
        top_level_block=True,
        fuse_local=False,
        provenance_identity="internal_note",
    )
    fed = FederatedResearchConnector(reg, wiki_store, fanout_grace_s=5.0)
    fed_res = await fed.call_tool(
        "search",
        {"query": "NVDA", "limit": 5},
        active_caps=frozenset({CapabilityId.ALPHAENGINE}),
    )
    assert fed_res["ok"] is True
    assert fed_res["alphaengine"]["cursor"] == "cur-page-2"
    assert fed_res["alphaengine"]["has_more"] is True
    assert fed_res["alphaengine"]["total_upstream"] == 42

