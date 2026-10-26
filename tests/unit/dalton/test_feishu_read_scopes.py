"""Unit tests for the `/api/v1/dalton/feishu/*` settings endpoints (`DALTON-HOOK(feishu-docs)`).

Pins the admin-only gate (401/403), the whitelist replace flow, and the
bridge-backed space listing. All doubles use `unittest.mock.create_autospec`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import HTTPException

import src.dalton.app.router as dalton_router
import src.dalton.settings as settings_mod
import src.dalton.feishu as feishu_client_mod
from src.dalton.app.router import FeishuReadScopesUpdateRequest, FeishuScopeEntry
from src.dalton.database import feishu as feishu_db

PM_USER_ID = "11111111-1111-4111-8111-111111111111"
NON_PM_USER_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def _pm_roster(monkeypatch: pytest.MonkeyPatch):
    settings_mod.get_dalton_config.cache_clear()
    monkeypatch.setenv("DALTON_PM_USER_IDS", PM_USER_ID)
    yield
    settings_mod.get_dalton_config.cache_clear()


def _scope_row(space_id: str, name: str = "") -> dict[str, Any]:
    return {"space_id": space_id, "name": name, "added_by": PM_USER_ID, "added_at": "2026-01-01T00:00:00+00:00"}


@pytest.mark.asyncio
async def test_read_scopes_admin_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET/PUT read-scopes and GET spaces all require a PM user: 401 when anonymous, 403 for others."""

    def _endpoint_calls(user_id: str):
        return (
            lambda: dalton_router.list_feishu_read_scopes_endpoint(user_id=user_id),
            lambda: dalton_router.update_feishu_read_scopes_endpoint(
                body=FeishuReadScopesUpdateRequest(scopes=[]), user_id=user_id
            ),
            lambda: dalton_router.list_feishu_spaces_endpoint(user_id=user_id),
        )

    for call in _endpoint_calls(""):
        with pytest.raises(HTTPException) as exc:
            await call()
        assert exc.value.status_code == 401

    for call in _endpoint_calls(NON_PM_USER_ID):
        with pytest.raises(HTTPException) as exc:
            await call()
        assert exc.value.status_code == 403
        assert "仅管理员" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_list_read_scopes_returns_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        feishu_db,
        "list_read_scopes",
        create_autospec(feishu_db.list_read_scopes, return_value=[_scope_row("sp1", "Research")]),
    )
    res = await dalton_router.list_feishu_read_scopes_endpoint(user_id=PM_USER_ID)
    assert res == {"scopes": [{"space_id": "sp1", "name": "Research", "added_by": PM_USER_ID, "added_at": "2026-01-01T00:00:00+00:00"}]}


@pytest.mark.asyncio
async def test_update_read_scopes_replaces_whitelist_and_stamps_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    orig_replace = feishu_db.replace_read_scopes
    captured: dict[str, Any] = {}

    async def _fake_replace(scopes: list[dict[str, Any]], *, added_by: str, conn: Any = None):
        captured["scopes"] = scopes
        captured["added_by"] = added_by
        return [_scope_row(s["space_id"], s.get("name") or "") for s in scopes]

    monkeypatch.setattr(
        feishu_db,
        "replace_read_scopes",
        create_autospec(orig_replace, side_effect=_fake_replace),
    )
    body = FeishuReadScopesUpdateRequest(
        scopes=[
            FeishuScopeEntry(space_id="sp1", name="Research"),
            FeishuScopeEntry(space_id="sp2"),
        ]
    )
    res = await dalton_router.update_feishu_read_scopes_endpoint(body=body, user_id=PM_USER_ID)
    assert captured["added_by"] == PM_USER_ID
    assert [s["space_id"] for s in captured["scopes"]] == ["sp1", "sp2"]
    assert [s["space_id"] for s in res["scopes"]] == ["sp1", "sp2"]

    # A too-large list raises ValueError inside the DB helper -> HTTP 400
    # (HTTP-level >200 submissions are rejected earlier as 422 by pydantic —
    # see test_update_request_validation).
    async def _too_many(scopes: list[dict[str, Any]], *, added_by: str, conn: Any = None):
        raise ValueError("Too many read scopes (201 > 200)")

    monkeypatch.setattr(
        feishu_db,
        "replace_read_scopes",
        create_autospec(orig_replace, side_effect=_too_many),
    )
    with pytest.raises(HTTPException) as exc:
        await dalton_router.update_feishu_read_scopes_endpoint(
            body=FeishuReadScopesUpdateRequest(scopes=[]), user_id=PM_USER_ID
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_list_spaces_proxies_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    orig_post_bridge = feishu_client_mod.post_bridge_feishu

    async def _fake_bridge(route: str, body: dict[str, Any]) -> dict[str, Any]:
        assert route == "wiki/nodes" and body == {"space_id": ""}
        return {"ok": True, "spaces": [{"space_id": "sp1", "name": "Research"}]}

    monkeypatch.setattr(
        feishu_client_mod,
        "post_bridge_feishu",
        create_autospec(orig_post_bridge, side_effect=_fake_bridge),
    )
    res = await dalton_router.list_feishu_spaces_endpoint(user_id=PM_USER_ID)
    assert res == {"spaces": [{"space_id": "sp1", "name": "Research"}]}

    from src.dalton.feishu import FeishuBridgeError

    async def _failing(route: str, body: dict[str, Any]) -> dict[str, Any]:
        raise FeishuBridgeError("bridge_unreachable", "connect")

    monkeypatch.setattr(
        feishu_client_mod,
        "post_bridge_feishu",
        create_autospec(orig_post_bridge, side_effect=_failing),
    )
    with pytest.raises(HTTPException) as exc:
        await dalton_router.list_feishu_spaces_endpoint(user_id=PM_USER_ID)
    assert exc.value.status_code == 502


# ---------------------------------------------------------------------------
# Request-boundary validation (FastAPI maps pydantic failures to 422)
# ---------------------------------------------------------------------------


def test_update_request_rejects_201_entries() -> None:
    """A real 201-entry submission dies in pydantic -> FastAPI answers 422, never the endpoint."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        FeishuReadScopesUpdateRequest(
            scopes=[FeishuScopeEntry(space_id=f"sp{i:03d}") for i in range(201)]
        )


def test_update_request_rejects_malformed_space_ids() -> None:
    import pydantic

    for bad in ("", "   ", "sp 1", "sp/../../etc", "sp;drop", "空间1"):
        with pytest.raises(pydantic.ValidationError):
            FeishuScopeEntry(space_id=bad)


def test_update_request_strips_and_normalizes() -> None:
    entry = FeishuScopeEntry(space_id="  sp1_-OK  ", name="  Research  ")
    assert entry.space_id == "sp1_-OK"
    assert entry.name == "Research"


# ---------------------------------------------------------------------------
# DB layer: advisory lock + defense-in-depth space_id validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_read_scopes_takes_advisory_lock_then_replaces(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import asynccontextmanager

    import psycopg
    from psycopg.rows import dict_row

    executed: list[tuple[str, Any]] = []

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)

    async def _exec(query: Any, params: Any = None, **kw: Any) -> None:
        executed.append((str(query), params))

    cursor.execute.side_effect = _exec

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cursor

    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.cursor.side_effect = _cursor_cm
    assert dict_row  # row_factory only matters on the read path

    @asynccontextmanager
    async def _get_db_conn(conn_arg: Any = None):
        yield conn

    monkeypatch.setattr(feishu_db, "get_db_connection", create_autospec(feishu_db.get_db_connection, side_effect=_get_db_conn))
    monkeypatch.setattr(
        feishu_db,
        "list_read_scopes",
        create_autospec(feishu_db.list_read_scopes, return_value=[]),
    )

    await feishu_db.replace_read_scopes(
        [{"space_id": "sp1", "name": "Research"}, {"space_id": " sp2 "}], added_by=PM_USER_ID
    )
    sql_sequence = [sql for sql, _ in executed]
    assert sql_sequence[0].strip() == "SELECT pg_advisory_xact_lock(%s)"
    assert executed[0][1] == (feishu_db.READ_SCOPES_LOCK_KEY,)
    assert sql_sequence[1].startswith("DELETE FROM dalton_feishu_read_scopes")
    insert_sqls = [params for sql, params in executed if sql.lstrip().startswith("INSERT")]
    assert sorted(p[0] for p in insert_sqls) == ["sp1", "sp2"]  # stripped on insert


@pytest.mark.asyncio
async def test_replace_read_scopes_rejects_invalid_space_ids() -> None:
    with pytest.raises(ValueError):
        await feishu_db.replace_read_scopes([{"space_id": "sp;drop"}], added_by=PM_USER_ID)
    with pytest.raises(ValueError):
        await feishu_db.replace_read_scopes([{"space_id": "   "}], added_by=PM_USER_ID)


# ---------------------------------------------------------------------------
# Real HTTP semantics over the in-process ASGI transport (no sockets):
# >200-entry and malformed submissions come back as 422
# ---------------------------------------------------------------------------


def _feishu_client(monkeypatch: pytest.MonkeyPatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.dalton.app import dalton_router
    from src.server.utils.api import get_current_user_id

    app = FastAPI()
    app.dependency_overrides[get_current_user_id] = lambda: PM_USER_ID
    app.include_router(dalton_router)
    return TestClient(app, raise_server_exceptions=False)


def test_put_201_entries_returns_422_over_asgi(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine 201-entry PUT through FastAPI's request pipeline -> 422, endpoint never runs."""
    called = {"replace": 0}
    orig_replace = feishu_db.replace_read_scopes

    async def _counting_replace(*args: Any, **kw: Any):
        called["replace"] += 1
        return []

    monkeypatch.setattr(
        feishu_db, "replace_read_scopes", create_autospec(orig_replace, side_effect=_counting_replace)
    )
    client = _feishu_client(monkeypatch)
    resp = client.put(
        "/api/v1/dalton/feishu/read-scopes",
        json={"scopes": [{"space_id": f"sp{i:03d}"} for i in range(201)]},
    )
    assert resp.status_code == 422
    assert called["replace"] == 0


def test_put_malformed_space_id_returns_422_over_asgi(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _feishu_client(monkeypatch)
    resp = client.put(
        "/api/v1/dalton/feishu/read-scopes",
        json={"scopes": [{"space_id": "sp;drop table"}]},
    )
    assert resp.status_code == 422
