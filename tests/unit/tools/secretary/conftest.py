"""Fixtures and fakes shared by the secretary tool suite.

The fakes below, imported via ``from .conftest import ...``, stand in for the
workspace manager and for the background dispatch POST.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

from src.server.database.workspace import get_live_workspace_names
from src.server.services.history.committed import committed_texts


@pytest.fixture(autouse=True)
def _no_committed_texts():
    """No test reaches the stored slices or the checkpoints: no turn has a
    committed text to read unless a test patches one in."""
    with patch(
        "src.server.services.history.committed.committed_texts",
        create_autospec(committed_texts, return_value={}),
    ):
        yield


@pytest.fixture(autouse=True)
def _no_live_workspaces():
    # DALTON-HOOK(workspace-duplicate-guard): a create finds no duplicate
    # unless a test names the workspaces the user already holds.
    """No workspace exists to duplicate: every create starts from a clean list."""
    with patch(
        "src.server.database.workspace.get_live_workspace_names",
        create_autospec(get_live_workspace_names, return_value=[]),
    ):
        yield


def workspace_manager(row: dict) -> MagicMock:
    """A ``WorkspaceManager`` whose create returns ``row``."""
    mgr = MagicMock()
    mgr.create_workspace = AsyncMock(return_value=row)
    return mgr


class FakeResp:
    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self._body = body if body is not None else {"status": "dispatched"}

    async def __aenter__(self) -> "FakeResp":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def json(self) -> dict:
        return self._body


class FakeSession:
    """An ``aiohttp.ClientSession`` whose post answers ``resp`` or raises
    ``post_exc``."""

    def __init__(
        self, resp: FakeResp | None = None, post_exc: Exception | None = None
    ) -> None:
        self._resp = resp
        self._post_exc = post_exc

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    def post(self, *_args, **_kwargs) -> FakeResp:
        if self._post_exc is not None:
            raise self._post_exc
        return self._resp
