"""Unit tests for Dalton colleague workspace permissions on thread forking."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import create_autospec, patch

import pytest
from fastapi import HTTPException

from src.dalton import settings as dalton_settings
from src.dalton import sharing as dalton_sharing
from src.server.app.threads import fork as fork_app
from src.server.database import thread_fork
from src.server.database.conversation import threads_read
from src.server.services import writer_guard
from src.server.services.forks import materialize as fork_materialize
from src.server.services.forks import service as fork_service
from src.server.services.forks.model import ForkRequest, ThroughTurn
from src.server.services.forks.snapshot import Snapshot
from src.server.services.shared_workspaces import access as sw_access

OWNER_ID = "owner-user-1111"
COLLEAGUE_ID = "colleague-user-2222"
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
SOURCE_THREAD_ID = "22222222-2222-4222-8222-222222222222"


def _colleague_source_meta(
    *,
    user_id: str = COLLEAGUE_ID,
    workspace_owner_id: str = OWNER_ID,
    workspace_id: str = WORKSPACE_ID,
) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "owner_id": user_id,
        "thread_user_id": user_id,
        "workspace_owner_id": workspace_owner_id,
        "workspace_status": "running",
        "workspace_id": workspace_id,
        "metadata": {},
        "is_shared": False,
        "msg_type": "ptc",
        "llm_model": "claude-sonnet-4-5",
        "reasoning_effort": None,
        "fast_mode": None,
        "title": "Colleague thread",
        "created_at": None,
    }


def _make_snapshot() -> Snapshot:
    return Snapshot(
        source_thread_id=SOURCE_THREAD_ID,
        checkpoint_id="cp-1",
        turn_indexes=(0,),
        read_through=0,
        run_horizon=1,
        tip_read="cp-1",
        prefill=None,
    )


def _make_plan() -> thread_fork.LedgerPlan:
    return thread_fork.LedgerPlan(source_thread_id=SOURCE_THREAD_ID)


def _make_fake_conn_cm(conn: Any):
    @asynccontextmanager
    async def _cm():
        yield conn

    return _cm


class _FakeConn:
    @asynccontextmanager
    async def transaction(self):
        yield self


@pytest.mark.asyncio
async def test_fork_refused_after_colleague_sharing_revoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When workspace sharing is revoked (hidden -> 404), forking a colleague's own
    thread in that workspace is rejected before snapshot or any write runs."""
    source_meta = _colleague_source_meta()
    monkeypatch.setattr(
        dalton_settings,
        "is_dalton_supabase_auth_enabled",
        create_autospec(dalton_settings.is_dalton_supabase_auth_enabled, return_value=True),
    )
    monkeypatch.setattr(
        dalton_settings,
        "is_colleague_dispatch_enabled",
        create_autospec(dalton_settings.is_colleague_dispatch_enabled, return_value=True),
    )
    req_ws_access = create_autospec(
        dalton_sharing.require_workspace_access,
        side_effect=HTTPException(status_code=404, detail="Workspace not found"),
    )
    monkeypatch.setattr(dalton_sharing, "require_workspace_access", req_ws_access)

    get_meta = create_autospec(threads_read.get_thread_auth_meta, return_value=source_meta)
    take_snap = create_autospec(fork_service.take_snapshot)
    create_thr = create_autospec(fork_service.create_thread)

    with (
        patch.object(threads_read, "get_thread_auth_meta", get_meta),
        patch.object(
            sw_access,
            "_dalton_external_outside",
            create_autospec(sw_access._dalton_external_outside, return_value=False),
        ),
        patch.object(fork_service, "take_snapshot", take_snap),
        patch.object(fork_service, "create_thread", create_thr),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await fork_service.fork_thread(
                ForkRequest(
                    source_thread_id=SOURCE_THREAD_ID,
                    cut=ThroughTurn(0),
                    user_id=COLLEAGUE_ID,
                )
            )

    assert exc_info.value.status_code == 404
    req_ws_access.assert_awaited_once_with(
        WORKSPACE_ID, COLLEAGUE_ID, channel="web", need="write", conn=None
    )
    take_snap.assert_not_awaited()
    create_thr.assert_not_awaited()


@pytest.mark.asyncio
async def test_fork_refused_when_colleague_workspace_downgraded_to_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Downgrading a colleague's workspace sharing to read-only (403) refuses fork
    without writing a thread row."""
    source_meta = _colleague_source_meta()
    monkeypatch.setattr(
        dalton_settings,
        "is_dalton_supabase_auth_enabled",
        create_autospec(dalton_settings.is_dalton_supabase_auth_enabled, return_value=True),
    )
    monkeypatch.setattr(
        dalton_settings,
        "is_colleague_dispatch_enabled",
        create_autospec(dalton_settings.is_colleague_dispatch_enabled, return_value=True),
    )
    req_ws_access = create_autospec(
        dalton_sharing.require_workspace_access,
        side_effect=HTTPException(status_code=403, detail="Forbidden"),
    )
    monkeypatch.setattr(dalton_sharing, "require_workspace_access", req_ws_access)

    take_snap = create_autospec(fork_service.take_snapshot)
    create_thr = create_autospec(fork_service.create_thread)

    with (
        patch.object(
            threads_read,
            "get_thread_auth_meta",
            create_autospec(threads_read.get_thread_auth_meta, return_value=source_meta),
        ),
        patch.object(
            sw_access,
            "_dalton_external_outside",
            create_autospec(sw_access._dalton_external_outside, return_value=False),
        ),
        patch.object(fork_service, "take_snapshot", take_snap),
        patch.object(fork_service, "create_thread", create_thr),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await fork_service.fork_thread(
                ForkRequest(
                    source_thread_id=SOURCE_THREAD_ID,
                    cut=ThroughTurn(0),
                    user_id=COLLEAGUE_ID,
                )
            )

    assert exc_info.value.status_code == 403
    take_snap.assert_not_awaited()
    create_thr.assert_not_awaited()


@pytest.mark.asyncio
async def test_fork_refused_when_colleague_dispatch_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When colleague_dispatch is disabled, forking in a colleague's workspace
    checks read access first and raises 403 matching turn_runtime."""
    source_meta = _colleague_source_meta()
    monkeypatch.setattr(
        dalton_settings,
        "is_dalton_supabase_auth_enabled",
        create_autospec(dalton_settings.is_dalton_supabase_auth_enabled, return_value=True),
    )
    monkeypatch.setattr(
        dalton_settings,
        "is_colleague_dispatch_enabled",
        create_autospec(dalton_settings.is_colleague_dispatch_enabled, return_value=False),
    )
    req_ws_access = create_autospec(
        dalton_sharing.require_workspace_access,
        return_value={"workspace_id": WORKSPACE_ID, "user_id": OWNER_ID},
    )
    monkeypatch.setattr(dalton_sharing, "require_workspace_access", req_ws_access)

    with (
        patch.object(
            threads_read,
            "get_thread_auth_meta",
            create_autospec(threads_read.get_thread_auth_meta, return_value=source_meta),
        ),
        patch.object(
            sw_access,
            "_dalton_external_outside",
            create_autospec(sw_access._dalton_external_outside, return_value=False),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await fork_service.fork_thread(
                ForkRequest(
                    source_thread_id=SOURCE_THREAD_ID,
                    cut=ThroughTurn(0),
                    user_id=COLLEAGUE_ID,
                )
            )

    assert exc_info.value.status_code == 403
    req_ws_access.assert_awaited_once_with(
        WORKSPACE_ID, COLLEAGUE_ID, channel="web", need="read", conn=None
    )


@pytest.mark.asyncio
async def test_fork_with_write_permission_records_caller_as_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the colleague still has write access to the colleague workspace,
    the forked thread row records user_id=caller (starter) and dispatched_by."""
    source_meta = _colleague_source_meta()
    monkeypatch.setattr(
        dalton_settings,
        "is_dalton_supabase_auth_enabled",
        create_autospec(dalton_settings.is_dalton_supabase_auth_enabled, return_value=True),
    )
    monkeypatch.setattr(
        dalton_settings,
        "is_colleague_dispatch_enabled",
        create_autospec(dalton_settings.is_colleague_dispatch_enabled, return_value=True),
    )
    req_ws_access = create_autospec(
        dalton_sharing.require_workspace_access,
        return_value={"workspace_id": WORKSPACE_ID, "user_id": OWNER_ID},
    )
    monkeypatch.setattr(dalton_sharing, "require_workspace_access", req_ws_access)
    dispatched_meta = {
        "user_id": COLLEAGUE_ID,
        "name": "Colleague",
        "email": "colleague@example.com",
        "channel": "web",
    }
    monkeypatch.setattr(
        dalton_sharing,
        "build_dispatched_by_meta",
        create_autospec(dalton_sharing.build_dispatched_by_meta, return_value=dispatched_meta),
    )

    fake_conn = _FakeConn()
    create_thr = create_autospec(fork_service.create_thread, return_value={"thread_index": 2})

    with (
        patch.object(
            threads_read,
            "get_thread_auth_meta",
            create_autospec(threads_read.get_thread_auth_meta, return_value=source_meta),
        ),
        patch.object(
            threads_read,
            "read_thread_subagents_allowed",
            create_autospec(threads_read.read_thread_subagents_allowed, return_value=None),
        ),
        patch.object(
            sw_access,
            "_dalton_external_outside",
            create_autospec(sw_access._dalton_external_outside, return_value=False),
        ),
        patch.object(
            fork_service,
            "take_snapshot",
            create_autospec(fork_service.take_snapshot, return_value=_make_snapshot()),
        ),
        patch.object(
            fork_service,
            "_plan",
            create_autospec(fork_service._plan, return_value=_make_plan()),
        ),
        patch.object(
            writer_guard,
            "guard_enabled",
            create_autospec(writer_guard.guard_enabled, return_value=True),
        ),
        patch.object(fork_service, "get_db_connection", _make_fake_conn_cm(fake_conn)),
        patch.object(
            fork_service,
            "_hold_source",
            create_autospec(fork_service._hold_source, return_value=None),
        ),
        patch.object(
            thread_fork,
            "check_source",
            create_autospec(thread_fork.check_source, return_value=None),
        ),
        patch.object(
            fork_service,
            "_write_checkpoints",
            create_autospec(fork_service._write_checkpoints, return_value=None),
        ),
        patch.object(fork_service, "create_thread", create_thr),
        patch.object(
            thread_fork,
            "write_tasks",
            create_autospec(thread_fork.write_tasks, return_value=None),
        ),
        patch.object(
            thread_fork,
            "finish_thread",
            create_autospec(thread_fork.finish_thread, return_value=None),
        ),
        patch.object(
            thread_fork,
            "hold_thread_row",
            create_autospec(thread_fork.hold_thread_row, return_value=None),
        ),
        patch.object(
            fork_materialize,
            "read_cut",
            create_autospec(fork_materialize.read_cut, return_value=None),
        ),
    ):
        result = await fork_service.fork_thread(
            ForkRequest(
                source_thread_id=SOURCE_THREAD_ID,
                cut=ThroughTurn(0),
                user_id=COLLEAGUE_ID,
                platform="web",
            )
        )

    assert result.thread_id
    assert req_ws_access.await_count == 2
    create_thr.assert_awaited_once()
    kwargs = create_thr.await_args.kwargs
    assert kwargs["user_id"] == COLLEAGUE_ID
    assert kwargs["created_by"] is None
    assert kwargs["metadata"]["dispatched_by"] == dispatched_meta
    assert kwargs["metadata"]["fork"]["by"] == COLLEAGUE_ID


@pytest.mark.asyncio
async def test_fork_aborts_if_colleague_sharing_revoked_mid_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If sharing is revoked between the initial check and transaction commit,
    the commit-time re-check raises and prevents committing the fork."""
    source_meta = _colleague_source_meta()
    monkeypatch.setattr(
        dalton_settings,
        "is_dalton_supabase_auth_enabled",
        create_autospec(dalton_settings.is_dalton_supabase_auth_enabled, return_value=True),
    )
    monkeypatch.setattr(
        dalton_settings,
        "is_colleague_dispatch_enabled",
        create_autospec(dalton_settings.is_colleague_dispatch_enabled, return_value=True),
    )
    req_ws_access = create_autospec(
        dalton_sharing.require_workspace_access,
        side_effect=[
            {"workspace_id": WORKSPACE_ID, "user_id": OWNER_ID},
            HTTPException(status_code=404, detail="Workspace not found"),
        ],
    )
    monkeypatch.setattr(dalton_sharing, "require_workspace_access", req_ws_access)
    monkeypatch.setattr(
        dalton_sharing,
        "build_dispatched_by_meta",
        create_autospec(dalton_sharing.build_dispatched_by_meta, return_value=None),
    )

    fake_conn = _FakeConn()

    with (
        patch.object(
            threads_read,
            "get_thread_auth_meta",
            create_autospec(threads_read.get_thread_auth_meta, return_value=source_meta),
        ),
        patch.object(
            threads_read,
            "read_thread_subagents_allowed",
            create_autospec(threads_read.read_thread_subagents_allowed, return_value=None),
        ),
        patch.object(
            sw_access,
            "_dalton_external_outside",
            create_autospec(sw_access._dalton_external_outside, return_value=False),
        ),
        patch.object(
            fork_service,
            "take_snapshot",
            create_autospec(fork_service.take_snapshot, return_value=_make_snapshot()),
        ),
        patch.object(
            fork_service,
            "_plan",
            create_autospec(fork_service._plan, return_value=_make_plan()),
        ),
        patch.object(
            writer_guard,
            "guard_enabled",
            create_autospec(writer_guard.guard_enabled, return_value=True),
        ),
        patch.object(fork_service, "get_db_connection", _make_fake_conn_cm(fake_conn)),
        patch.object(
            fork_service,
            "_hold_source",
            create_autospec(fork_service._hold_source, return_value=None),
        ),
        patch.object(
            thread_fork,
            "check_source",
            create_autospec(thread_fork.check_source, return_value=None),
        ),
        patch.object(
            fork_service,
            "_write_checkpoints",
            create_autospec(fork_service._write_checkpoints, return_value=None),
        ),
        patch.object(
            fork_service,
            "create_thread",
            create_autospec(fork_service.create_thread, return_value={"thread_index": 2}),
        ),
        patch.object(
            thread_fork,
            "write_tasks",
            create_autospec(thread_fork.write_tasks, return_value=None),
        ),
        patch.object(
            thread_fork,
            "finish_thread",
            create_autospec(thread_fork.finish_thread, return_value=None),
        ),
        patch.object(
            thread_fork,
            "hold_thread_row",
            create_autospec(thread_fork.hold_thread_row, return_value=None),
        ),
        patch.object(
            fork_materialize,
            "read_cut",
            create_autospec(fork_materialize.read_cut, return_value=None),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await fork_service.fork_thread(
                ForkRequest(
                    source_thread_id=SOURCE_THREAD_ID,
                    cut=ThroughTurn(0),
                    user_id=COLLEAGUE_ID,
                )
            )

    assert exc_info.value.status_code == 404
    assert req_ws_access.await_count == 2


@pytest.mark.asyncio
async def test_fork_admit_rejects_revoked_colleague_before_credit_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In platform mode, fork._admit checks colleague workspace write permission
    before hitting the credit gate."""
    from src.server.dependencies import usage_limits

    source_meta = _colleague_source_meta()
    monkeypatch.setattr(fork_app, "HOST_MODE", "platform")
    monkeypatch.setattr(
        dalton_settings,
        "is_dalton_supabase_auth_enabled",
        create_autospec(dalton_settings.is_dalton_supabase_auth_enabled, return_value=True),
    )
    monkeypatch.setattr(
        dalton_settings,
        "is_colleague_dispatch_enabled",
        create_autospec(dalton_settings.is_colleague_dispatch_enabled, return_value=True),
    )
    req_ws_access = create_autospec(
        dalton_sharing.require_workspace_access,
        side_effect=HTTPException(status_code=404, detail="Workspace not found"),
    )
    monkeypatch.setattr(dalton_sharing, "require_workspace_access", req_ws_access)
    enforce_credit = create_autospec(usage_limits.enforce_credit_limit)

    with (
        patch.object(
            threads_read,
            "get_thread_auth_meta",
            create_autospec(threads_read.get_thread_auth_meta, return_value=source_meta),
        ),
        patch.object(
            sw_access,
            "_dalton_external_outside",
            create_autospec(sw_access._dalton_external_outside, return_value=False),
        ),
        patch.object(usage_limits, "enforce_credit_limit", enforce_credit),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await fork_app._admit(SOURCE_THREAD_ID, COLLEAGUE_ID, "web")

    assert exc_info.value.status_code == 404
    enforce_credit.assert_not_awaited()


@pytest.mark.asyncio
async def test_colleague_fork_succeeds_with_single_connection_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a DB pool of size 1, a colleague with rw access can fork their thread
    without deadlocking/timing out on a second pool checkout inside the fork
    transaction."""
    import psycopg.pq

    from src.config import settings as app_settings
    from src.dalton import invites as dalton_invites
    from src.server.database import pool as db_pool

    colleague_email = "colleague@example.com"
    owner_email = "owner@example.com"
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setenv("DALTON_ALLOWED_EMAILS", f"{owner_email},{colleague_email}")
    monkeypatch.setenv("DALTON_COLLEAGUE_DISPATCH_ENABLED", "1")
    dalton_invites.invalidate_team_members_cache()

    source_meta = _colleague_source_meta()
    users_by_id = {
        COLLEAGUE_ID: {
            "user_id": COLLEAGUE_ID,
            "email": colleague_email,
            "name": "Colleague User",
        },
        OWNER_ID: {
            "user_id": OWNER_ID,
            "email": owner_email,
            "name": "Owner User",
        },
    }
    workspace_row = {
        "workspace_id": WORKSPACE_ID,
        "user_id": OWNER_ID,
        "name": "Owner Workspace",
        "status": "running",
        "is_home": False,
        "config": {},
    }
    sharing_row = {
        "workspace_id": WORKSPACE_ID,
        "cos_access": "rw",
        "web_access": "rw",
        "updated_by": OWNER_ID,
        "updated_at": None,
    }

    class _FakeCursor:
        def __init__(self) -> None:
            self._one: Any = None
            self._all: list[Any] = []

        async def __aenter__(self) -> _FakeCursor:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def execute(self, sql: str, params: Any = None) -> None:
            norm = " ".join(sql.lower().split())
            if "from conversation_threads" in norm and "join workspaces" in norm:
                self._one = dict(source_meta)
            elif "from users" in norm:
                uid = str(params[-1]) if params else ""
                self._one = dict(users_by_id[uid]) if uid in users_by_id else None
            elif "from workspaces" in norm:
                self._one = dict(workspace_row)
            elif "from dalton_workspace_sharing" in norm:
                self._one = dict(sharing_row)
            elif "from dalton_members" in norm:
                self._all = []
            else:
                self._one = None
                self._all = []

        async def fetchone(self) -> Any:
            return self._one

        async def fetchall(self) -> list[Any]:
            return list(self._all)

    class _FakeConnInfo:
        transaction_status = psycopg.pq.TransactionStatus.IDLE

    class _SingleSlotConn:
        info = _FakeConnInfo()

        def cursor(self, **kwargs: Any) -> _FakeCursor:
            return _FakeCursor()

        @asynccontextmanager
        async def transaction(self):
            yield self

    class _SingleSlotPool:
        closed = False

        def __init__(self) -> None:
            self.in_use = 0
            self.max_concurrent = 0

        @asynccontextmanager
        async def connection(self, timeout: float = 10):
            if self.in_use >= 1:
                raise TimeoutError(
                    "Single-connection pool exhausted: nested connection checkout inside transaction"
                )
            self.in_use += 1
            self.max_concurrent = max(self.max_concurrent, self.in_use)
            try:
                yield _SingleSlotConn()
            finally:
                self.in_use -= 1

    single_pool = _SingleSlotPool()
    monkeypatch.setattr(
        db_pool,
        "get_or_create_pool",
        create_autospec(db_pool.get_or_create_pool, return_value=single_pool),
    )

    create_thr = create_autospec(fork_service.create_thread, return_value={"thread_index": 2})

    with (
        patch.object(
            threads_read,
            "read_thread_subagents_allowed",
            create_autospec(threads_read.read_thread_subagents_allowed, return_value=None),
        ),
        patch.object(
            fork_service,
            "take_snapshot",
            create_autospec(fork_service.take_snapshot, return_value=_make_snapshot()),
        ),
        patch.object(
            fork_service,
            "_plan",
            create_autospec(fork_service._plan, return_value=_make_plan()),
        ),
        patch.object(
            writer_guard,
            "guard_enabled",
            create_autospec(writer_guard.guard_enabled, return_value=True),
        ),
        patch.object(
            fork_service,
            "_hold_source",
            create_autospec(fork_service._hold_source, return_value=None),
        ),
        patch.object(
            thread_fork,
            "check_source",
            create_autospec(thread_fork.check_source, return_value=None),
        ),
        patch.object(
            fork_service,
            "_write_checkpoints",
            create_autospec(fork_service._write_checkpoints, return_value=None),
        ),
        patch.object(fork_service, "create_thread", create_thr),
        patch.object(
            thread_fork,
            "write_tasks",
            create_autospec(thread_fork.write_tasks, return_value=None),
        ),
        patch.object(
            thread_fork,
            "finish_thread",
            create_autospec(thread_fork.finish_thread, return_value=None),
        ),
        patch.object(
            thread_fork,
            "hold_thread_row",
            create_autospec(thread_fork.hold_thread_row, return_value=None),
        ),
        patch.object(
            fork_materialize,
            "read_cut",
            create_autospec(fork_materialize.read_cut, return_value=None),
        ),
    ):
        result = await fork_service.fork_thread(
            ForkRequest(
                source_thread_id=SOURCE_THREAD_ID,
                cut=ThroughTurn(0),
                user_id=COLLEAGUE_ID,
                platform="web",
            )
        )

    assert result.thread_id
    assert single_pool.max_concurrent == 1
    assert single_pool.in_use == 0
    create_thr.assert_awaited_once()
    kwargs = create_thr.await_args.kwargs
    assert kwargs["user_id"] == COLLEAGUE_ID
    assert kwargs["metadata"]["dispatched_by"] == {
        "user_id": COLLEAGUE_ID,
        "name": "Colleague User",
        "email": colleague_email,
        "channel": "web",
    }
