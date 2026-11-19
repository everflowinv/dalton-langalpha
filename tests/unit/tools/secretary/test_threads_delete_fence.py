"""Secretary thread deletion goes through the exclusive-T mutation fence.

Review F2 (v4 2.4): the agent-facing delete must take the same
``ThreadMutationRunner.exclusive(thread_id, "delete")`` fence as the HTTP
endpoint and run the DELETE on the fenced session — an unfenced
``delete_thread`` would cascade away a live run's ledger rows out from under
a writer on any worker.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest

from src.server.database.conversation.threads_read import get_thread_by_id
from src.server.database.conversation.threads_write import delete_thread
from src.server.database.runs import command_runs as cr_db
from src.server.services.thread_mutation import (
    MutationConflict,
    MutationUnavailable,
    ThreadMutationRunner,
)
from src.tools.secretary import tools as sec_tools
from src.utils.cache.redis_cache import RedisCacheClient, get_cache_client

_FENCED_CONN = object()


def _runner(enter_exc: Exception | None = None) -> ThreadMutationRunner:
    @asynccontextmanager
    async def _exclusive(thread_id, verb):
        assert verb == "delete"
        if enter_exc is not None:
            raise enter_exc
        yield SimpleNamespace(conn=_FENCED_CONN)

    runner = create_autospec(ThreadMutationRunner, instance=True)
    runner.exclusive = _exclusive
    return runner


def _content(cmd) -> dict:
    return json.loads(cmd.update["messages"][0].content)


def _patches(runner, delete_mock):
    disabled_cache = create_autospec(RedisCacheClient, instance=True)
    disabled_cache.enabled = False
    disabled_cache.client = None
    return (
        patch.object(
            sec_tools,
            "verify_thread_owner",
            create_autospec(sec_tools.verify_thread_owner, return_value=None),
        ),
        patch.object(
            sec_tools,
            "hitl_confirm",
            create_autospec(sec_tools.hitl_confirm, return_value=(True, None)),
        ),
        patch(
            "src.server.database.conversation.threads_read.get_thread_by_id",
            create_autospec(get_thread_by_id, return_value=None),
        ),
        patch(
            "src.server.services.commands.control.cr_db.list_running",
            create_autospec(cr_db.list_running, return_value=[]),
        ),
        patch(
            "src.server.services.thread_mutation.ThreadMutationRunner.get_instance",
            create_autospec(ThreadMutationRunner.get_instance, return_value=runner),
        ),
        patch(
            "src.server.database.conversation.threads_write.delete_thread",
            delete_mock,
        ),
        patch(
            "src.utils.cache.redis_cache.get_cache_client",
            create_autospec(get_cache_client, return_value=disabled_cache),
        ),
    )


@pytest.mark.asyncio
async def test_delete_runs_on_the_fenced_session():
    delete_mock = create_autospec(delete_thread)
    p1, p2, p3, p4, p5, p6, p7 = _patches(_runner(), delete_mock)
    with p1, p2, p3, p4, p5, p6, p7:
        cmd = await sec_tools.threads_delete("u-1", "t-1", "tc-1")

    delete_mock.assert_awaited_once_with("t-1", conn=_FENCED_CONN)
    assert _content(cmd)["success"] is True


@pytest.mark.asyncio
async def test_delete_refused_while_thread_is_busy():
    """MutationConflict (live run / rival mutation holds the guard) surfaces
    as an error command and the DELETE never executes."""
    delete_mock = create_autospec(delete_thread)
    conflict = MutationConflict("busy_thread", "delete", "Thread has an active run")
    p1, p2, p3, p4, p5, p6, p7 = _patches(_runner(enter_exc=conflict), delete_mock)
    with p1, p2, p3, p4, p5, p6, p7:
        cmd = await sec_tools.threads_delete("u-1", "t-1", "tc-1")

    delete_mock.assert_not_awaited()
    body = _content(cmd)
    assert body["success"] is False
    assert body["error"] == "Thread has an active run"


@pytest.mark.asyncio
async def test_delete_unavailable_surfaces_retriable_error():
    delete_mock = create_autospec(delete_thread)
    p1, p2, p3, p4, p5, p6, p7 = _patches(
        _runner(enter_exc=MutationUnavailable("no session")), delete_mock
    )
    with p1, p2, p3, p4, p5, p6, p7:
        cmd = await sec_tools.threads_delete("u-1", "t-1", "tc-1")

    delete_mock.assert_not_awaited()
    body = _content(cmd)
    assert body["success"] is False
    assert "retry" in body["error"].lower()
