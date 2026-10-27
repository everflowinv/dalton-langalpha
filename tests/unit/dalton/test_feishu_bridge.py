"""Unit tests for the researchdata bridge's Feishu OpenAPI proxy (`DALTON-HOOK(feishu-docs)`).

Covers token parsing, tenant-token caching/refresh, the five `/feishu/*` routes'
admin-token gate (401/403), and doc-create grant behavior. All test doubles use
`unittest.mock.create_autospec`; no real network calls leave the test process
(the bridge server runs on an ephemeral localhost port with stubbed handlers).
"""

from __future__ import annotations

import asyncio
import http.client
import json
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402


BRIDGE_TOKEN = "bridge-mcp-token-secret"
ADMIN_TOKEN = "bridge-admin-token-secret"


def _make_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> rb.BridgeConfig:
    empty_env = tmp_path / "bridge.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(empty_env))
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", BRIDGE_TOKEN)
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    # Deterministic regardless of the developer's ~/.openclaw: never read real credentials.
    monkeypatch.setattr(
        rb,
        "_read_openclaw_feishu_credentials",
        create_autospec(rb._read_openclaw_feishu_credentials, return_value=("", "")),
    )
    return rb.BridgeConfig()


def _make_bridge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> rb.ResearchDataBridge:
    cfg = _make_config(monkeypatch, tmp_path)
    assert cfg.feishu_app_id == "" and cfg.feishu_app_secret == ""
    return rb.ResearchDataBridge(cfg)


# ---------------------------------------------------------------------------
# parse_feishu_token
# ---------------------------------------------------------------------------


def test_parse_feishu_token_variants() -> None:
    assert rb.parse_feishu_token("https://x.feishu.cn/wiki/AbCd1234?from=tim") == ("wiki", "AbCd1234")
    assert rb.parse_feishu_token("https://x.feishu.cn/docx/DocX9876") == ("docx", "DocX9876")
    assert rb.parse_feishu_token("https://x.feishu.cn/docs/Legacy55#heading") == ("docx", "Legacy55")
    # Bare tokens carry no kind: resolve_feishu_document tries wiki first, then docx.
    assert rb.parse_feishu_token("bare-token-123") == ("", "bare-token-123")
    assert rb.parse_feishu_token("https://x.feishu.cn/messenger/xyz") == ("", "")
    assert rb.parse_feishu_token("") == ("", "")
    assert rb.parse_feishu_token("   ") == ("", "")


# ---------------------------------------------------------------------------
# FeishuClient tenant-token cache
# ---------------------------------------------------------------------------


def _stubbed_client() -> tuple[rb.FeishuClient, list[dict[str, Any]]]:
    client = rb.FeishuClient("fake-app-id", "fake-app-secret", timeout_s=1.0)
    auth_calls: list[dict[str, Any]] = []

    def _fake_request(method, path, *, json_body=None, query=None, with_auth=True, unwrap_data=True):
        auth_calls.append({"method": method, "path": path})
        if path == "/open-apis/auth/v3/tenant_access_token/internal":
            return {"tenant_access_token": "t-tenant-1", "expire": 7200}
        return {}

    client._request = create_autospec(client._request, side_effect=_fake_request)
    return client, auth_calls


def test_tenant_token_cached_until_margin_expiry() -> None:
    client, auth_calls = _stubbed_client()
    token_a = client._tenant_access_token()
    token_b = client._tenant_access_token()
    assert token_a == token_b == "t-tenant-1"
    # 7200s TTL minus the 300s refresh margin
    assert 6850 < client._token_expires_at - time.time() <= 6900
    assert len(auth_calls) == 1

    # Rewind past the margin: the next call re-authenticates.
    client._token_expires_at = time.time() - 1.0
    assert client._tenant_access_token() == "t-tenant-1"
    assert len(auth_calls) == 2


def test_tenant_token_auth_failure_raises_without_leaking_secret() -> None:
    client = rb.FeishuClient("fake-app-id", "fake-app-secret", timeout_s=1.0)

    def _fake_request(method, path, *, json_body=None, query=None, with_auth=True, unwrap_data=True):
        return {"code": 99991663, "msg": "app secret invalid"}

    client._request = create_autospec(client._request, side_effect=_fake_request)
    with pytest.raises(rb.FeishuApiError) as excinfo:
        client._tenant_access_token()
    assert "fake-app-secret" not in str(excinfo.value)
    # The stubbed _request bypasses code validation, so the empty token surfaces
    # as feishu_auth_failed; the secret never appears in the message either way.
    assert excinfo.value.error_code == "feishu_auth_failed"


# ---------------------------------------------------------------------------
# handler-level: doc create + grant
# ---------------------------------------------------------------------------


def _stub_feishu_client(bridge: rb.ResearchDataBridge, **overrides: Any) -> rb.FeishuClient:
    client = rb.FeishuClient("fake-app-id", "fake-app-secret", timeout_s=1.0)
    defaults: dict[str, Any] = {
        "create_document": {"document_id": "doccnCreated1", "title": "T"},
        "create_wiki_doc": {"obj_token": "doccnWiki1", "node_token": "wikinode1"},
        "convert_markdown": (["blk1", "blk2"], [{"block_id": "blk1"}, {"block_id": "blk2"}]),
        "insert_descendants": None,
        "get_document": {"title": "Readback Title"},
        "get_node": {"obj_token": "doccnWiki1", "node_token": "wikinode1", "space_id": "sp1"},
        "raw_content": "body text",
        "list_spaces": [{"space_id": "sp1", "name": "Research", "description": ""}],
        "list_nodes": [{"node_token": "n1", "obj_token": "doccnA", "obj_type": "docx",
                        "title": "Node A", "parent_node_token": "", "has_child": False}],
        "root_children_count": 3,
        "grant_chat_full_access": None,
    }
    # Capture the real bound methods before any stubbing so overrides on a key
    # that defaults also stubs still spec the original, not another mock.
    originals = {name: getattr(client, name) for name in set(defaults) | set(overrides)}
    for name, value in defaults.items():
        setattr(client, name, create_autospec(originals[name], return_value=value))
    for name, value in overrides.items():
        setattr(
            client,
            name,
            create_autospec(
                originals[name],
                side_effect=value if callable(value) else None,
                return_value=None if callable(value) else value,
            ),
        )
    bridge._feishu_client_instance = client
    return client


@pytest.mark.asyncio
async def test_doc_create_grants_and_survives_grant_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _make_bridge(monkeypatch, tmp_path)

    def _grant_fail(token, email):
        raise rb.FeishuApiError("feishu_http_403", "code=99991672 no permission")

    _stub_feishu_client(
        bridge,
        grant_full_access=_grant_fail,
    )

    code, payload = await bridge.handle_feishu_doc_create(
        {"title": "T", "markdown": "# hi", "grant_email": "user@corp.com"}
    )
    assert code == 200
    assert payload["ok"] is True
    assert payload["document_id"] == "doccnCreated1"
    assert payload["title"] == "Readback Title"
    assert "grant_failed" in payload["warning"]
    assert "user@corp.com" in payload["warning"]


@pytest.mark.asyncio
async def test_doc_create_grant_success_has_no_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _make_bridge(monkeypatch, tmp_path)
    client = _stub_feishu_client(bridge)
    client.grant_full_access = create_autospec(client.grant_full_access, return_value=None)

    code, payload = await bridge.handle_feishu_doc_create(
        {"title": "T", "markdown": "# hi", "grant_email": "user@corp.com", "space_id": "sp1"}
    )
    assert code == 200
    assert payload["ok"] is True
    assert payload["space_id"] == "sp1"
    assert payload["node_token"] == "wikinode1"
    assert "warning" not in payload
    client.create_wiki_doc.assert_called_once()


@pytest.mark.asyncio
async def test_doc_create_validation_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _make_bridge(monkeypatch, tmp_path)
    code, payload = await bridge.handle_feishu_doc_create({"title": "", "markdown": "x"})
    assert code == 400 and payload["error"] == "title_required"
    code, payload = await bridge.handle_feishu_doc_create({"title": "T", "markdown": ""})
    assert code == 400 and payload["error"] == "markdown_required"


@pytest.mark.asyncio
async def test_doc_append_inserts_at_end_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _make_bridge(monkeypatch, tmp_path)
    client = _stub_feishu_client(
        bridge,
        get_node={"obj_token": "doccnWiki1", "node_token": "wikinode1", "space_id": "sp1"},
    )

    code, payload = await bridge.handle_feishu_doc_append(
        {"token": "https://x.feishu.cn/wiki/wikinode1", "markdown": "more"}
    )
    assert code == 200
    assert payload["ok"] is True and payload["appended_blocks"] == 2
    client.insert_descendants.assert_called_once_with(
        "doccnWiki1", "doccnWiki1", ["blk1", "blk2"], [{"block_id": "blk1"}, {"block_id": "blk2"}], 3
    )


@pytest.mark.asyncio
async def test_unconfigured_credentials_returns_503(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _make_bridge(monkeypatch, tmp_path)
    code, payload = await bridge.handle_feishu_doc_read({"token": "abc123"})
    assert code == 503
    assert payload == {"ok": False, "error": "feishu_not_configured"}


# ---------------------------------------------------------------------------
# HTTP surface: admin-token gate in front of /feishu/* (401/403) and dispatch
#
# `_handle_client` is driven in process over an in-memory StreamReader and an
# autospecced StreamWriter: this host denies outbound TCP connect() even to
# 127.0.0.1, so a real-socket variant cannot run here.
# ---------------------------------------------------------------------------


async def _drive_handle_client(
    bridge: rb.ResearchDataBridge,
    *,
    path: str,
    body: dict[str, Any],
    token: str | None,
) -> tuple[int, dict[str, Any]]:
    """Feed one POST request through `rb._handle_client`; return `(status, json)`."""
    payload = json.dumps(body).encode("utf-8")
    header_lines = ["Host: 127.0.0.1"]
    if token:
        header_lines.append(f"Authorization: Bearer {token}")
    raw = (
        f"POST {path} HTTP/1.1\r\n"
        + "\r\n".join(header_lines)
        + f"\r\nContent-Length: {len(payload)}\r\n\r\n"
    ).encode("utf-8") + payload
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    writer = create_autospec(asyncio.StreamWriter, instance=True)
    await rb._handle_client(bridge, reader, writer)
    written = b"".join(call.args[0] for call in writer.write.call_args_list)
    status_line = written.split(b"\r\n", 1)[0].decode("utf-8")
    status = int(status_line.split(" ")[1])
    body_start = written.split(b"\r\n\r\n", 1)[1]
    return status, json.loads(body_start.decode("utf-8"))


@pytest.mark.asyncio
async def test_feishu_routes_require_admin_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _make_bridge(monkeypatch, tmp_path)
    bridge.handle_feishu_wiki_nodes = create_autospec(
        bridge.handle_feishu_wiki_nodes, return_value=(200, {"ok": True, "spaces": []})
    )

    # Missing token -> 401, MCP token -> 403, admin token -> dispatched to handler.
    status, payload = await _drive_handle_client(
        bridge, path="/feishu/wiki/nodes", body={"space_id": ""}, token=None
    )
    assert status == 401 and payload["error"] == "Unauthorized"

    status, payload = await _drive_handle_client(
        bridge, path="/feishu/wiki/nodes", body={"space_id": ""}, token=BRIDGE_TOKEN
    )
    assert status == 403 and payload["error"] == "Forbidden"

    status, payload = await _drive_handle_client(
        bridge, path="/feishu/wiki/nodes", body={"space_id": ""}, token=ADMIN_TOKEN
    )
    assert status == 200
    assert payload == {"ok": True, "spaces": []}
    assert bridge.handle_feishu_wiki_nodes.await_count == 1
    assert bridge.handle_feishu_wiki_nodes.await_args.args[0] == {"space_id": ""}

    # Unknown /feishu/* path behind a valid admin token -> 404.
    status, _ = await _drive_handle_client(
        bridge, path="/feishu/doc/nope", body={}, token=ADMIN_TOKEN
    )
    assert status == 404


# ---------------------------------------------------------------------------
# HTTP layer (urllib opener autospec'd; no real sockets): auth payload shape,
# descendant insert body, and create-failure reporting
# ---------------------------------------------------------------------------


def _http_layer(
    monkeypatch: pytest.MonkeyPatch, responses: list[tuple[int, dict[str, Any]]]
) -> list[Any]:
    """Serve canned Feishu responses through an autospecced urllib opener.

    Each response is ``(status, payload_dict)``; the real ``FeishuClient._request``
    runs untouched, so the exact wire shape (top-level auth fields, `data`
    wrapping, insert bodies) is what gets asserted.
    """
    import email
    import io
    import urllib.error
    import urllib.request

    requests_seen: list[Any] = []
    opener = create_autospec(urllib.request.OpenerDirector, instance=True)

    def _open(req: Any, timeout: Any = None) -> Any:
        requests_seen.append(req)
        status, payload = responses.pop(0)
        body = json.dumps(payload).encode("utf-8")
        if status >= 400:
            raise urllib.error.HTTPError(
                req.full_url, status, "Error", email.message.Message(), io.BytesIO(body)
            )
        resp = create_autospec(http.client.HTTPResponse, instance=True)
        resp.read.return_value = body
        resp.status = status
        resp.__enter__.return_value = resp
        return resp

    opener.open.side_effect = _open
    monkeypatch.setattr(
        rb.urllib.request,
        "build_opener",
        create_autospec(urllib.request.build_opener, return_value=opener),
    )
    return requests_seen


def _request_body(req: Any) -> dict[str, Any]:
    return json.loads((req.data or b"{}").decode("utf-8"))


def test_tenant_token_reads_top_level_fields_over_http_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auth endpoint has NO `data` envelope; the client must still get and cache the token."""
    seen = _http_layer(
        monkeypatch,
        [(200, {"code": 0, "msg": "ok", "tenant_access_token": "t-real-1", "expire": 7200})],
    )
    client = rb.FeishuClient("fake-app-id", "fake-app-secret", timeout_s=1.0)
    assert client._tenant_access_token() == "t-real-1"
    # Cached: a second fetch makes no HTTP call.
    assert client._tenant_access_token() == "t-real-1"
    assert len(seen) == 1
    assert seen[0].full_url.endswith("/open-apis/auth/v3/tenant_access_token/internal")
    body = _request_body(seen[0])
    assert body["app_id"] == "fake-app-id"
    assert "fake-app-secret" not in json.dumps(seen[0].headers)


@pytest.mark.asyncio
async def test_append_insert_body_carries_descendants(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The descendant insert must submit sanitized `descendants` (official block shapes)."""
    # Official docx block structure: block_type is an int (2=text, 31=table,
    # 32=table_cell) and table.cells is a list of block ids, not nested objects.
    convert_blocks = [
        {
            "block_id": "b1",
            "block_type": 2,
            "parent_id": "doccnA",
            "text": {"elements": [{"text_run": {"content": "hi"}}]},
        },
        {
            "block_id": "b2",
            "block_type": 31,
            "parent_id": "doccnA",
            "children": ["c1", "c2"],
            "table": {
                "cells": ["c1", "c2"],
                "property": {
                    "row_size": 1,
                    "column_size": 2,
                    "column_width": [110, 220],
                    "merge_info": [
                        {
                            "row_start_index": 0,
                            "row_end_index": 0,
                            "column_start_index": 0,
                            "column_end_index": 1,
                        }
                    ],
                },
            },
        },
        {"block_id": "c1", "block_type": 32, "parent_id": "b2", "children": "t1"},
        {"block_id": "t1", "block_type": 2, "parent_id": "c1", "text": {"elements": []}},
    ]
    seen = _http_layer(
        monkeypatch,
        [
            (200, {"code": 0, "msg": "ok", "tenant_access_token": "t-real-1", "expire": 7200}),
            (200, {"code": 0, "data": {"node": {"obj_token": "doccnA", "node_token": "w1", "space_id": "sp1"}}}),
            (200, {"code": 0, "data": {"first_level_block_ids": ["b1", "b2"], "blocks": convert_blocks}}),
            (200, {"code": 0, "data": {"items": [{}, {}], "has_more": False}}),
            (200, {"code": 0, "data": {}}),
        ],
    )
    bridge = _make_bridge(monkeypatch, tmp_path)
    bridge._feishu_client_instance = rb.FeishuClient(
        "fake-app-id", "fake-app-secret", timeout_s=1.0
    )

    code, payload = await bridge.handle_feishu_doc_append(
        {"token": "https://x.feishu.cn/wiki/w1", "markdown": "# hi"}
    )
    assert code == 200 and payload["ok"] is True

    assert [r.full_url for r in seen] == [
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node?token=w1",
        "https://open.feishu.cn/open-apis/docx/v1/documents/blocks/convert",
        "https://open.feishu.cn/open-apis/docx/v1/documents/doccnA/blocks/doccnA/children?page_size=50",
        "https://open.feishu.cn/open-apis/docx/v1/documents/doccnA/blocks/doccnA/descendant",
    ]
    insert_body = _request_body(seen[4])
    assert insert_body["children_id"] == ["b1", "b2"]
    assert insert_body["index"] == 2
    # descendants mirror convert output, sanitized per OpenClaw cleanBlocksForDescendant:
    # parent_id dropped everywhere; table.property rebuilt writable-only (no
    # cells/merge_info); a table cell's string children normalized to a list.
    assert insert_body["descendants"] == [
        {"block_id": "b1", "block_type": 2, "text": {"elements": [{"text_run": {"content": "hi"}}]}},
        {
            "block_id": "b2",
            "block_type": 31,
            "children": ["c1", "c2"],
            "table": {"property": {"row_size": 1, "column_size": 2, "column_width": [110, 220]}},
        },
        {"block_id": "c1", "block_type": 32, "children": ["t1"]},
        {"block_id": "t1", "block_type": 2, "text": {"elements": []}},
    ]


@pytest.mark.asyncio
async def test_create_insert_failure_reports_created_doc_and_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed body insert must NOT look like success: 502, explicit message, doc id + link."""
    seen = _http_layer(
        monkeypatch,
        [
            (200, {"code": 0, "msg": "ok", "tenant_access_token": "t-real-1", "expire": 7200}),
            (200, {"code": 0, "data": {"first_level_block_ids": ["b1"], "blocks": [{"block_id": "b1"}]}}),
            (200, {"code": 0, "data": {"document": {"document_id": "doccnHalf", "title": "T"}}}),
            (400, {"code": 1770043, "msg": "no edit permission for this document"}),
        ],
    )
    bridge = _make_bridge(monkeypatch, tmp_path)
    bridge._feishu_client_instance = rb.FeishuClient(
        "fake-app-id", "fake-app-secret", timeout_s=1.0
    )

    code, payload = await bridge.handle_feishu_doc_create(
        {"title": "T", "markdown": "# hi", "grant_email": "user@corp.com"}
    )
    assert code == 502
    assert payload["ok"] is False
    assert payload["error"] == "content_insert_failed"
    assert payload["document_id"] == "doccnHalf"
    assert payload["url"] == "https://feishu.cn/docx/doccnHalf"
    assert "NOT written" in payload["message"]
    assert "1770043" in payload["message"]
    # Convert runs before document creation; no grant attempt and no readback on a failed insert.
    assert [r.full_url for r in seen] == [
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        "https://open.feishu.cn/open-apis/docx/v1/documents/blocks/convert",
        "https://open.feishu.cn/open-apis/docx/v1/documents",
        "https://open.feishu.cn/open-apis/docx/v1/documents/doccnHalf/blocks/doccnHalf/descendant",
    ]
    assert all("/permissions/" not in r.full_url for r in seen)
    assert all("/documents/doccnHalf?" not in r.full_url for r in seen)


@pytest.mark.asyncio
async def test_create_convert_failure_does_not_create_document_or_wiki_doc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When markdown conversion fails or produces no blocks, neither create_document nor create_wiki_doc is called."""
    bridge = _make_bridge(monkeypatch, tmp_path)

    def _convert_fail(markdown: str):
        raise rb.FeishuApiError(
            "feishu_invalid_response",
            "http_404 content_type=text/plain path=/open-apis/docx/v1/documents/blocks/convert body='404 page not found'",
        )

    client_drive = _stub_feishu_client(bridge, convert_markdown=_convert_fail)
    code, payload = await bridge.handle_feishu_doc_create({"title": "T", "markdown": "# hi"})
    assert code == 502
    assert payload["ok"] is False
    assert payload["error"] == "feishu_invalid_response"
    assert "http_404" in payload["detail"]
    client_drive.create_document.assert_not_called()
    client_drive.create_wiki_doc.assert_not_called()

    client_wiki = _stub_feishu_client(bridge, convert_markdown=_convert_fail)
    code_w, payload_w = await bridge.handle_feishu_doc_create(
        {"title": "T", "markdown": "# hi", "space_id": "sp1"}
    )
    assert code_w == 502
    assert payload_w["ok"] is False
    client_wiki.create_document.assert_not_called()
    client_wiki.create_wiki_doc.assert_not_called()

    # Empty convert result also aborts before creating any document.
    client_empty = _stub_feishu_client(bridge, convert_markdown=([], []))
    code_e, payload_e = await bridge.handle_feishu_doc_create({"title": "T", "markdown": "# hi"})
    assert code_e == 502
    assert payload_e["error"] == "markdown_convert_empty"
    client_empty.create_document.assert_not_called()
    client_empty.create_wiki_doc.assert_not_called()


@pytest.mark.asyncio
async def test_append_insert_failure_reports_doc_url_and_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _make_bridge(monkeypatch, tmp_path)

    def _insert_fail(doc_id, block_id, children_ids, descendants, index):
        raise rb.FeishuApiError("feishu_http_400", "code=1770001 msg=invalid block")

    _stub_feishu_client(bridge, insert_descendants=_insert_fail)
    code, payload = await bridge.handle_feishu_doc_append(
        {"token": "https://x.feishu.cn/wiki/wikinode1", "markdown": "# more"}
    )
    assert code == 502
    assert payload["ok"] is False
    assert payload["error"] == "content_insert_failed"
    assert payload["document_id"] == "doccnWiki1"
    assert payload["url"] == "https://feishu.cn/wiki/wikinode1"
    assert "1770001" in payload["message"]
    assert "1770001" in payload["detail"]


def test_request_non_json_error_carries_status_content_type_path_template_without_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import email
    import io
    import urllib.error
    import urllib.request

    hdrs = email.message.Message()
    hdrs["Content-Type"] = "text/plain; charset=utf-8"
    raw_body = (
        "404 page not found Authorization: Bearer t-secretTokenValue999 "
        "token=u-userSecretToken888 fake-app-secret doxcnSecretDoc123456 "
        + ("x" * 200)
    )

    opener = create_autospec(urllib.request.OpenerDirector, instance=True)

    def _open(req: Any, timeout: Any = None) -> Any:
        raise urllib.error.HTTPError(
            req.full_url, 404, "Not Found", hdrs, io.BytesIO(raw_body.encode("utf-8"))
        )

    opener.open.side_effect = _open
    monkeypatch.setattr(
        rb.urllib.request,
        "build_opener",
        create_autospec(urllib.request.build_opener, return_value=opener),
    )

    client = rb.FeishuClient("fake-app-id", "fake-app-secret", timeout_s=1.0)
    client._tenant_token = "t-secretTokenValue999"
    client._token_expires_at = time.time() + 3600.0

    with pytest.raises(rb.FeishuApiError) as excinfo:
        client._request(
            "POST",
            "/open-apis/docx/v1/documents/doxcnSecretDoc123456/blocks/doxcnSecretDoc123456/descendant",
            query={"page_token": "pt_secret_query", "access_token": "t-secretTokenValue999"},
            json_body={"index": 0},
        )

    msg = str(excinfo.value)
    assert excinfo.value.error_code == "feishu_invalid_response"
    assert "http_404" in msg
    assert "text/plain; charset=utf-8" in msg
    assert "/open-apis/docx/v1/documents/{document_id}/blocks/{block_id}/descendant" in msg
    assert "404 page not found" in msg
    # No token, secret, or query string leaked in the error detail.
    for forbidden in (
        "doxcnSecretDoc123456",
        "t-secretTokenValue999",
        "u-userSecretToken888",
        "fake-app-secret",
        "pt_secret_query",
        "access_token",
    ):
        assert forbidden not in msg


@pytest.mark.asyncio
async def test_bare_token_resolves_wiki_first_then_docx(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _make_bridge(monkeypatch, tmp_path)

    # Bare token that IS a wiki node: resolved to its obj_token via one get_node.
    bridge._feishu_client_instance = client = _plain_client(
        get_node={"obj_token": "doccnBehindWiki", "node_token": "wikinode9", "space_id": "sp1"}
    )
    doc_id, node = rb.resolve_feishu_document(client, "", "wikinode9")
    assert doc_id == "doccnBehindWiki" and node["space_id"] == "sp1"
    client.get_node.assert_called_once_with("wikinode9", obj_type="")

    # Bare token that is not a wiki node (upstream rejects): falls back to docx id.
    bridge._feishu_client_instance = client2 = _plain_client(
        get_node=rb.FeishuApiError("feishu_api_error", "code=1310004 msg=node not found")
    )
    doc_id2, node2 = rb.resolve_feishu_document(client2, "", "doccnDirect")
    assert doc_id2 == "doccnDirect" and node2 == {}

    # Transport errors do NOT silently degrade to docx.
    bridge._feishu_client_instance = client3 = _plain_client(
        get_node=rb.FeishuApiError("feishu_network_error", "boom")
    )
    with pytest.raises(rb.FeishuApiError):
        rb.resolve_feishu_document(client3, "", "anything")


def _plain_client(**overrides: Any) -> rb.FeishuClient:
    client = rb.FeishuClient("fake-app-id", "fake-app-secret", timeout_s=1.0)
    originals = {name: getattr(client, name) for name in overrides}
    for name, value in overrides.items():
        if isinstance(value, Exception):
            side_effect, return_value = value, None
        elif callable(value):
            side_effect, return_value = value, None
        else:
            side_effect, return_value = None, value
        setattr(
            client,
            name,
            create_autospec(originals[name], side_effect=side_effect, return_value=return_value),
        )
    return client


@pytest.mark.asyncio
async def test_wiki_node_resolution_keeps_shortcut_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _make_bridge(monkeypatch, tmp_path)
    bridge._feishu_client_instance = _plain_client(
        get_node={
            "node_token": "sc1",
            "obj_token": "doccnOrigin",
            "obj_type": "docx",
            "title": "Shortcut to origin",
            "space_id": "sp-public",
            "node_type": "shortcut",
            "origin_space_id": "sp-secret",
        }
    )
    code, payload = await bridge.handle_feishu_wiki_node({"token": "sc1"})
    assert code == 200
    node = payload["node"]
    assert node["node_type"] == "shortcut"
    assert node["origin_space_id"] == "sp-secret"
    assert node["space_id"] == "sp-public"


# ---------------------------------------------------------------------------
# Permission-check resolution: bare docx tokens get the docx reverse lookup
# ---------------------------------------------------------------------------


def test_resolve_node_for_check_bare_token_falls_back_to_docx_lookup() -> None:
    """A bare token that is not a wiki node gets one obj_type=docx reverse lookup."""

    def _get_node(token: str, *, obj_type: str = "") -> dict[str, Any]:
        assert token == "doccnBare1"
        if obj_type == "docx":
            return {"obj_token": "doccnBare1", "space_id": "sp1", "node_token": "w1"}
        return {}  # plain wiki lookup misses

    client = _plain_client(get_node=_get_node)
    document_id, node = rb.resolve_feishu_node_for_check(client, "", "doccnBare1")
    assert document_id == "doccnBare1"
    assert node["space_id"] == "sp1"

    # Bare wiki node token: the plain lookup hits, no reverse lookup happens.
    def _get_node_wiki(token: str, *, obj_type: str = "") -> dict[str, Any]:
        assert obj_type == ""
        return {"obj_token": "doccnBehind", "space_id": "sp1", "node_token": "wikinode9"}

    client_wiki = _plain_client(get_node=_get_node_wiki)
    document_id2, node2 = rb.resolve_feishu_node_for_check(client_wiki, "", "wikinode9")
    assert document_id2 == "doccnBehind" and node2["space_id"] == "sp1"

    # Both lookups miss -> not in a wiki (caller denies), never raises.
    client_miss = _plain_client(get_node={})
    document_id3, node3 = rb.resolve_feishu_node_for_check(client_miss, "", "nothing")
    assert document_id3 == "nothing" and node3 == {}


@pytest.mark.asyncio
async def test_wiki_node_handler_reverse_looks_up_bare_docx_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _make_bridge(monkeypatch, tmp_path)

    def _get_node(token: str, *, obj_type: str = "") -> dict[str, Any]:
        if obj_type == "docx" and token == "doccnBare1":
            return {
                "obj_token": "doccnBare1",
                "space_id": "sp1",
                "node_token": "w1",
                "obj_type": "docx",
                "title": "Doc in wiki",
            }
        return {}

    bridge._feishu_client_instance = _plain_client(get_node=_get_node)
    code, payload = await bridge.handle_feishu_wiki_node({"token": "doccnBare1"})
    assert code == 200
    assert payload["node"]["space_id"] == "sp1"
    assert payload["node"]["obj_token"] == "doccnBare1"

    # A token resolving to nothing still reports not_in_wiki (not an error).
    bridge._feishu_client_instance = _plain_client(get_node={})
    code2, payload2 = await bridge.handle_feishu_wiki_node({"token": "orphan1"})
    assert code2 == 200 and payload2["node"] is None and payload2["reason"] == "not_in_wiki"


# ---------------------------------------------------------------------------
# Team-group share on create (mirrors OpenClaw's 飞书文档权限规则)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_doc_create_shares_with_team_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("FEISHU_DEFAULT_GRANT_CHAT_ID", raising=False)
    bridge = _make_bridge(monkeypatch, tmp_path)
    client = _stub_feishu_client(bridge)

    code, payload = await bridge.handle_feishu_doc_create({"title": "T", "markdown": "# hi"})

    assert code == 200
    assert "warning" not in payload
    client.grant_chat_full_access.assert_called_once_with(
        "doccnCreated1", rb.FEISHU_DEFAULT_GRANT_CHAT_ID
    )


@pytest.mark.asyncio
async def test_doc_create_team_group_failure_is_a_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("FEISHU_DEFAULT_GRANT_CHAT_ID", raising=False)
    bridge = _make_bridge(monkeypatch, tmp_path)

    def _fail(token, chat_id):
        raise rb.FeishuApiError("feishu_http_403", "code=1063002 no permission")

    def _email_fail(token, email):
        raise rb.FeishuApiError("feishu_http_403", "code=1063002 no permission")

    _stub_feishu_client(bridge, grant_chat_full_access=_fail, grant_full_access=_email_fail)

    code, payload = await bridge.handle_feishu_doc_create(
        {"title": "T", "markdown": "# hi", "grant_email": "user@corp.com"}
    )

    assert code == 200
    assert payload["ok"] is True
    assert "team group" in payload["warning"]
    assert "user@corp.com" in payload["warning"]


@pytest.mark.asyncio
async def test_doc_create_team_group_share_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FEISHU_DEFAULT_GRANT_CHAT_ID", "")
    bridge = _make_bridge(monkeypatch, tmp_path)
    client = _stub_feishu_client(bridge)

    code, _payload = await bridge.handle_feishu_doc_create({"title": "T", "markdown": "# hi"})

    assert code == 200
    client.grant_chat_full_access.assert_not_called()


def test_grant_chat_full_access_request_shape() -> None:
    client = rb.FeishuClient("fake-app-id", "fake-app-secret", timeout_s=1.0)
    client._request = create_autospec(client._request, return_value={})

    client.grant_chat_full_access("doccnX", "oc_team")

    client._request.assert_called_once_with(
        "POST",
        "/open-apis/drive/v1/permissions/doccnX/members",
        query={"type": "docx", "need_notification": "false"},
        json_body={"member_type": "openchat", "member_id": "oc_team", "perm": "full_access"},
    )
