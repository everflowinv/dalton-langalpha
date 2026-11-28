"""Unit tests for legacy `researchdata_bridge` transparent forwarding to `dalton-research-host` (:8951)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import httpx
import pytest

from src.dalton.profile import DeploymentProfile
from src.dalton.research_host.app import ResearchHostConfig, ResearchHostRuntime, create_app
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
    assert body["results"] == upstream_payload["results"]
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
    assert payload["alphaengine"]["count"] == 1

