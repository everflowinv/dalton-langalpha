"""Unit tests for the feishu_* agent tools' tier gating (`DALTON-HOOK(feishu-docs)`).

The tier rules live in `src/dalton/access_tiers.py` (covered separately);
these tests pin the *tool* behavior: external members cannot write, external
reads require a whitelisted wiki space, and non-wiki docs are refused for
them. All doubles use `unittest.mock.create_autospec`; no network.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import create_autospec

import pytest

import src.dalton.tools.feishu_tools as feishu_tools_mod
from src.dalton.feishu import post_bridge_feishu
from src.dalton.tools.feishu_tools import (
    EXTERNAL_NOT_IN_WIKI_MESSAGE,
    EXTERNAL_READ_ONLY_MESSAGE,
    EXTERNAL_SPACE_DENIED_MESSAGE,
    UNAUTHENTICATED_MESSAGE,
    create_feishu_tools,
)

ADMIN_USER_ID = "11111111-1111-4111-8111-111111111111"
EXTERNAL_USER_ID = "33333333-3333-4333-8333-333333333333"


def _config(user_id: str | None) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    if user_id is not None:
        cfg["user_id"] = user_id
    return {"configurable": cfg}


def _parse(result: Any) -> dict[str, Any]:
    assert isinstance(result, str)
    return json.loads(result)


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch):
    """Fresh tool objects with the bridge and DB seams stubbed."""
    read_scope_ids: set[str] = {"space-ok"}
    bridge_calls: list[tuple[str, dict[str, Any]]] = []
    # The node the wiki/node resolution returns; tests mutate it (shortcut etc.).
    node_state: dict[str, Any] = {
        "node_token": "w1",
        "obj_token": "doccn1",
        "space_id": "space-ok",
        "node_type": "",
        "origin_space_id": "",
    }

    async def _fake_bridge(route: str, body: dict[str, Any]) -> dict[str, Any]:
        bridge_calls.append((route, dict(body)))
        if route == "wiki/node":
            return {"ok": True, "node": dict(node_state)}
        if route == "doc/read":
            return {
                "ok": True,
                "document_id": body.get("document_id") or "doccn1",
                "title": "T",
                "content": "body",
                "total_chars": 4,
                "truncated": False,
            }
        if route == "wiki/nodes":
            return {
                "ok": True,
                "space_id": body.get("space_id") or "",
                "nodes": [
                    {"node_token": "n1", "obj_token": "doccnA", "title": "Plain", "node_type": ""},
                    {
                        "node_token": "n2",
                        "obj_token": "doccnB",
                        "title": "Shortcut to open space",
                        "node_type": "shortcut",
                        "origin_space_id": "space-ok",
                    },
                    {
                        "node_token": "n3",
                        "obj_token": "doccnC",
                        "title": "Shortcut to closed space",
                        "node_type": "shortcut",
                        "origin_space_id": "space-secret",
                    },
                ],
            }
        if route == "doc/create":
            return {"ok": True, "document_id": "docNew1", "title": "T"}
        if route == "doc/append":
            return {"ok": True, "document_id": "doccn1", "appended_blocks": 2}
        return {"ok": True}

    monkeypatch.setattr(
        feishu_tools_mod,
        "post_bridge_feishu",
        create_autospec(post_bridge_feishu, side_effect=_fake_bridge),
    )

    async def _fake_scope_ids(*, conn: Any = None) -> set[str]:
        return set(read_scope_ids)

    async def _fake_list_scopes(*, conn: Any = None) -> list[dict[str, Any]]:
        return [{"space_id": sid, "name": sid, "added_by": "pm", "added_at": "2026-01-01"}
                for sid in sorted(read_scope_ids)]

    monkeypatch.setattr(
        feishu_tools_mod,
        "get_read_scope_space_ids",
        create_autospec(feishu_tools_mod.get_read_scope_space_ids, side_effect=_fake_scope_ids),
    )
    monkeypatch.setattr(
        feishu_tools_mod,
        "list_read_scopes",
        create_autospec(feishu_tools_mod.list_read_scopes, side_effect=_fake_list_scopes),
    )
    return create_feishu_tools(), bridge_calls, read_scope_ids, node_state


def _set_tier(monkeypatch: pytest.MonkeyPatch, tier: str) -> None:
    monkeypatch.setattr(
        feishu_tools_mod,
        "get_user_tier",
        create_autospec(feishu_tools_mod.get_user_tier, return_value=tier),
    )


# ---------------------------------------------------------------------------
# Unauthenticated calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_user_id_is_rejected_for_every_tool(monkeypatch: pytest.MonkeyPatch, tools) -> None:
    ts, bridge_calls, _, _ = tools
    args_by_tool = {
        "feishu_read": {"token": "tok"},
        "feishu_list_wiki": {},
        "feishu_create_doc": {"title": "T", "markdown": "x"},
        "feishu_append": {"token": "tok", "markdown": "x"},
        "feishu_grant_access": {"token": "tok"},
    }
    for tool in ts:
        payload = _parse(await tool.ainvoke(args_by_tool[tool.name], config=_config(None)))
        assert payload["ok"] is False and payload["error"] == "unauthenticated"
        assert payload["message"] == UNAUTHENTICATED_MESSAGE
    assert bridge_calls == []


# ---------------------------------------------------------------------------
# External members: write forbidden
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_member_cannot_create_or_append(monkeypatch: pytest.MonkeyPatch, tools) -> None:
    ts, bridge_calls, _, _ = tools
    _set_tier(monkeypatch, "external")
    feishu_create_doc, feishu_append = ts[2], ts[3]

    payload = _parse(
        await feishu_create_doc.ainvoke(
            {"title": "T", "markdown": "x"}, config=_config(EXTERNAL_USER_ID)
        )
    )
    assert payload["ok"] is False and payload["error"] == "external_write_forbidden"
    assert payload["message"] == EXTERNAL_READ_ONLY_MESSAGE

    payload = _parse(
        await feishu_append.ainvoke(
            {"token": "tok", "markdown": "x"}, config=_config(EXTERNAL_USER_ID)
        )
    )
    assert payload["ok"] is False and payload["error"] == "external_write_forbidden"
    # Neither write reached the bridge.
    assert not any(route.startswith("doc/") for route, _ in bridge_calls)


# ---------------------------------------------------------------------------
# External members: read gated by the wiki-space whitelist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_read_allowed_inside_whitelisted_space(monkeypatch: pytest.MonkeyPatch, tools) -> None:
    ts, bridge_calls, _, _ = tools
    _set_tier(monkeypatch, "external")
    feishu_read = ts[0]

    payload = _parse(
        await feishu_read.ainvoke({"token": "tokWiki"}, config=_config(EXTERNAL_USER_ID))
    )
    assert payload["ok"] is True and payload["tier"] == "external"
    assert payload["title"] == "T"
    assert payload["space_id"] == "space-ok" and payload["node_token"] == "w1"
    calls = list(bridge_calls)
    assert [route for route, _ in calls] == ["wiki/node", "doc/read"]
    # Single resolution: the read reuses the permission check's obj_token.
    assert calls[1][1]["document_id"] == "doccn1"


@pytest.mark.asyncio
async def test_external_read_denied_outside_whitelisted_space(monkeypatch: pytest.MonkeyPatch, tools) -> None:
    ts, bridge_calls, read_scope_ids, _ = tools
    read_scope_ids.clear()  # wiki/node still resolves, but to an unlisted space
    _set_tier(monkeypatch, "external")
    feishu_read = ts[0]

    payload = _parse(
        await feishu_read.ainvoke({"token": "tokWiki"}, config=_config(EXTERNAL_USER_ID))
    )
    assert payload["ok"] is False and payload["error"] == "external_space_not_allowed"
    assert payload["message"] == EXTERNAL_SPACE_DENIED_MESSAGE
    assert "doc/read" not in [route for route, _ in bridge_calls]


@pytest.mark.asyncio
async def test_external_read_denied_for_non_wiki_docx(monkeypatch: pytest.MonkeyPatch, tools) -> None:
    ts, bridge_calls, _, _ = tools

    async def _no_node(route: str, body: dict[str, Any]) -> dict[str, Any]:
        if route == "wiki/node":
            return {"ok": True, "node": None, "reason": "not_in_wiki"}
        raise AssertionError(f"unexpected bridge route for non-wiki external read: {route}")

    monkeypatch.setattr(
        feishu_tools_mod,
        "post_bridge_feishu",
        create_autospec(post_bridge_feishu, side_effect=_no_node),
    )
    _set_tier(monkeypatch, "external")
    feishu_read = create_feishu_tools()[0]

    payload = _parse(
        await feishu_read.ainvoke({"token": "tokDocx"}, config=_config(EXTERNAL_USER_ID))
    )
    assert payload["ok"] is False and payload["error"] == "external_doc_not_in_wiki"
    assert payload["message"] == EXTERNAL_NOT_IN_WIKI_MESSAGE


@pytest.mark.asyncio
async def test_external_list_wiki_shows_only_whitelisted_spaces(monkeypatch: pytest.MonkeyPatch, tools) -> None:
    ts, bridge_calls, _, _ = tools
    _set_tier(monkeypatch, "external")
    feishu_list_wiki = ts[1]

    payload = _parse(await feishu_list_wiki.ainvoke({}, config=_config(EXTERNAL_USER_ID)))
    assert payload["ok"] is True
    assert [s["space_id"] for s in payload["spaces"]] == ["space-ok"]
    assert bridge_calls == []  # served from the whitelist; no bridge round-trip

    payload = _parse(
        await feishu_list_wiki.ainvoke({"space_id": "space-other"}, config=_config(EXTERNAL_USER_ID))
    )
    assert payload["ok"] is False and payload["error"] == "external_space_not_allowed"


@pytest.mark.asyncio
async def test_external_list_wiki_filters_shortcuts_by_origin_space(
    monkeypatch: pytest.MonkeyPatch, tools
) -> None:
    """Shortcut nodes pointing outside the whitelist are hidden from external listings."""
    ts, _, _, _ = tools
    _set_tier(monkeypatch, "external")
    feishu_list_wiki = ts[1]

    payload = _parse(
        await feishu_list_wiki.ainvoke({"space_id": "space-ok"}, config=_config(EXTERNAL_USER_ID))
    )
    assert payload["ok"] is True
    assert [n["node_token"] for n in payload["nodes"]] == ["n1", "n2"]
    assert all(n["node_token"] != "n3" for n in payload["nodes"])


@pytest.mark.asyncio
async def test_external_shortcut_read_requires_both_spaces_whitelisted(
    monkeypatch: pytest.MonkeyPatch, tools
) -> None:
    """A shortcut crossing into a closed origin space is denied even from an open space."""
    ts, bridge_calls, read_scope_ids, node_state = tools
    _set_tier(monkeypatch, "external")
    feishu_read = ts[0]
    node_state["node_type"] = "shortcut"

    # Origin space NOT whitelisted -> denied.
    node_state["origin_space_id"] = "space-secret"
    payload = _parse(
        await feishu_read.ainvoke({"token": "tokShortcut"}, config=_config(EXTERNAL_USER_ID))
    )
    assert payload["ok"] is False and payload["error"] == "external_space_not_allowed"
    assert "doc/read" not in [route for route, _ in bridge_calls]

    # Origin space whitelisted too -> allowed.
    read_scope_ids.add("space-secret")
    bridge_calls.clear()
    payload = _parse(
        await feishu_read.ainvoke({"token": "tokShortcut"}, config=_config(EXTERNAL_USER_ID))
    )
    assert payload["ok"] is True
    assert [route for route, _ in bridge_calls] == ["wiki/node", "doc/read"]


# ---------------------------------------------------------------------------
# Admin / internal members: no whitelist detour, writes pass through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_read_goes_straight_to_doc_read(monkeypatch: pytest.MonkeyPatch, tools) -> None:
    ts, bridge_calls, _, _ = tools
    _set_tier(monkeypatch, "admin")
    feishu_read = ts[0]

    payload = _parse(
        await feishu_read.ainvoke({"token": "tokDocx"}, config=_config(ADMIN_USER_ID))
    )
    assert payload["ok"] is True and payload["tier"] == "admin"
    assert [route for route, _ in bridge_calls] == ["doc/read"]


@pytest.mark.asyncio
async def test_internal_create_doc_grants_real_company_email(
    monkeypatch: pytest.MonkeyPatch, tools
) -> None:
    ts, bridge_calls, _, _ = tools
    _set_tier(monkeypatch, "internal")
    monkeypatch.setenv("DALTON_INVITE_EMAIL_DOMAIN", "team.dalton.local")

    async def _fake_get_user(uid: str):
        return {"user_id": uid, "email": "User@Corp.com"}

    from src.server.database import user as user_db

    orig_get_user = user_db.get_user
    monkeypatch.setattr(
        user_db, "get_user", create_autospec(orig_get_user, side_effect=_fake_get_user)
    )
    feishu_create_doc = ts[2]

    payload = _parse(
        await feishu_create_doc.ainvoke(
            {"title": "T", "markdown": "# hi", "space_id": "space-ok"},
            config=_config("u-internal"),
        )
    )
    assert payload["ok"] is True and payload["tier"] == "internal"
    create_body = next(b for route, b in bridge_calls if route == "doc/create")
    assert create_body["grant_email"] == "user@corp.com"  # lowercased real email

    # An invite-signup synthetic address is never granted.
    async def _fake_get_user_invite(uid: str):
        return {"user_id": uid, "email": "alice01@team.dalton.local"}

    monkeypatch.setattr(
        user_db, "get_user", create_autospec(orig_get_user, side_effect=_fake_get_user_invite)
    )
    bridge_calls.clear()
    payload = _parse(
        await feishu_create_doc.ainvoke(
            {"title": "T", "markdown": "# hi"}, config=_config("u-invite")
        )
    )
    assert payload["ok"] is True
    create_body = next(b for route, b in bridge_calls if route == "doc/create")
    assert "grant_email" not in create_body


@pytest.mark.asyncio
async def test_bridge_failure_maps_to_error_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.dalton.feishu import FeishuBridgeError

    async def _unreachable(route: str, body: dict[str, Any]) -> dict[str, Any]:
        raise FeishuBridgeError("bridge_unreachable", "connect")

    monkeypatch.setattr(
        feishu_tools_mod,
        "post_bridge_feishu",
        create_autospec(post_bridge_feishu, side_effect=_unreachable),
    )
    _set_tier(monkeypatch, "admin")
    payload = _parse(
        await create_feishu_tools()[0].ainvoke({"token": "tok"}, config=_config(ADMIN_USER_ID))
    )
    assert payload["ok"] is False and payload["error"] == "bridge_unreachable"
    assert "宿主机" in payload["message"]


# ---------------------------------------------------------------------------
# Structured bridge errors reach the user (content_insert_failed full path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_bridge_feishu_unwraps_structured_502_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """`post_bridge_feishu` must parse a non-2xx JSON body, not blanket-map to http_502."""
    import httpx

    import src.dalton.feishu as feishu_client_mod
    from src.dalton.feishu import FeishuBridgeError, post_bridge_feishu

    real_client_cls = httpx.AsyncClient

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            json={
                "ok": False,
                "error": "content_insert_failed",
                "message": "document created but its body was NOT written (feishu_http_400: …); fix manually at https://feishu.cn/docx/doccnHalf",
                "document_id": "doccnHalf",
                "url": "https://feishu.cn/docx/doccnHalf",
            },
        )

    def _factory(**kwargs: Any):
        return real_client_cls(transport=httpx.MockTransport(_handler), timeout=kwargs.get("timeout"))

    monkeypatch.setattr(
        feishu_client_mod.httpx,
        "AsyncClient",
        create_autospec(real_client_cls, side_effect=_factory),
    )

    with pytest.raises(FeishuBridgeError) as excinfo:
        await post_bridge_feishu("doc/create", {"title": "T", "markdown": "x"})
    assert excinfo.value.error_code == "content_insert_failed"
    assert excinfo.value.payload["document_id"] == "doccnHalf"
    assert excinfo.value.payload["url"] == "https://feishu.cn/docx/doccnHalf"

    # A non-JSON or error-less body still maps to http_<status> as before.
    def _plain_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    def _plain_factory(**kwargs: Any):
        return real_client_cls(transport=httpx.MockTransport(_plain_handler), timeout=kwargs.get("timeout"))

    monkeypatch.setattr(
        feishu_client_mod.httpx,
        "AsyncClient",
        create_autospec(real_client_cls, side_effect=_plain_factory),
    )
    with pytest.raises(FeishuBridgeError) as excinfo_plain:
        await post_bridge_feishu("doc/read", {"token": "t"})
    assert excinfo_plain.value.error_code == "http_502"


@pytest.mark.asyncio
async def test_create_doc_reports_created_doc_url_to_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """End of the chain: the tool's reply names the half-written doc, its URL, and detail."""
    from src.dalton.feishu import FeishuBridgeError

    async def _insert_failed(route: str, body: dict[str, Any]) -> dict[str, Any]:
        raise FeishuBridgeError(
            "content_insert_failed",
            "document created but its body was NOT written",
            payload={
                "ok": False,
                "error": "content_insert_failed",
                "message": "document created but its body was NOT written",
                "detail": "feishu_http_400: code=1770001 msg=invalid block",
                "document_id": "doccnHalf",
                "url": "https://feishu.cn/docx/doccnHalf",
            },
        )

    monkeypatch.setattr(
        feishu_tools_mod,
        "post_bridge_feishu",
        create_autospec(post_bridge_feishu, side_effect=_insert_failed),
    )
    _set_tier(monkeypatch, "internal")
    payload = _parse(
        await create_feishu_tools()[2].ainvoke(
            {"title": "T", "markdown": "x"}, config=_config("u-internal")
        )
    )
    assert payload["ok"] is False and payload["error"] == "content_insert_failed"
    assert payload["message"] == "文档已创建但正文没写进去，请到 https://feishu.cn/docx/doccnHalf 手动处理。"
    assert payload["detail"] == "feishu_http_400: code=1770001 msg=invalid block"
    assert payload["url"] == "https://feishu.cn/docx/doccnHalf"
    assert payload["document_id"] == "doccnHalf"


@pytest.mark.asyncio
async def test_append_doc_reports_unchanged_original_doc_and_preserves_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Append failure must say appended content was not written and original doc was not modified."""
    from src.dalton.feishu import FeishuBridgeError

    async def _append_failed(route: str, body: dict[str, Any]) -> dict[str, Any]:
        raise FeishuBridgeError(
            "content_insert_failed",
            "追加内容没有写入，原文档未改变",
            payload={
                "ok": False,
                "error": "content_insert_failed",
                "outcome": "rejected",
                "message": "追加内容没有写入，原文档未改变 (feishu_http_400: code=1770001); url=https://feishu.cn/wiki/wikinode1",
                "detail": "feishu_http_400: code=1770001 msg=invalid block",
                "document_id": "doccnWiki1",
                "url": "https://feishu.cn/wiki/wikinode1",
            },
        )

    monkeypatch.setattr(
        feishu_tools_mod,
        "post_bridge_feishu",
        create_autospec(post_bridge_feishu, side_effect=_append_failed),
    )
    _set_tier(monkeypatch, "internal")
    payload = _parse(
        await create_feishu_tools()[3].ainvoke(
            {"token": "https://feishu.cn/wiki/wikinode1", "markdown": "more"},
            config=_config("u-internal"),
        )
    )
    assert payload["ok"] is False and payload["error"] == "content_insert_failed"
    assert "追加内容没有写入，原文档未改变" in payload["message"]
    assert "文档已创建" not in payload["message"]
    assert "1770001" in payload["message"]
    assert payload["detail"] == "feishu_http_400: code=1770001 msg=invalid block"
    assert payload["url"] == "https://feishu.cn/wiki/wikinode1"
    assert payload["document_id"] == "doccnWiki1"


@pytest.mark.asyncio
async def test_append_unknown_outcome_warns_to_check_doc_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network-blinded append may have landed: the tool must say the outcome
    is unknown and point at the doc, never that the original is unchanged."""
    from src.dalton.feishu import FeishuBridgeError

    async def _append_unknown(route: str, body: dict[str, Any]) -> dict[str, Any]:
        raise FeishuBridgeError(
            "content_insert_failed",
            "追加请求结果未知，可能已经写入",
            payload={
                "ok": False,
                "error": "content_insert_failed",
                "outcome": "unknown",
                "message": "追加请求结果未知，可能已经写入 (feishu_network_error: timeout); url=https://feishu.cn/wiki/wikinode1",
                "detail": "feishu_network_error: timeout",
                "document_id": "doccnWiki1",
                "url": "https://feishu.cn/wiki/wikinode1",
            },
        )

    monkeypatch.setattr(
        feishu_tools_mod,
        "post_bridge_feishu",
        create_autospec(post_bridge_feishu, side_effect=_append_unknown),
    )
    _set_tier(monkeypatch, "internal")
    payload = _parse(
        await create_feishu_tools()[3].ainvoke(
            {"token": "https://feishu.cn/wiki/wikinode1", "markdown": "more"},
            config=_config("u-internal"),
        )
    )
    assert payload["ok"] is False and payload["error"] == "content_insert_failed"
    assert "追加请求结果未知，可能已经写入" in payload["message"]
    assert "不要直接重试" in payload["message"]
    assert "原文档未改变" not in payload["message"]
    assert "timeout" in payload["message"]
    assert payload["url"] == "https://feishu.cn/wiki/wikinode1"
    assert payload["document_id"] == "doccnWiki1"


# ---------------------------------------------------------------------------
# Bare docx tokens for external members (docx reverse lookup on the check path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_bare_docx_token_resolves_via_reverse_lookup(monkeypatch: pytest.MonkeyPatch, tools) -> None:
    """A bare docx token in a whitelisted space is readable; outside the whitelist it is denied.

    The reverse lookup itself lives in the bridge (`resolve_feishu_node_for_check`,
    covered in test_feishu_bridge.py); here the node it returns feeds the tool gate.
    """
    ts, bridge_calls, _, node_state = tools
    _set_tier(monkeypatch, "external")
    feishu_read = ts[0]
    # The bridge's docx reverse lookup reports the same token as its own obj_token.
    node_state["obj_token"] = "doccnBare1"

    payload = _parse(
        await feishu_read.ainvoke({"token": "doccnBare1"}, config=_config(EXTERNAL_USER_ID))
    )
    assert payload["ok"] is True and payload["tier"] == "external"
    calls = list(bridge_calls)
    assert [route for route, _ in calls] == ["wiki/node", "doc/read"]
    assert calls[1][1]["document_id"] == "doccnBare1"

    # Same token resolving into a non-whitelisted space stays denied.
    node_state["space_id"] = "space-secret"
    payload = _parse(
        await feishu_read.ainvoke({"token": "doccnBare1"}, config=_config(EXTERNAL_USER_ID))
    )
    assert payload["ok"] is False and payload["error"] == "external_space_not_allowed"
    assert "doc/read" not in [route for route, _ in bridge_calls[2:]]


# ---------------------------------------------------------------------------
# Bound Feishu open_id lookup & grant propagation
# ---------------------------------------------------------------------------


class _FakeConnCtx:
    def __init__(self, target: Any) -> None:
        self._target = target

    async def __aenter__(self) -> Any:
        return self._target

    async def __aexit__(self, *args: Any) -> bool:
        return False


def _install_channel_gateway_db(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    from psycopg import AsyncConnection, AsyncCursor
    import src.dalton.database.channel_gateway as cg_mod

    state: dict[str, Any] = {"rows": [], "error": None}
    cur = create_autospec(AsyncCursor, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = False

    async def _execute(*args: Any, **kwargs: Any) -> None:
        if state["error"] is not None:
            raise state["error"]

    async def _fetchall() -> list[dict[str, Any]]:
        return list(state["rows"])

    cur.execute.side_effect = _execute
    cur.fetchall.side_effect = _fetchall

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.return_value = cur
    ctx = _FakeConnCtx(conn)
    monkeypatch.setattr(
        cg_mod,
        "get_db_connection",
        create_autospec(cg_mod.get_db_connection, side_effect=lambda existing=None: ctx),
    )
    return state


@pytest.mark.asyncio
async def test_get_feishu_open_id_for_user_cases(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    from src.dalton.database.channel_gateway import get_feishu_open_id_for_user

    secret_oid = "ou_SecretBound99"
    state = _install_channel_gateway_db(monkeypatch)
    state["rows"] = [{"platform_user_id": secret_oid}]
    assert await get_feishu_open_id_for_user("u-1") == secret_oid

    # 0 rows -> "", logged.
    state["rows"] = []
    with caplog.at_level(logging.INFO, logger="src.dalton.database.channel_gateway"):
        assert await get_feishu_open_id_for_user("u-none") == ""
    assert any("no binding" in r.getMessage() and "u-none" in r.getMessage() for r in caplog.records)

    # Multiple rows -> "", logged without open_id plaintext.
    caplog.clear()
    state["rows"] = [{"platform_user_id": secret_oid}, {"platform_user_id": "ou_Second2"}]
    with caplog.at_level(logging.WARNING, logger="src.dalton.database.channel_gateway"):
        assert await get_feishu_open_id_for_user("u-multi") == ""
    assert any("multiple rows" in r.getMessage() for r in caplog.records)
    assert all(secret_oid not in r.getMessage() and "ou_Second2" not in r.getMessage() for r in caplog.records)

    # Query error -> "", logged.
    caplog.clear()
    state["rows"] = []
    state["error"] = RuntimeError("db down")
    with caplog.at_level(logging.WARNING, logger="src.dalton.database.channel_gateway"):
        assert await get_feishu_open_id_for_user("u-err") == ""
    assert any("failed" in r.getMessage() and "RuntimeError" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_create_doc_and_grant_access_propagate_bound_open_id(
    monkeypatch: pytest.MonkeyPatch, tools
) -> None:
    import src.dalton.database.channel_gateway as cg_mod
    from src.server.database import user as user_db

    ts, bridge_calls, _, _ = tools
    _set_tier(monkeypatch, "internal")
    monkeypatch.setenv("DALTON_INVITE_EMAIL_DOMAIN", "team.dalton.local")

    orig_get_user = user_db.get_user
    orig_get_oid = cg_mod.get_feishu_open_id_for_user

    # 1) Bound open_id + real email: doc/create carries both grant_open_id & grant_email.
    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(orig_get_user, return_value={"user_id": "u-1", "email": "User@Corp.com"}),
    )
    monkeypatch.setattr(
        cg_mod,
        "get_feishu_open_id_for_user",
        create_autospec(orig_get_oid, return_value="ou_BoundOwner1"),
    )
    feishu_create_doc = ts[2]
    feishu_grant_access = ts[4]

    payload = _parse(
        await feishu_create_doc.ainvoke(
            {"title": "T", "markdown": "# hi"}, config=_config("u-1")
        )
    )
    assert payload["ok"] is True
    create_body = next(b for route, b in bridge_calls if route == "doc/create")
    assert create_body["grant_open_id"] == "ou_BoundOwner1"
    assert create_body["grant_email"] == "user@corp.com"

    # feishu_grant_access sends both email and open_id.
    bridge_calls.clear()
    grant_payload = _parse(
        await feishu_grant_access.ainvoke({"token": "doccn1"}, config=_config("u-1"))
    )
    assert grant_payload["ok"] is True
    grant_body = next(b for route, b in bridge_calls if route == "doc/grant")
    assert grant_body == {"token": "doccn1", "email": "user@corp.com", "open_id": "ou_BoundOwner1"}

    # 2) Bound open_id + synthetic invite email: doc/create omits grant_email, keeps grant_open_id;
    #    feishu_grant_access sends open_id instead of returning no_grant_email.
    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(
            orig_get_user, return_value={"user_id": "u-inv", "email": "alice@team.dalton.local"}
        ),
    )
    bridge_calls.clear()
    await feishu_create_doc.ainvoke({"title": "T", "markdown": "# hi"}, config=_config("u-inv"))
    create_body = next(b for route, b in bridge_calls if route == "doc/create")
    assert create_body["grant_open_id"] == "ou_BoundOwner1"
    assert "grant_email" not in create_body

    bridge_calls.clear()
    grant_payload = _parse(
        await feishu_grant_access.ainvoke({"token": "doccn1"}, config=_config("u-inv"))
    )
    assert grant_payload["ok"] is True
    grant_body = next(b for route, b in bridge_calls if route == "doc/grant")
    assert grant_body == {"token": "doccn1", "open_id": "ou_BoundOwner1"}

    # 3) No bound open_id + real email: body omits grant_open_id / open_id.
    monkeypatch.setattr(
        cg_mod,
        "get_feishu_open_id_for_user",
        create_autospec(orig_get_oid, return_value=""),
    )
    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(orig_get_user, return_value={"user_id": "u-email", "email": "a@corp.com"}),
    )
    bridge_calls.clear()
    await feishu_create_doc.ainvoke({"title": "T", "markdown": "# hi"}, config=_config("u-email"))
    create_body = next(b for route, b in bridge_calls if route == "doc/create")
    assert "grant_open_id" not in create_body
    assert create_body["grant_email"] == "a@corp.com"

    # 4) Neither bound open_id nor valid email: feishu_grant_access returns no_grant_email.
    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(orig_get_user, return_value={"user_id": "u-none", "email": ""}),
    )
    bridge_calls.clear()
    err_payload = _parse(
        await feishu_grant_access.ainvoke({"token": "doccn1"}, config=_config("u-none"))
    )
    assert err_payload["ok"] is False
    assert err_payload["error"] == "no_grant_email"
    assert "open_id" in err_payload["message"] and "邮箱" in err_payload["message"]
    assert bridge_calls == []
