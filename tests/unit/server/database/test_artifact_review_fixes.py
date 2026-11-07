"""sync3 B11 r4 review fixes on the artifact routing and version rules.

- Comment destinations are the *thread's* to allow, not the workspace's:
  since d020 a thread in the owner's workspace can belong to a colleague,
  and comments are sent into the thread, so only its owner may pick it.
- A legacy link's first cut keeps the keep-updated setting as the owner left
  it: migration 101 already backfilled TRUE, so the owner's later "off" must
  survive the link's first view.

DALTON-HOOK(colleague-dispatch), DALTON-HOOK(legacy-keeps-owner-setting)
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, create_autospec

import pytest

from src.server.database import artifacts
from src.server.database.artifacts import routing, versions

_A = "00000000-0000-0000-0000-0000000000a1"
_T = "00000000-0000-0000-0000-0000000000b1"
_U = "00000000-0000-0000-0000-0000000000e1"
_W = "00000000-0000-0000-0000-0000000000f1"
_HOME = "00000000-0000-0000-0000-0000000000ff"
_NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)


def _flat(sql: str) -> str:
    return re.sub(r"\s+", " ", sql)


def _sql(mock_cursor) -> list[str]:
    return [_flat(c.args[0]) for c in mock_cursor.execute.call_args_list]


def _row(**overrides) -> dict:
    row = {
        "artifact_id": _A,
        "code": "c0de",
        "workspace_id": _W,
        "path": "r.html",
        "created_thread_id": None,
        "bound_thread_id": None,
        "keep_updated": False,
        "latest_seq": 0,
        "shared_at": None,
        "files": None,
        "revision": 0,
        "created_at": _NOW,
        "legacy_refused": None,
        "loaded_files": [],
        "left_out_files": [],
        "comment_destination": None,
    }
    return {**row, **overrides}


def _file(text: str) -> artifacts.VersionFile:
    data = text.encode()
    return artifacts.VersionFile(
        "r.html", hashlib.sha256(data).hexdigest(), len(data), "text/html", data
    )


@pytest.fixture(autouse=True)
def _own_home(monkeypatch):
    monkeypatch.setattr(
        "src.server.database.home_workspace.get_home_workspace_id",
        lambda user_id: _HOME,
    )


# --- comments may only go to the sender's own thread ------------------------


@pytest.mark.asyncio
async def test_require_destination_checks_the_thread_owner_not_the_workspaces(
    artifacts_db, mock_cursor
):
    """A colleague's thread in the owner's workspace is not a destination: the
    gate reads COALESCE(t.user_id, tw.user_id) — the thread's owner — against
    the sender, the same rule replay_rows reads the owner with."""
    mock_cursor.fetchone = AsyncMock(return_value=None)

    with pytest.raises(routing.DestinationUnavailable):
        await routing.require_destination(
            mock_cursor, user_id=_U, workspace_id=_W, thread_id=_T
        )

    (sql,) = _sql(mock_cursor)
    assert "COALESCE(t.user_id, tw.user_id) = %(user_id)s" in sql


@pytest.mark.asyncio
async def test_destinations_candidates_filter_on_the_thread_owner(
    artifacts_db, mock_cursor
):
    """The candidate query drops a thread its sender does not own, so the
    colleague's created thread never comes back as a usable destination and
    the default falls through to a new thread."""
    mock_cursor.fetchone = AsyncMock(return_value=_row(created_thread_id=_T))
    mock_cursor.fetchall = AsyncMock(return_value=[])

    found = await routing.destinations(user_id=_U, artifact_id=_A)

    assert found.creator is None
    assert found.bound is None
    assert found.default == (None, "new")
    thread_sql = _sql(mock_cursor)[-1]
    assert "COALESCE(t.user_id, tw.user_id) = %(user_id)s" in thread_sql


# --- a legacy first cut keeps the owner's keep-updated setting --------------


@pytest.mark.asyncio
async def test_a_legacy_first_cut_keeps_the_owners_keep_updated_off(
    artifacts_db, mock_cursor, monkeypatch
):
    """Migration 101 backfilled the pre-versions links to keep_updated=TRUE;
    an owner who turned it off after the migration must not have it switched
    back on by the link's first view."""
    after = _row(keep_updated=False, latest_seq=1, shared_at=_NOW, revision=1)

    async def fetchone():
        return after

    monkeypatch.setattr(
        versions,
        "lock_owned",
        create_autospec(
            versions.lock_owned,
            return_value=artifacts.artifact_from_row(
                _row(keep_updated=False, shared_at=_NOW)
            ),
        ),
    )
    monkeypatch.setattr(
        versions,
        "_prune_kept_updated",
        create_autospec(versions._prune_kept_updated),
    )
    mock_cursor.fetchone = fetchone

    result = await artifacts.publish_version(
        user_id=_U,
        artifact_id=_A,
        origin="legacy",
        files=[_file("first view")],
    )

    assert result.cut is True
    updates = [i for i, s in enumerate(_sql(mock_cursor)) if "UPDATE share_links" in s]
    assert updates, "the record's row must move with the cut"
    update_params = mock_cursor.execute.call_args_list[updates[0]].args[1]
    # None falls to the row's current value in the UPDATE's COALESCE — the
    # owner's keep_updated=FALSE survives the link's first view.
    assert update_params["keep_updated"] is None
    assert result.artifact.keep_updated is False
