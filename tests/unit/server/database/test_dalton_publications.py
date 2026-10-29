"""Unit tests for workspace status guard in src/dalton/database/publications.py."""

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from unittest.mock import create_autospec, patch

import psycopg
import pytest

from src.dalton.database.publications import (
    WorkspaceUnavailableForPublishError,
    insert_publication,
)


def _make_db_doubles(fetchone_side_effect):
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    cur.fetchone.side_effect = list(fetchone_side_effect)
    conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _tx():
        yield

    @asynccontextmanager
    async def _cursor(**_kwargs):
        yield cur

    @asynccontextmanager
    async def _get_db():
        yield conn

    conn.transaction.side_effect = _tx
    conn.cursor.side_effect = _cursor
    return _get_db, cur


def _sample_kwargs() -> dict:
    return {
        "publish_request_id": "req-1",
        "user_id": "user-1",
        "workspace_id": "ws-1",
        "ticker": "NVDA",
        "kind": "deep_dive",
        "title": "NVDA Report",
        "published_date": date(2026, 10, 28),
        "files": [],
        "file_blobs": [],
        "bundle_sha256": "a" * 64,
        "sources": [],
        "audit_summary": {},
        "reviewer": {},
        "approver_user_id": "user-1",
        "approved_at": datetime(2026, 10, 28, tzinfo=timezone.utc),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ws_row",
    [
        None,
        {"workspace_id": "ws-1", "status": "deleted"},
        {"workspace_id": "ws-1", "status": "DELETED"},
    ],
)
async def test_insert_publication_rejects_missing_or_deleted_workspace(ws_row):
    get_db, cur = _make_db_doubles([ws_row])
    with patch("src.dalton.database.publications.get_db_connection", side_effect=get_db):
        with pytest.raises(WorkspaceUnavailableForPublishError, match="已删除"):
            await insert_publication(**_sample_kwargs())
    assert cur.execute.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "stopped", "stopping", "error"])
async def test_insert_publication_allows_non_deleted_workspace_statuses(status):
    pub_row = {
        "publication_id": "pub-1",
        "publish_request_id": "req-1",
        "workspace_id": "ws-1",
        "published_date": date(2026, 10, 28),
        "approved_at": datetime(2026, 10, 28, tzinfo=timezone.utc),
    }
    get_db, cur = _make_db_doubles(
        [
            {"workspace_id": "ws-1", "status": status},
            None,
            pub_row,
        ]
    )
    with patch("src.dalton.database.publications.get_db_connection", side_effect=get_db):
        out = await insert_publication(**_sample_kwargs())
    assert out["publication_id"] == "pub-1"
    assert cur.execute.await_count == 4
