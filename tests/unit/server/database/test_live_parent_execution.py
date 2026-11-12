"""A manual trigger locks the parent before it inserts a pending execution."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock, create_autospec

import pytest

import src.server.database.automation_executions as exec_db
from src.server.services.automation_scheduler import AutomationScheduler
from src.server.services.automations.lifecycle import trigger_automation

AUTO = "6f1c2a9e-3b4d-4e8f-9a1b-2c3d4e5f6a7b"
USER = "user-1"


class _Cursor:
    def __init__(self, parent):
        self.parent = parent
        self.queries: list[str] = []
        self._one = None

    async def execute(self, query, params=None):
        text = " ".join(str(query).split())
        self.queries.append(text)
        self._one = self.parent if "FOR UPDATE" in text else None

    async def fetchone(self):
        return self._one

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Conn:
    def __init__(self, parent):
        self.cursor_obj = _Cursor(parent)
        self.transactions = 0

    def cursor(self, **_kwargs):
        return self.cursor_obj

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self_tx):
                conn.transactions += 1
                return self_tx

            async def __aexit__(self_tx, *_args):
                return False

        return _Tx()


def _install(monkeypatch, parent):
    conn = _Conn(parent)

    @asynccontextmanager
    async def _db(*_args, **_kwargs):
        yield conn

    monkeypatch.setattr(exec_db, "get_db_connection", _db)
    return conn


@pytest.mark.asyncio
async def test_a_deleted_parent_inserts_nothing(monkeypatch) -> None:
    conn = _install(
        monkeypatch,
        {"status": "active", "deleted_at": datetime(2026, 11, 1, tzinfo=UTC), "user_id": USER},
    )
    with pytest.raises(ValueError, match="Automation not found"):
        await exec_db.create_execution_for_live_parent(
            AUTO, USER, datetime(2026, 11, 11, tzinfo=UTC), "server-1"
        )
    assert conn.transactions == 1
    assert len(conn.cursor_obj.queries) == 1
    assert "FOR UPDATE" in conn.cursor_obj.queries[0]
    assert "INSERT" not in conn.cursor_obj.queries[0]


@pytest.mark.asyncio
async def test_a_live_parent_inserts_in_the_same_transaction(monkeypatch) -> None:
    parent = {"status": "paused", "deleted_at": None, "user_id": USER, "automation_id": AUTO}
    conn = _install(monkeypatch, parent)
    execution_id, locked = await exec_db.create_execution_for_live_parent(
        AUTO, USER, datetime(2026, 11, 11, tzinfo=UTC), "server-1"
    )
    assert locked["status"] == "paused"
    assert conn.transactions == 1
    assert len(conn.cursor_obj.queries) == 2
    assert "FOR UPDATE" in conn.cursor_obj.queries[0]
    assert "INSERT INTO automation_executions" in conn.cursor_obj.queries[1]
    assert "'pending'" in conn.cursor_obj.queries[1]
    assert execution_id


@pytest.mark.asyncio
async def test_trigger_uses_the_locked_helper_and_a_deleted_parent_dispatches_nothing(
    monkeypatch,
) -> None:
    created = create_autospec(exec_db.create_execution, side_effect=AssertionError("orphan insert"))
    dispatched = MagicMock()

    async def _locked(automation_id, user_id, scheduled_at, server_id):
        return "exec-locked", {
            "automation_id": automation_id,
            "user_id": user_id,
            "status": "active",
        }

    real_locked_insert = exec_db.create_execution_for_live_parent
    locked = create_autospec(real_locked_insert, side_effect=_locked)
    monkeypatch.setattr(exec_db, "create_execution", created)
    import src.server.services.automations.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle.exec_db, "create_execution_for_live_parent", locked)
    monkeypatch.setattr(lifecycle.exec_db, "create_execution", created)
    scheduler = MagicMock()
    scheduler.server_id = "server-1"
    scheduler.dispatch = dispatched

    def _instance(_cls=None):
        return scheduler

    monkeypatch.setattr(AutomationScheduler, "get_instance", _instance)

    result = await trigger_automation(AUTO, USER)
    assert result["execution_id"] == "exec-locked"
    locked.assert_awaited_once()
    created.assert_not_awaited()
    dispatched.assert_called_once()

    async def _gone(*_args, **_kwargs):
        raise ValueError("Automation not found")

    monkeypatch.setattr(
        lifecycle.exec_db,
        "create_execution_for_live_parent",
        create_autospec(real_locked_insert, side_effect=_gone),
    )
    dispatched.reset_mock()
    with pytest.raises(ValueError, match="Automation not found"):
        await trigger_automation(AUTO, USER)
    dispatched.assert_not_called()
    created.assert_not_awaited()


@pytest.mark.asyncio
async def test_system_job_fallback_uses_the_locked_helper(monkeypatch) -> None:
    import src.dalton.autonomy.system_jobs as system_jobs_mod
    import src.dalton.database.automation_recycle as recycle_mod

    async def _missing(_automation_id, _user_id):
        raise ValueError("Automation not found")

    async def _locked(automation_id, user_id, _when, _server_id):
        return "exec-fallback", {"automation_id": automation_id, "user_id": user_id, "status": "active"}

    monkeypatch.setattr(
        system_jobs_mod.auto_lifecycle,
        "trigger_automation",
        create_autospec(
            system_jobs_mod.auto_lifecycle.trigger_automation,
            side_effect=_missing,
        ),
    )
    orphan = create_autospec(system_jobs_mod.exec_db.create_execution)
    locked = create_autospec(
        system_jobs_mod.exec_db.create_execution_for_live_parent,
        side_effect=_locked,
    )
    monkeypatch.setattr(system_jobs_mod.exec_db, "create_execution", orphan)
    monkeypatch.setattr(system_jobs_mod.exec_db, "create_execution_for_live_parent", locked)
    monkeypatch.setattr(
        recycle_mod,
        "automation_is_soft_deleted",
        create_autospec(recycle_mod.automation_is_soft_deleted, return_value=False),
    )
    scheduler = MagicMock()
    scheduler.server_id = "server-1"

    def _instance(_cls=None):
        return scheduler

    monkeypatch.setattr(AutomationScheduler, "get_instance", _instance)

    result = await system_jobs_mod._trigger_automation_row_async(
        USER,
        {"automation_id": AUTO, "deleted_at": None},
    )
    assert result["execution_id"] == "exec-fallback"
    locked.assert_awaited_once()
    orphan.assert_not_awaited()
    scheduler.dispatch.assert_called_once()
    assert scheduler.dispatch.call_args.args[0]["automation_id"] == AUTO
