"""Unit coverage for recycle-bin decisions, auth gates, and purge SQL."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import re
from unittest.mock import create_autospec

from fastapi import HTTPException
from psycopg.errors import UniqueViolation
import pytest

from ptc_agent.core.paths import AUTOMATION_FILE_NAME

from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.database import automation_recycle as recycle_db
from src.dalton.services import automation_recycle as recycle
from src.dalton.services.company_automations import AutomationVersionConflictError
from src.server.dependencies.principal import Principal

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
AUTO_ID = "6f1c2a9e-3b4d-4e8f-9a1b-2c3d4e5f6a7b"
OWNER = "recycle-owner"


def _principal(user_id: str) -> Principal:
    return Principal(user_id=user_id, auth="session")


def test_pre_delete_desired_status_follows_live_work() -> None:
    assert recycle.pre_delete_desired_status({"status": "active"}, []) == "active"
    assert (
        recycle.pre_delete_desired_status({"status": "executing"}, ["pending"])
        == "active"
    )
    assert recycle.pre_delete_desired_status({"status": "executing"}, []) == "paused"
    assert recycle.pre_delete_desired_status({"status": "paused"}, ["running"]) == "paused"


def test_restored_file_name_keeps_the_column_limit() -> None:
    assert (
        recycle.restored_file_name("morning-brief.json", "abcd1234")
        == "morning-brief-restored-abcd1234.json"
    )
    long_name = recycle.restored_file_name("a" * 300 + ".json", "abcd1234")
    assert len(long_name) <= 255
    assert long_name.endswith("-restored-abcd1234.json")
    assert len(long_name[:-5]) == 64
    assert re.fullmatch(AUTOMATION_FILE_NAME, long_name)
    assert re.fullmatch(
        AUTOMATION_FILE_NAME,
        recycle.restored_file_name("morning-brief.json", "abcd1234"),
    )


def test_decide_effective_state_covers_the_restore_rules() -> None:
    future = NOW + timedelta(hours=2)
    past = NOW - timedelta(hours=2)
    assert recycle.decide_effective_state(
        trigger_type="cron",
        desired_status="active",
        disable_reason="max_failures",
        once_when=None,
        once_completed=False,
        now=NOW,
        has_price_claim=False,
        cron_next=future,
        kept_once_next=None,
    ) == ("disabled", None)
    assert recycle.decide_effective_state(
        trigger_type="cron",
        desired_status="paused",
        disable_reason=None,
        once_when=None,
        once_completed=False,
        now=NOW,
        has_price_claim=False,
        cron_next=future,
        kept_once_next=None,
    ) == ("paused", None)
    assert recycle.decide_effective_state(
        trigger_type="once",
        desired_status="paused",
        disable_reason=None,
        once_when=past,
        once_completed=False,
        now=NOW,
        has_price_claim=False,
        cron_next=None,
        kept_once_next=past,
    ) == ("paused", past)
    assert recycle.decide_effective_state(
        trigger_type="cron",
        desired_status="active",
        disable_reason=None,
        once_when=None,
        once_completed=False,
        now=NOW,
        has_price_claim=False,
        cron_next=future,
        kept_once_next=None,
    ) == ("active", future)
    assert recycle.decide_effective_state(
        trigger_type="once",
        desired_status="active",
        disable_reason=None,
        once_when=future,
        once_completed=False,
        now=NOW,
        has_price_claim=False,
        cron_next=None,
        kept_once_next=None,
    ) == ("active", future)
    assert recycle.decide_effective_state(
        trigger_type="once",
        desired_status="active",
        disable_reason=None,
        once_when=past,
        once_completed=False,
        now=NOW,
        has_price_claim=False,
        cron_next=None,
        kept_once_next=past,
    ) == ("paused", None)
    assert recycle.decide_effective_state(
        trigger_type="once",
        desired_status="active",
        disable_reason=None,
        once_when=past,
        once_completed=True,
        now=NOW,
        has_price_claim=False,
        cron_next=None,
        kept_once_next=None,
    ) == ("completed", None)
    assert recycle.decide_effective_state(
        trigger_type="price",
        desired_status="active",
        disable_reason=None,
        once_when=None,
        once_completed=False,
        now=NOW,
        has_price_claim=True,
        cron_next=None,
        kept_once_next=None,
    ) == ("executing", None)
    assert recycle.decide_effective_state(
        trigger_type="price",
        desired_status="active",
        disable_reason=None,
        once_when=None,
        once_completed=False,
        now=NOW,
        has_price_claim=False,
        cron_next=None,
        kept_once_next=None,
    ) == ("active", None)


def test_purge_sql_touches_only_old_automations() -> None:
    sql = recycle_db.PURGE_AUTOMATIONS_SQL
    assert "30 days" in sql
    assert "dalton_data_task_defs" not in sql
    assert "pending" in sql and "waiting" in sql and "running" in sql
    assert recycle_db.PURGE_LOCK_NAMESPACE == "dalton"
    assert recycle_db.PURGE_LOCK_NAME == "automation_recycle_purge"


@pytest.mark.asyncio
async def test_purge_outside_the_window_does_not_touch_the_database() -> None:
    outside = datetime(2026, 11, 11, 4, 0, tzinfo=UTC)
    assert await recycle.purge_expired_automations_if_due(outside) == 0


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Conn:
    def transaction(self):
        return _Tx()


@asynccontextmanager
async def _db(conn=None):
    yield _Conn()


def _row(**overrides):
    data = {
        "automation_id": AUTO_ID,
        "user_id": OWNER,
        "config_version": 1,
        "active_version": 1,
        "audit_history": [],
        "deleted_at": None,
        "scope": "personal",
        "status": "active",
        "trigger_type": "cron",
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_rollback_target_must_be_the_previous_version(monkeypatch) -> None:
    async def _lock(automation_id, *, owner_user_id, conn):
        return _row()

    monkeypatch.setattr(recycle, "get_db_connection", _db)
    monkeypatch.setattr(recycle_db, "lock_automation_for_recycle", _lock)
    with pytest.raises(HTTPException) as exc:
        await recycle.rollback_automation(
            AUTO_ID,
            actor_user_id=OWNER,
            expected_config_version=1,
            target_version=1,
            owner_user_id=OWNER,
            company=False,
            prelocked_pref_row={},
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_version_conflict_is_the_patch_error(monkeypatch) -> None:
    async def _lock(automation_id, *, owner_user_id, conn):
        return _row()

    monkeypatch.setattr(recycle, "get_db_connection", _db)
    monkeypatch.setattr(recycle_db, "lock_automation_for_recycle", _lock)
    with pytest.raises(AutomationVersionConflictError) as exc:
        await recycle.soft_delete_automation(
            AUTO_ID,
            actor_user_id=OWNER,
            expected_config_version=4,
            owner_user_id=OWNER,
            company=False,
            prelocked_pref_row={},
        )
    assert "expected 4, current is 1" in str(exc.value)


@pytest.mark.asyncio
async def test_second_delete_is_409(monkeypatch) -> None:
    async def _lock(automation_id, *, owner_user_id, conn):
        return _row(deleted_at=NOW)

    monkeypatch.setattr(recycle, "get_db_connection", _db)
    monkeypatch.setattr(recycle_db, "lock_automation_for_recycle", _lock)
    with pytest.raises(HTTPException) as exc:
        await recycle.soft_delete_automation(
            AUTO_ID,
            actor_user_id=OWNER,
            expected_config_version=1,
            owner_user_id=OWNER,
            company=False,
            prelocked_pref_row={},
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_company_service_account_cannot_delete() -> None:
    with pytest.raises(HTTPException) as exc:
        await recycle.delete_task(_principal(COMPANY_SYSTEM_USER_ID), AUTO_ID, 1)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_other_owner_is_404(monkeypatch) -> None:
    async def _identity(automation_id, *, conn=None):
        return {"user_id": "someone-else", "scope": "personal", "deleted_at": None}

    monkeypatch.setattr(
        recycle.company_data_db,
        "get_automation_owner_identity",
        _identity,
    )
    with pytest.raises(HTTPException) as exc:
        await recycle.delete_task(_principal(OWNER), AUTO_ID, 1)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_external_company_reader_is_forbidden(monkeypatch) -> None:
    async def _identity(automation_id, *, conn=None):
        return {
            "user_id": COMPANY_SYSTEM_USER_ID,
            "scope": "company",
            "deleted_at": None,
        }

    async def _deny(principal):
        raise HTTPException(status_code=403, detail="Forbidden")

    monkeypatch.setattr(
        recycle.company_data_db,
        "get_automation_owner_identity",
        _identity,
    )
    monkeypatch.setattr(recycle, "require_company_scope_read_access", _deny)
    with pytest.raises(HTTPException) as exc:
        await recycle.delete_task(_principal("external-user"), AUTO_ID, 1)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_company_service_account_cannot_delete_a_script(monkeypatch) -> None:
    async def _allow(principal):
        return None

    monkeypatch.setattr(
        "src.dalton.services.company_script_writes.require_company_scope_read_access",
        _allow,
    )
    with pytest.raises(HTTPException) as exc:
        await recycle.delete_task(
            _principal(COMPANY_SYSTEM_USER_ID),
            "data:bbg-data-pull-asia",
            1,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_invalid_id_is_422() -> None:
    with pytest.raises(HTTPException) as exc:
        await recycle.delete_task(_principal(OWNER), "not-a-uuid", 1)
    assert exc.value.status_code == 422


def test_a_paused_once_rollback_clears_next_run_at() -> None:
    """Rollback leaves the one-shot time in the snapshot. Restore still keeps it."""
    past = NOW - timedelta(hours=2)
    kept = recycle.decide_effective_state(
        trigger_type="once",
        desired_status="paused",
        disable_reason=None,
        once_when=past,
        once_completed=False,
        now=NOW,
        has_price_claim=False,
        cron_next=None,
        kept_once_next=past,
    )
    cleared = recycle.decide_effective_state(
        trigger_type="once",
        desired_status="paused",
        disable_reason=None,
        once_when=past,
        once_completed=False,
        now=NOW,
        has_price_claim=False,
        cron_next=None,
        kept_once_next=past,
        keep_paused_once_next=False,
    )
    assert kept == ("paused", past)
    assert cleared == ("paused", None)


_ONCE = "2026-12-01T09:00:00+00:00"
_OLD = "feishu:user:ou_old"
_NEW = "feishu:user:ou_new"


def _complete_snapshot(**overrides):
    data = {
        "name": "Morning",
        "description": None,
        "desired_status": "paused",
        "trigger_type": "once",
        "cron_expression": None,
        "once_run_at": _ONCE,
        "trigger_config": {},
        "timezone": "UTC",
        "instruction": "run once",
        "workspace_id": None,
        "llm_model": None,
        "additional_context": None,
        "thread_strategy": "new",
        "conversation_thread_id": None,
        "max_failures": 3,
        "notify_enabled": True,
        "notify_routes": [_NEW],
        "delivery_config": {},
    }
    data.update(overrides)
    return data


def _history_row(*, mode: str, trigger_type: str = "once"):
    target = _complete_snapshot(trigger_type=trigger_type)
    current = _complete_snapshot(
        trigger_type=trigger_type,
        notify_enabled=False,
        notify_routes=[_OLD],
        once_run_at=_ONCE if trigger_type == "once" else None,
    )
    if trigger_type != "once":
        target["once_run_at"] = None
        target["cron_expression"] = "0 9 * * *"
        current["cron_expression"] = "0 9 * * *"
    return _row(
        name="Morning",
        description=None,
        trigger_type=trigger_type,
        cron_expression=None if trigger_type == "once" else "0 8 * * *",
        timezone="UTC",
        instruction="run once",
        workspace_id=None,
        llm_model=None,
        additional_context=None,
        thread_strategy="new",
        conversation_thread_id=None,
        max_failures=3,
        trigger_config={},
        delivery_config={},
        notify_enabled=False,
        notify_routes=[_OLD],
        notify_storage_mode=mode,
        file_name="morning.json",
        status="paused",
        next_run_at=datetime(2026, 12, 1, 9, 0, tzinfo=UTC),
        config_version=2,
        active_version=2,
        scope="personal",
        disable_reason=None,
        deleted_at=None,
        audit_history=[
            {"version": 1, "prev_version": None, "snapshot": target},
            {"version": 2, "prev_version": 1, "snapshot": current},
        ],
    )


def _install_rollback_fakes(monkeypatch, row):
    captured: dict = {}
    upserts: list = []

    async def _lock(automation_id, *, owner_user_id, conn):
        return row

    async def _update(automation_id, *, expected_config_version, fields, conn):
        captured["fields"] = fields
        return True

    async def _fetch(automation_id, *, conn):
        fields = captured["fields"]
        return {
            "automation_id": AUTO_ID,
            "user_id": OWNER,
            "name": fields["name"],
            "description": fields["description"],
            "trigger_type": fields["trigger_type"],
            "cron_expression": fields["cron_expression"],
            "timezone": fields["timezone"] or "UTC",
            "trigger_config": fields["trigger_config"],
            "next_run_at": fields["next_run_at"],
            "last_run_at": None,
            "instruction": fields["instruction"],
            "workspace_id": fields["workspace_id"],
            "llm_model": fields["llm_model"],
            "additional_context": fields["additional_context"],
            "thread_strategy": fields["thread_strategy"],
            "conversation_thread_id": fields["conversation_thread_id"],
            "status": fields["status"],
            "max_failures": fields["max_failures"],
            "failure_count": 0,
            "disable_reason": None,
            "delivery_config": fields["delivery_config"],
            "metadata": {},
            "created_at": NOW,
            "updated_at": NOW,
            "scope": "personal",
            "notify_storage_mode": row["notify_storage_mode"],
            "notify_enabled": fields["notify_enabled"],
            "notify_routes": fields["notify_routes"],
            "config_version": fields["config_version"],
            "active_version": fields["active_version"],
        }

    async def _upsert(*_args, **kwargs):
        upserts.append(kwargs)
        return {}

    async def _tier(_user_id, *, conn=None):
        return "external"

    async def _not_completed(*_args, **_kwargs):
        return False

    async def _no_rows(*_args, **_kwargs):
        return []

    async def _zero(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(recycle, "get_db_connection", _db)
    monkeypatch.setattr(recycle_db, "lock_automation_for_recycle", _lock)
    monkeypatch.setattr(recycle_db, "update_automation_recycle", _update)
    monkeypatch.setattr(recycle_db, "fetch_automation_read_row", _fetch)
    monkeypatch.setattr(recycle_db, "once_schedule_completed", _not_completed)
    monkeypatch.setattr(recycle_db, "lock_unfinished_executions", _no_rows)
    monkeypatch.setattr(recycle_db, "skip_unstarted_executions", _zero)
    monkeypatch.setattr("src.server.database.user.upsert_user_preferences", _upsert)
    invalidations: list[str] = []

    async def _invalidate(user_id: str) -> None:
        invalidations.append(str(user_id))

    monkeypatch.setattr(
        "src.dalton.database.company_automations.resolve_owner_tier_for_snapshot",
        _tier,
    )
    monkeypatch.setattr(
        "src.server.database.user.invalidate_user_prefs_cache",
        _invalidate,
    )
    return captured, upserts, invalidations


def _old_prefs():
    key = f"automation:{AUTO_ID}"
    return {
        "user_id": OWNER,
        "model_preference": {
            "dalton_notify_enabled": True,
            "dalton_notify_targets": [_OLD, _NEW],
            "dalton_notify_overrides": {key: False},
            "dalton_notify_routes": {key: [_OLD]},
        },
    }


@pytest.mark.asyncio
async def test_legacy_rollback_writes_prefs_and_clears_a_paused_once(monkeypatch) -> None:
    row = _history_row(mode="legacy")
    captured, upserts, invalidations = _install_rollback_fakes(monkeypatch, row)
    result = await recycle.rollback_automation(
        AUTO_ID,
        actor_user_id=OWNER,
        expected_config_version=2,
        target_version=1,
        owner_user_id=OWNER,
        company=False,
        prelocked_pref_row=_old_prefs(),
    )
    fields = captured["fields"]
    snapshot = fields["audit_history"][-1]["snapshot"]
    changes = fields["audit_history"][-1]["changes"]
    assert fields["status"] == "paused"
    assert fields["next_run_at"] is None
    assert snapshot["once_run_at"] == _ONCE
    assert snapshot["desired_status"] == "paused"
    assert snapshot["notify_enabled"] is True
    assert snapshot["notify_routes"] == [_NEW]
    assert fields["notify_enabled"] is True
    assert fields["notify_routes"] == [_NEW]
    assert changes["notify_routes"]["old"] == [_OLD]
    assert changes["notify_routes"]["new"] == [_NEW]
    assert changes["notify_enabled"]["new"] is True
    assert result is not None
    assert result.notify_enabled is True
    assert result.notify_routes == [_NEW]
    assert result.next_run_at is None
    assert len(upserts) == 1
    written = upserts[0]["model_preference"]
    assert written["dalton_notify_routes"][f"automation:{AUTO_ID}"] == [_NEW]
    assert written["dalton_notify_overrides"][f"automation:{AUTO_ID}"] is True
    assert invalidations == [OWNER]


@pytest.mark.asyncio
async def test_task_row_rollback_writes_the_row_and_not_prefs(monkeypatch) -> None:
    row = _history_row(mode="task_row", trigger_type="cron")
    captured, upserts, invalidations = _install_rollback_fakes(monkeypatch, row)
    result = await recycle.rollback_automation(
        AUTO_ID,
        actor_user_id=OWNER,
        expected_config_version=2,
        target_version=1,
        owner_user_id=OWNER,
        company=False,
        prelocked_pref_row=_old_prefs(),
    )
    fields = captured["fields"]
    snapshot = fields["audit_history"][-1]["snapshot"]
    assert upserts == []
    assert fields["notify_enabled"] is True
    assert fields["notify_routes"] == [_NEW]
    assert snapshot["notify_enabled"] is True
    assert snapshot["notify_routes"] == [_NEW]
    assert snapshot["desired_status"] == "paused"
    assert result is not None
    assert result.notify_enabled is True
    assert result.notify_routes == [_NEW]
    assert invalidations == []


@pytest.mark.asyncio
async def test_legacy_rollback_effective_notify_uses_the_written_prefs(
    monkeypatch,
) -> None:
    """The response uses the prefs written in this transaction, after commit."""
    events: list[str] = []
    row = _history_row(mode="legacy")
    _captured, _upserts, invalidations = _install_rollback_fakes(monkeypatch, row)

    class _OrderTx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            if exc_type is None:
                events.append("commit")
            return False

    class _OrderConn:
        def transaction(self):
            return _OrderTx()

    @asynccontextmanager
    async def _order_db(conn=None):
        yield _OrderConn()

    async def _invalidate(user_id: str) -> None:
        invalidations.append(str(user_id))
        events.append("invalidate")

    async def _cache_must_not_be_read(*_args, **_kwargs):
        raise AssertionError("preference cache was read")

    monkeypatch.setattr(recycle, "get_db_connection", _order_db)
    monkeypatch.setattr(
        "src.server.database.user.invalidate_user_prefs_cache",
        _invalidate,
    )
    monkeypatch.setattr(
        "src.dalton.llm.model_prefs.get_user_preferences",
        _cache_must_not_be_read,
    )
    monkeypatch.setattr(
        "src.server.database.user.get_user_preferences",
        _cache_must_not_be_read,
    )

    result = await recycle.rollback_automation(
        AUTO_ID,
        actor_user_id=OWNER,
        expected_config_version=2,
        target_version=1,
        owner_user_id=OWNER,
        company=False,
        prelocked_pref_row=_old_prefs(),
        principal=_principal(OWNER),
    )
    assert events == ["commit", "invalidate"]
    assert invalidations == [OWNER]
    assert result is not None
    assert result.dalton_notify_effective is True
    assert result.dalton_notify_route_targets == [_NEW]


@pytest.mark.asyncio
async def test_restore_unique_violation_is_409(monkeypatch) -> None:
    async def _lock(automation_id, *, owner_user_id, conn):
        return _row(
            deleted_at=NOW,
            file_name="morning.json",
            status="paused",
            pre_delete_desired_status="paused",
            config_version=1,
            active_version=1,
            audit_history=[],
        )

    async def _append(*_args, **_kwargs):
        return ([{"version": 2, "snapshot": {}}], 2)

    async def _effective(*_args, **_kwargs):
        return "paused", None

    async def _free(_row_arg, *, conn):
        return "morning.json"

    async def _update(*_args, **_kwargs):
        raise UniqueViolation("duplicate key")

    monkeypatch.setattr(recycle, "get_db_connection", _db)
    monkeypatch.setattr(recycle_db, "lock_automation_for_recycle", _lock)
    monkeypatch.setattr(recycle, "_append_agent_history", _append)
    monkeypatch.setattr(recycle, "_effective_for_row", _effective)
    monkeypatch.setattr(recycle, "_free_restored_name", _free)
    monkeypatch.setattr(recycle_db, "update_automation_recycle", _update)
    with pytest.raises(HTTPException) as exc:
        await recycle.restore_automation(
            AUTO_ID,
            actor_user_id=OWNER,
            expected_config_version=1,
            owner_user_id=OWNER,
            company=False,
            prelocked_pref_row={},
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "Could not choose a free file name for the restored automation"


@pytest.mark.asyncio
async def test_restore_before_the_skip_keeps_the_new_execution(
    monkeypatch,
) -> None:
    """One UPDATE reads deleted_at. A restore that lands first keeps the new run."""
    aid = AUTO_ID
    parent = {"deleted_at": NOW}
    executions = [
        {"id": "old", "automation_id": aid, "status": "pending"},
    ]
    statements: list[str] = []

    class _Cur:
        def __init__(self) -> None:
            self.rowcount = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, sql, _params=None):
            text = " ".join(str(sql).split())
            statements.append(text)
            assert text.startswith("UPDATE")
            assert "automation_executions" in text
            assert "EXISTS" in text
            assert "deleted_at IS NOT NULL" in text
            assert "FOR UPDATE" not in text
            assert "SELECT DISTINCT" not in text
            parent_checked = "automations" in text and "deleted_at IS NOT NULL" in text
            # The scan already holds the old pending row. Restore commits and
            # inserts a new execution before this statement reads deleted_at.
            parent["deleted_at"] = None
            executions.append(
                {"id": "new", "automation_id": aid, "status": "pending"}
            )
            skipped = 0
            for row in executions:
                if row["status"] not in ("pending", "waiting"):
                    continue
                if parent_checked and parent["deleted_at"] is None:
                    continue
                row["status"] = "skipped"
                row["skip_reason"] = "task_deleted"
                skipped += 1
            self.rowcount = skipped

    class _Conn:
        def cursor(self):
            return _Cur()

    @asynccontextmanager
    async def _pool(*_args, **_kwargs):
        yield _Conn()

    real_connect = recycle_db.get_db_connection
    monkeypatch.setattr(
        recycle_db,
        "get_db_connection",
        create_autospec(real_connect, side_effect=_pool),
    )
    skipped = await recycle_db.skip_unstarted_executions_of_deleted_parents()

    assert skipped == 0
    assert len(statements) == 1
    assert [row["status"] for row in executions] == ["pending", "pending"]
    assert executions[1]["id"] == "new"
