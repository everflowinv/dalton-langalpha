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
            "_copy_chains",
            create_autospec(fork_service._copy_chains, return_value=None),
        ),
        patch.object(fork_service, "create_thread", create_thr),
        patch.object(
            thread_fork,
            "write_ledger",
            create_autospec(thread_fork.write_ledger, return_value=1),
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
            fork_service,
            "_after_commit",
            create_autospec(fork_service._after_commit, return_value=None),
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

    assert result.turns_copied == 1
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
    after_commit = create_autospec(fork_service._after_commit)

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
            "_copy_chains",
            create_autospec(fork_service._copy_chains, return_value=None),
        ),
        patch.object(
            fork_service,
            "create_thread",
            create_autospec(fork_service.create_thread, return_value={"thread_index": 2}),
        ),
        patch.object(
            thread_fork,
            "write_ledger",
            create_autospec(thread_fork.write_ledger, return_value=1),
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
        patch.object(fork_service, "_after_commit", after_commit),
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
    after_commit.assert_not_called()


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
