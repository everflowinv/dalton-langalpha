"""sync3 B11 r4 review fixes: the per-turn keep-updated cut runs as the
workspace's owner, and one link's failure never stops the others.

DALTON-HOOK(colleague-dispatch)
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from src.server.database.artifacts import Artifact
from src.server.services.artifacts import snapshot

_NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)
_OWNER = "u-owner-richard"
_SENDER = "u-colleague-chao"


def _artifact(artifact_id: str) -> Artifact:
    return Artifact(
        artifact_id=artifact_id,
        code="c0de" + artifact_id[-4:],
        workspace_id="ws-1",
        path="report.html",
        created_thread_id=None,
        bound_thread_id=None,
        keep_updated=True,
        latest_seq=3,
        shared_at=_NOW,
        files=("report.html",),
        revision=1,
        created_at=_NOW,
    )


def _captured(*paths: str) -> SimpleNamespace:
    return SimpleNamespace(
        files=[SimpleNamespace(path=p) for p in paths],
        entries=[SimpleNamespace(path=p, reason="page") for p in paths],
        left_out=[],
        total=10,
        blobs={},
        title=None,
    )


@pytest.fixture(autouse=True)
def _publishing_on(monkeypatch):
    """The per-turn cut sits behind dalton.artifact_publishing.enabled."""
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")


@pytest.fixture
def owners_workspace(monkeypatch):
    """The workspace row the cut reads: running, owned by _OWNER — the turn
    that just ended was started by somebody else (a colleague dispatch, the
    research director's dispatch, an rw colleague's own turn)."""
    monkeypatch.setattr(
        snapshot,
        "db_get_workspace",
        create_autospec(
            snapshot.db_get_workspace,
            return_value={
                "workspace_id": "ws-1",
                "user_id": _OWNER,
                "status": "running",
            },
        ),
    )


@pytest.mark.asyncio
async def test_a_colleague_turn_cuts_the_owners_link_as_the_owner(
    monkeypatch, owners_workspace
):
    """The record lock, the blob store and the redaction vault are all
    owner-scoped, so the sender's id must not reach them."""
    artifact = _artifact("00000000-0000-0000-0000-0000000000a1")
    monkeypatch.setattr(
        snapshot.artifacts_db,
        "list_kept_updated",
        create_autospec(
            snapshot.artifacts_db.list_kept_updated, return_value=[artifact]
        ),
    )
    capture = create_autospec(
        snapshot._capture, return_value=_captured(*artifact.files)
    )
    cut = create_autospec(snapshot._cut)
    monkeypatch.setattr(snapshot, "_capture", capture)
    monkeypatch.setattr(snapshot, "_cut", cut)

    await snapshot.keep_updated_after_turn(workspace_id="ws-1", user_id=_SENDER)

    capture.assert_awaited_once()
    assert capture.await_args.kwargs["user_id"] == _OWNER
    cut.assert_awaited_once()
    assert cut.await_args.kwargs["user_id"] == _OWNER
    assert cut.await_args.kwargs["origin"] == "keep_updated"


@pytest.mark.asyncio
async def test_one_links_failure_never_stops_the_workspaces_others(
    monkeypatch, owners_workspace
):
    """A record deleted or refused mid-run is that link's own failure; the
    workspace's other links still get their versions."""
    first = _artifact("00000000-0000-0000-0000-0000000000a1")
    second = _artifact("00000000-0000-0000-0000-0000000000a2")
    monkeypatch.setattr(
        snapshot.artifacts_db,
        "list_kept_updated",
        create_autospec(
            snapshot.artifacts_db.list_kept_updated, return_value=[first, second]
        ),
    )
    keep_one = create_autospec(
        snapshot._keep_one, side_effect=[Exception("record gone"), None]
    )
    monkeypatch.setattr(snapshot, "_keep_one", keep_one)

    # Never raises: the failure is logged for the one link and the loop goes on.
    await snapshot.keep_updated_after_turn(workspace_id="ws-1", user_id=_SENDER)

    assert keep_one.await_count == 2
    assert [c.args[0] for c in keep_one.await_args_list] == [first, second]
    assert keep_one.await_args_list[0].args[1] == _OWNER
