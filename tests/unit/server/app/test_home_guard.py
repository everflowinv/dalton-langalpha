"""Home before its first computer: every route that reaches its files or
sandbox refuses it the same way, through one guard."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from src.server.app.share_links import router as share_links_router
from src.server.app.workspace_files import router as files_router
from src.server.app.workspace_files._shared import (
    HOME_NOT_ON_COMPUTER,
    owned_workspace_on_computer,
)
from src.server.app.workspace_sandbox import router as sandbox_router
from tests.conftest import create_test_app

pytestmark = pytest.mark.asyncio

USER = "test-user-123"
WS = "ws-home-guard"
BASE = f"/api/v1/workspaces/{WS}"
_READ = "src.server.app.workspace_files._shared.db_get_workspace"


def _row(**overrides) -> dict:
    return {"workspace_id": WS, "user_id": USER, "status": "running", **overrides}


@pytest.mark.parametrize(
    "row, status, detail",
    [
        (None, 404, "Workspace not found"),
        (_row(user_id="someone-else"), 403, "Forbidden"),
        (_row(status="unbound"), 400, HOME_NOT_ON_COMPUTER),
    ],
    ids=["missing", "another-users", "home-not-on-a-computer"],
)
async def test_the_guard_refuses_as_the_routes_always_have(row, status, detail):
    with patch(_READ, AsyncMock(return_value=row)):
        with pytest.raises(HTTPException) as exc:
            await owned_workspace_on_computer(WS, USER)
    assert (exc.value.status_code, exc.value.detail) == (status, detail)


async def test_the_guard_returns_the_row_it_read():
    row = _row()
    with patch(_READ, AsyncMock(return_value=row)):
        assert await owned_workspace_on_computer(WS, USER) is row


ROUTES = [
    ("GET", "/files/read", {"params": {"path": "a.txt"}}),
    ("PUT", "/files/write", {"params": {"path": "a.txt"}, "json": {"content": "x"}}),
    ("GET", "/files/download", {"params": {"path": "a.txt"}}),
    ("GET", "/files/download-url", {"params": {"path": "a.txt"}}),
    (
        "POST",
        "/files/upload",
        {"params": {"path": "a.txt"}, "files": {"file": ("a.txt", b"x", "text/plain")}},
    ),
    ("POST", "/files/backup", {}),
    ("DELETE", "/files", {"json": {"paths": ["a.txt"]}}),
    ("GET", "/sandbox/stats", {}),
    ("POST", "/sandbox/packages", {"json": {"packages": ["pandas"]}}),
    ("POST", "/file-grant", {}),
    ("GET", "/share-links", {}),
    ("POST", "/share-links", {"json": {"kind": "file", "path": "a.txt"}}),
]


@pytest.mark.parametrize("method, path, kwargs", ROUTES, ids=[f"{m} {p}" for m, p, _ in ROUTES])
async def test_every_route_that_needs_the_computer_refuses_home_before_it(method, path, kwargs):
    app = create_test_app(files_router, sandbox_router, share_links_router)
    with patch(_READ, AsyncMock(return_value=_row(status="unbound"))):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.request(method, f"{BASE}{path}", **kwargs)
    assert (resp.status_code, resp.json()) == (400, {"detail": HOME_NOT_ON_COMPUTER})


# --- DALTON-HOOK(workspace-sharing-settings): begin ---
async def test_owned_workspace_on_computer_respects_dalton_sharing_need(monkeypatch):
    from unittest.mock import create_autospec
    from src.config import settings as app_settings
    from src.dalton import sharing as dalton_sharing
    from src.server.database import user as user_db

    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setattr(app_settings, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("DALTON_ALLOWED_EMAILS", "owner@dalton.test,peer@dalton.test")
    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(
            user_db.get_user,
            return_value={"user_id": "u-peer", "email": "peer@dalton.test"},
        ),
    )

    from src.server.database.workspace import get_workspace as db_get_workspace

    row = _row(user_id="u-owner", is_home=False)
    orig_get_sharing = dalton_sharing.get_workspace_sharing
    with patch(_READ, create_autospec(db_get_workspace, return_value=row)):
        # rw allows need="write"
        monkeypatch.setattr(
            dalton_sharing,
            "get_workspace_sharing",
            create_autospec(
                orig_get_sharing,
                return_value={"workspace_id": WS, "cos_access": "ro", "web_access": "rw"},
            ),
        )
        out = await owned_workspace_on_computer(WS, "u-peer", need="write")
        assert out["workspace_id"] == WS

        # ro rejects need="write" with 403
        monkeypatch.setattr(
            dalton_sharing,
            "get_workspace_sharing",
            create_autospec(
                orig_get_sharing,
                return_value={"workspace_id": WS, "cos_access": "ro", "web_access": "ro"},
            ),
        )
        with pytest.raises(HTTPException) as exc_ro:
            await owned_workspace_on_computer(WS, "u-peer", need="write")
        assert exc_ro.value.status_code == 403

        # hidden rejects with 404
        monkeypatch.setattr(
            dalton_sharing,
            "get_workspace_sharing",
            create_autospec(
                orig_get_sharing,
                return_value={"workspace_id": WS, "cos_access": "ro", "web_access": "hidden"},
            ),
        )
        with pytest.raises(HTTPException) as exc_hid:
            await owned_workspace_on_computer(WS, "u-peer", need="write")
        assert exc_hid.value.status_code == 404
# --- DALTON-HOOK(workspace-sharing-settings): end ---
