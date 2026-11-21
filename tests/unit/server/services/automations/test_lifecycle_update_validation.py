"""What the automation PATCH path refuses, and what a pause and resume keep.

Every automation runs in some workspace, Home's for the user's Home, so the
merged state of a PATCH has to name one; otherwise the row is only rejected by
the executor at run time, which spends a failure against ``max_failures``. The
same goes for a price config the monitor cannot read, and for a trigger kind a
PATCH cannot change.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from src.server.database.home_workspace import get_home_workspace_id
from src.server.services.automations.lifecycle import (
    AutomationRefusal,
    pause_automation,
    resume_automation,
    update_automation,
)

OWNER = "user-owner"
AUTOMATION_ID = str(uuid.uuid4())
WORKSPACE_ID = str(uuid.uuid4())
OTHER_WORKSPACE_ID = str(uuid.uuid4())
HOME = get_home_workspace_id(OWNER)
_LIFECYCLE = "src.server.services.automations.lifecycle"
_THREAD_META = "src.server.database.conversation.get_thread_auth_meta"


def _row(**overrides):
    row = {
        "automation_id": AUTOMATION_ID,
        "user_id": OWNER,
        "trigger_type": "cron",
        "cron_expression": "0 9 * * 1-5",
        "timezone": "UTC",
        "status": "active",
        "workspace_id": WORKSPACE_ID,
    }
    row.update(overrides)
    return row


class TestPlacement:
    @pytest.mark.asyncio
    @patch(f"{_LIFECYCLE}.auto_db")
    @patch(f"{_LIFECYCLE}.get_workspace")
    async def test_naming_a_workspace_moves_the_automation_there(
        self, mock_get_workspace, mock_auto_db,
    ):
        mock_auto_db.get_automation = AsyncMock(return_value=_row())
        mock_auto_db.update_automation = AsyncMock(return_value=_row())
        mock_get_workspace.return_value = {
            "workspace_id": OTHER_WORKSPACE_ID, "user_id": OWNER,
        }

        await update_automation(AUTOMATION_ID, OWNER, {"workspace_id": OTHER_WORKSPACE_ID})

        written = mock_auto_db.update_automation.call_args.kwargs
        assert written["workspace_id"] == OTHER_WORKSPACE_ID
        assert mock_get_workspace.call_args.args == (OTHER_WORKSPACE_ID,)

    @pytest.mark.asyncio
    @patch(f"{_LIFECYCLE}.auto_db")
    async def test_an_edit_that_names_no_workspace_leaves_a_placed_row_alone(
        self, mock_auto_db,
    ):
        mock_auto_db.get_automation = AsyncMock(return_value=_row())
        mock_auto_db.update_automation = AsyncMock(return_value=_row())

        await update_automation(AUTOMATION_ID, OWNER, {"name": "renamed"})

        assert mock_auto_db.update_automation.call_args.kwargs == {
            "conn": None, "name": "renamed",
        }


class TestTriggerKindIsFixed:
    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    async def test_changing_the_kind_is_refused(self, mock_auto_db):
        mock_auto_db.get_automation = AsyncMock(return_value=_row())

        with pytest.raises(ValidationError, match="can't change from 'cron' to 'once'"):
            await update_automation(AUTOMATION_ID, OWNER, {"trigger_type": "once"})

        mock_auto_db.update_automation.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    async def test_restating_the_same_kind_proceeds(self, mock_auto_db):
        mock_auto_db.get_automation = AsyncMock(return_value=_row())
        mock_auto_db.update_automation = AsyncMock(return_value=_row())

        await update_automation(
            AUTOMATION_ID, OWNER, {"trigger_type": "cron", "name": "renamed"}
        )

        mock_auto_db.update_automation.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kind", "field", "value"),
        [
            ("price", "next_run_at", "2026-10-01T13:00:00Z"),
            ("once", "cron_expression", "0 9 * * *"),
            ("cron", "trigger_config", {
                "symbol": "AAPL", "conditions": [{"type": "price_above", "value": 200}],
            }),
        ],
    )
    @patch("src.server.services.automations.lifecycle.auto_db")
    async def test_another_kinds_schedule_field_is_refused(self, mock_auto_db, kind, field, value):
        # A next_run_at stored on a price row would be claimed and fired by
        # the scheduler as if it were due.
        mock_auto_db.get_automation = AsyncMock(return_value=_row(trigger_type=kind))

        with pytest.raises(ValidationError, match=f"{field} doesn't apply to a '{kind}'"):
            await update_automation(AUTOMATION_ID, OWNER, {field: value})

        mock_auto_db.update_automation.assert_not_called()


PRICE_CONFIG = {"symbol": "AAPL", "conditions": [{"type": "price_above", "value": 200}]}


class TestPriceConfigOnUpdate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "config",
        [
            {**PRICE_CONFIG, "conditions": [{"type": "price_above", "value": 0}]},
            {**PRICE_CONFIG, "symbol": "^SPX"},
            {**PRICE_CONFIG, "symbol": "I:SPX"},
            {**PRICE_CONFIG, "symbol": "ABCDEFGHIJK"},
        ],
        ids=["zero-value", "caret", "I-prefix", "too-long"],
    )
    @patch("src.server.services.automations.lifecycle.auto_db")
    async def test_a_config_the_monitor_would_skip_is_refused(self, mock_auto_db, config):
        mock_auto_db.get_automation = AsyncMock(
            return_value=_row(trigger_type="price", cron_expression=None)
        )

        with pytest.raises(ValueError, match="Invalid price trigger config"):
            await update_automation(AUTOMATION_ID, OWNER, {"trigger_config": config})

        mock_auto_db.update_automation.assert_not_called()


@pytest.mark.asyncio
@patch("src.server.services.automations.lifecycle.auto_db")
async def test_an_empty_description_clears_it(mock_auto_db):
    mock_auto_db.get_automation = AsyncMock(return_value=_row(description="old"))
    mock_auto_db.update_automation = AsyncMock(return_value=_row(description=""))

    await update_automation(AUTOMATION_ID, OWNER, {"description": ""})

    assert mock_auto_db.update_automation.call_args.kwargs["description"] == ""


class TestPause:
    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    async def test_a_one_time_automation_keeps_its_run_time(self, mock_auto_db):
        """Resume reads the time back; the scheduler claims only active rows."""
        mock_auto_db.get_automation = AsyncMock(
            return_value=_row(trigger_type="once", cron_expression=None)
        )
        mock_auto_db.update_automation = AsyncMock(return_value=_row(status="paused"))

        await pause_automation(AUTOMATION_ID, OWNER)

        mock_auto_db.update_automation.assert_awaited_once_with(
            AUTOMATION_ID, OWNER, conn=None, status="paused"
        )

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    async def test_a_cron_clears_its_next_run(self, mock_auto_db):
        mock_auto_db.get_automation = AsyncMock(return_value=_row())
        mock_auto_db.update_automation = AsyncMock(return_value=_row(status="paused"))

        await pause_automation(AUTOMATION_ID, OWNER)

        mock_auto_db.update_automation.assert_awaited_once_with(
            AUTOMATION_ID, OWNER, conn=None, status="paused", next_run_at=None
        )


class TestResumeOnce:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status, resumes", [("disabled", True), ("paused", False)])
    @patch("src.server.services.automations.lifecycle.auto_db")
    async def test_a_past_time_resumes_only_what_the_server_switched_off(
        self, mock_auto_db, status, resumes
    ):
        """A switched-off one-time automation already had its run; back on, it
        waits for Run now. A paused one would silently never run."""
        mock_auto_db.get_automation = AsyncMock(
            return_value=_row(
                trigger_type="once", cron_expression=None, status=status,
                next_run_at=None,
            )
        )
        mock_auto_db.update_automation = AsyncMock(return_value=_row())

        if not resumes:
            with pytest.raises(ValueError, match="scheduled time has passed"):
                await resume_automation(AUTOMATION_ID, OWNER)
            return
        await resume_automation(AUTOMATION_ID, OWNER)
        mock_auto_db.update_automation.assert_awaited_once_with(
            AUTOMATION_ID, OWNER, conn=None, status="active", failure_count=0,
            disable_reason=None, next_run_at=None,
        )


class TestCronEditRecomputesOnlyWhenActive:
    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    async def test_active_cron_gets_a_new_next_run(self, mock_auto_db):
        mock_auto_db.get_automation = AsyncMock(return_value=_row())
        mock_auto_db.update_automation = AsyncMock(return_value=_row())

        await update_automation(AUTOMATION_ID, OWNER, {"cron_expression": "0 10 * * *"})

        assert mock_auto_db.update_automation.call_args.kwargs["next_run_at"] is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["paused", "disabled"])
    @patch("src.server.services.automations.lifecycle.auto_db")
    async def test_an_edit_does_not_give_a_stopped_cron_a_next_run(self, mock_auto_db, status):
        """Resume computes it from the edited expression and zone."""
        mock_auto_db.get_automation = AsyncMock(
            return_value=_row(status=status, next_run_at=None)
        )
        mock_auto_db.update_automation = AsyncMock(return_value=_row(status=status))

        await update_automation(
            AUTOMATION_ID, OWNER, {"cron_expression": "0 10 * * *", "timezone": "Asia/Tokyo"}
        )

        assert "next_run_at" not in mock_auto_db.update_automation.call_args.kwargs


class TestThreadPin:
    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    async def test_a_named_none_unpins_the_thread(self, mock_auto_db):
        """The agent tool's thread='new'. A pin left behind would be resumed
        by a later switch back to 'persistent'."""
        mock_auto_db.get_automation = AsyncMock(
            return_value=_row(
                thread_strategy="continue", conversation_thread_id=str(uuid.uuid4())
            )
        )
        mock_auto_db.update_automation = AsyncMock(return_value=_row())

        await update_automation(
            AUTOMATION_ID, OWNER, {"thread_strategy": "new", "conversation_thread_id": None}
        )

        written = mock_auto_db.update_automation.call_args.kwargs
        assert written["thread_strategy"] == "new"
        assert written["conversation_thread_id"] is None

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    async def test_an_update_that_names_no_thread_keeps_the_pin(self, mock_auto_db):
        mock_auto_db.get_automation = AsyncMock(return_value=_row())
        mock_auto_db.update_automation = AsyncMock(return_value=_row())

        await update_automation(AUTOMATION_ID, OWNER, {"name": "Renamed"})

        assert "conversation_thread_id" not in mock_auto_db.update_automation.call_args.kwargs


PINNED = str(uuid.uuid4())


def _owned_workspace(workspace_id, conn=None):
    return {"workspace_id": workspace_id, "user_id": OWNER}


class TestMoveUnpinsTheThread:
    """A run continues its pinned thread only in the automation's workspace,
    so a move that names no thread unpins it: the next run starts one in the
    new place."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("row", "target"),
        [
            (_row(), OTHER_WORKSPACE_ID),
            (_row(), HOME),
            (_row(workspace_id=HOME), WORKSPACE_ID),
        ],
        ids=["to-another-workspace", "to-home", "out-of-home"],
    )
    @patch(f"{_LIFECYCLE}.auto_db")
    @patch(f"{_LIFECYCLE}.get_workspace", new=AsyncMock(side_effect=_owned_workspace))
    async def test_a_move_that_names_no_thread_unpins_it(self, mock_auto_db, row, target):
        mock_auto_db.get_automation = AsyncMock(
            return_value={**row, "thread_strategy": "continue", "conversation_thread_id": PINNED}
        )
        mock_auto_db.update_automation = AsyncMock(return_value=_row())

        # The Automations page's PATCH: the place and the strategy, no thread.
        await update_automation(
            AUTOMATION_ID, OWNER, {"workspace_id": target, "thread_strategy": "continue"}
        )

        written = mock_auto_db.update_automation.call_args.kwargs
        assert written["workspace_id"] == target
        assert written["conversation_thread_id"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("row", "fields"),
        [
            (_row(), {"workspace_id": WORKSPACE_ID, "thread_strategy": "continue"}),
            (_row(workspace_id=HOME), {"workspace_id": HOME}),
        ],
        ids=["same-workspace", "same-home"],
    )
    @patch(f"{_LIFECYCLE}.auto_db")
    @patch(f"{_LIFECYCLE}.get_workspace", new=AsyncMock(side_effect=_owned_workspace))
    async def test_restating_where_it_runs_keeps_the_pin(self, mock_auto_db, row, fields):
        mock_auto_db.get_automation = AsyncMock(
            return_value={**row, "thread_strategy": "continue", "conversation_thread_id": PINNED}
        )
        mock_auto_db.update_automation = AsyncMock(return_value=_row())

        await update_automation(AUTOMATION_ID, OWNER, fields)

        assert "conversation_thread_id" not in mock_auto_db.update_automation.call_args.kwargs

    @pytest.mark.asyncio
    @patch(f"{_LIFECYCLE}.auto_db")
    @patch(f"{_LIFECYCLE}.get_workspace", new=AsyncMock(side_effect=_owned_workspace))
    @patch("src.server.database.conversation.get_thread_owner_id", new=AsyncMock(return_value=OWNER))
    @patch(
        _THREAD_META,
        new=AsyncMock(return_value={"user_id": OWNER, "workspace_id": uuid.UUID(OTHER_WORKSPACE_ID)}),
    )
    async def test_a_move_that_names_a_thread_there_keeps_that_thread(self, mock_auto_db):
        other_thread = str(uuid.uuid4())
        mock_auto_db.get_automation = AsyncMock(
            return_value=_row(thread_strategy="continue", conversation_thread_id=PINNED)
        )
        mock_auto_db.update_automation = AsyncMock(return_value=_row())

        await update_automation(
            AUTOMATION_ID,
            OWNER,
            {"workspace_id": OTHER_WORKSPACE_ID, "conversation_thread_id": other_thread},
        )

        assert mock_auto_db.update_automation.call_args.kwargs["conversation_thread_id"] == other_thread

    @pytest.mark.asyncio
    @patch(f"{_LIFECYCLE}.auto_db")
    @patch(f"{_LIFECYCLE}.get_workspace", new=AsyncMock(side_effect=_owned_workspace))
    @patch("src.server.database.conversation.get_thread_owner_id", new=AsyncMock(return_value=OWNER))
    @patch(
        _THREAD_META,
        new=AsyncMock(return_value={"user_id": OWNER, "workspace_id": uuid.UUID(WORKSPACE_ID)}),
    )
    async def test_a_move_that_names_the_thread_it_leaves_is_refused(self, mock_auto_db):
        mock_auto_db.get_automation = AsyncMock(
            return_value=_row(thread_strategy="continue", conversation_thread_id=PINNED)
        )

        with pytest.raises(AutomationRefusal) as exc:
            await update_automation(
                AUTOMATION_ID,
                OWNER,
                {"workspace_id": OTHER_WORKSPACE_ID, "conversation_thread_id": PINNED},
            )

        assert exc.value.field == "conversation_thread_id"
        mock_auto_db.update_automation.assert_not_called()


class TestManagedUnitMetadataProtection:
    @pytest.mark.asyncio
    @patch(f"{_LIFECYCLE}.auto_db")
    async def test_stripping_or_mutating_managed_unit_metadata_is_refused(
        self, mock_auto_db, monkeypatch: pytest.MonkeyPatch
    ):
        from unittest.mock import create_autospec
        from src.dalton import settings as dalton_settings
        from src.dalton.coverage import units as cu_units
        from src.server.database import pool as pool_mod
        from src.server.services.automations.lifecycle import TargetRefused

        monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)
        managed_row = _row(
            name="dalton-nvda-watch",
            metadata={
                "unit_id": "unit-nvda",
                "ticker": "NVDA",
                "dalton_kind": "company_watch",
                "dalton_managed": True,
            },
        )
        mock_auto_db.get_automation = AsyncMock(return_value=managed_row)
        mock_auto_db.update_automation = AsyncMock(return_value=managed_row)

        class _NoopTx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

        class _NoopConn:
            def transaction(self):
                return _NoopTx()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

        monkeypatch.setattr(
            pool_mod, "get_db_connection", create_autospec(pool_mod.get_db_connection, return_value=_NoopConn())
        )
        monkeypatch.setattr(
            cu_units,
            "lock_and_assert_managed_unit_write_allowed",
            create_autospec(
                cu_units.lock_and_assert_managed_unit_write_allowed,
                return_value={"unit_id": "unit-nvda", "lead_analyst_user_id": OWNER},
            ),
        )

        # Stripping unit_id via generic PATCH refused with 403 TargetRefused(field="metadata")
        with pytest.raises(TargetRefused) as exc_strip:
            await update_automation(
                AUTOMATION_ID,
                OWNER,
                {
                    "metadata": {
                        "ticker": "NVDA",
                        "dalton_kind": "company_watch",
                        "dalton_managed": True,
                    }
                },
            )
        assert exc_strip.value.status_code == 403
        assert exc_strip.value.field == "metadata"
        mock_auto_db.update_automation.assert_not_called()

        # Mutating ticker refused
        with pytest.raises(TargetRefused) as exc_mut:
            await update_automation(
                AUTOMATION_ID,
                OWNER,
                {
                    "metadata": {
                        "unit_id": "unit-nvda",
                        "ticker": "AMD",
                        "dalton_kind": "company_watch",
                        "dalton_managed": True,
                    }
                },
            )
        assert exc_mut.value.status_code == 403
        mock_auto_db.update_automation.assert_not_called()

    @pytest.mark.asyncio
    @patch(f"{_LIFECYCLE}.auto_db")
    async def test_ordinary_task_metadata_update_allowed(
        self, mock_auto_db, monkeypatch: pytest.MonkeyPatch
    ):
        from src.dalton import settings as dalton_settings

        monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)
        ordinary = _row(name="Daily morning brief", metadata={"note": "v1"})
        mock_auto_db.get_automation = AsyncMock(return_value=ordinary)
        mock_auto_db.update_automation = AsyncMock(
            return_value={**ordinary, "metadata": {"note": "v2"}}
        )

        updated = await update_automation(
            AUTOMATION_ID, OWNER, {"metadata": {"note": "v2"}}
        )
        assert updated["metadata"] == {"note": "v2"}
        mock_auto_db.update_automation.assert_awaited_once()

    @pytest.mark.asyncio
    @patch(f"{_LIFECYCLE}.auto_db")
    async def test_resume_automation_refuses_replaced_by_tracker(
        self, mock_auto_db
    ):
        """Round 6 Item 2: generic resume endpoint refuses replaced_by_tracker delta automations."""
        from src.server.services.automations.lifecycle import resume_automation

        replaced = _row(
            status="paused",
            next_run_at=None,
            metadata={"dalton_kind": "company_watch", "replaced_by_tracker": True},
        )
        mock_auto_db.get_automation = AsyncMock(return_value=replaced)
        mock_auto_db.update_automation = AsyncMock()

        with pytest.raises(AutomationRefusal) as exc:
            await resume_automation(AUTOMATION_ID, OWNER)
        assert "多空跟踪替代" in str(exc.value)
        mock_auto_db.update_automation.assert_not_awaited()

    @pytest.mark.asyncio
    @patch(f"{_LIFECYCLE}.auto_db")
    async def test_ordinary_row_protects_replaced_by_tracker_unconditionally(
        self, mock_auto_db, monkeypatch: pytest.MonkeyPatch
    ):
        """Round 6 Item 3: stripping/mutating replaced_by_tracker is refused even on non-unit rows."""
        from unittest.mock import create_autospec
        from src.dalton import settings as dalton_settings
        from src.dalton.database import coverage_units as cu_db
        from src.server.services.automations.lifecycle import TargetRefused

        monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)
        monkeypatch.setattr(
            cu_db,
            "get_active_unit_by_workspace",
            create_autospec(cu_db.get_active_unit_by_workspace, return_value=None),
        )
        ordinary_replaced = _row(
            name="custom-delta-name",
            metadata={"replaced_by_tracker": True, "note": "v1"},
        )
        mock_auto_db.get_automation = AsyncMock(return_value=ordinary_replaced)
        mock_auto_db.update_automation = AsyncMock()

        with pytest.raises(TargetRefused) as exc_strip:
            await update_automation(
                AUTOMATION_ID, OWNER, {"metadata": {"note": "v2"}}
            )
        assert exc_strip.value.status_code == 403
        assert exc_strip.value.field == "metadata"

        with pytest.raises(TargetRefused) as exc_flip:
            await update_automation(
                AUTOMATION_ID,
                OWNER,
                {"metadata": {"replaced_by_tracker": False, "note": "v2"}},
            )
        assert exc_flip.value.status_code == 403
        mock_auto_db.update_automation.assert_not_awaited()

    @pytest.mark.asyncio
    @patch(f"{_LIFECYCLE}.auto_db")
    async def test_adding_dalton_kind_weekly_delta_derives_research_tracking_scope(
        self, mock_auto_db, monkeypatch: pytest.MonkeyPatch
    ):
        """Round 7 Item 5: generic update adding dalton_kind='weekly_delta' derives scope='research_tracking'."""
        from unittest.mock import create_autospec
        from src.dalton import settings as dalton_settings
        from src.dalton.database import coverage_units as cu_db
        from src.server.database import pool as pool_mod

        monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)
        monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: False)
        monkeypatch.setattr(
            cu_db,
            "get_active_unit_by_workspace",
            create_autospec(cu_db.get_active_unit_by_workspace, return_value=None),
        )

        class _NoopTx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

        class _NoopConn:
            def transaction(self):
                return _NoopTx()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

        monkeypatch.setattr(
            pool_mod,
            "get_db_connection",
            create_autospec(pool_mod.get_db_connection, return_value=_NoopConn()),
        )

        ordinary = _row(name="Daily morning brief", scope="personal", metadata={"note": "v1"})
        mock_auto_db.get_automation = AsyncMock(return_value=ordinary)
        mock_auto_db.update_automation = AsyncMock(
            side_effect=lambda aid, uid, conn=None, **kw: {**ordinary, **kw}
        )

        updated = await update_automation(
            AUTOMATION_ID,
            OWNER,
            {"metadata": {"note": "v1", "dalton_kind": "weekly_delta"}},
        )
        assert updated["scope"] == "research_tracking"
        assert mock_auto_db.update_automation.await_args.kwargs["scope"] == "research_tracking"

    @pytest.mark.asyncio
    @patch(f"{_LIFECYCLE}.get_workspace")
    @patch(f"{_LIFECYCLE}.auto_db")
    async def test_replaced_by_tracker_null_value_protected_and_generic_add_forbidden(
        self, mock_auto_db, mock_get_workspace, monkeypatch: pytest.MonkeyPatch
    ):
        """Round 7 Item 7: key presence (even with None) is protected on update, and adding
        replaced_by_tracker via generic update or create is refused with 403 TargetRefused."""
        from src.dalton import settings as dalton_settings
        from src.server.models.automation import AutomationCreate
        from src.server.services.automations.lifecycle import TargetRefused, create_automation

        monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: False)
        mock_get_workspace.return_value = {"workspace_id": WORKSPACE_ID, "user_id": OWNER}

        # 1. Existing row with replaced_by_tracker=None cannot drop or mutate the key
        null_marked = _row(name="custom-delta", metadata={"replaced_by_tracker": None, "note": "v1"})
        mock_auto_db.get_automation = AsyncMock(return_value=null_marked)
        mock_auto_db.update_automation = AsyncMock()

        with pytest.raises(TargetRefused) as exc_drop_null:
            await update_automation(AUTOMATION_ID, OWNER, {"metadata": {"note": "v2"}})
        assert exc_drop_null.value.status_code == 403
        assert exc_drop_null.value.field == "metadata"

        with pytest.raises(TargetRefused) as exc_set_val:
            await update_automation(
                AUTOMATION_ID,
                OWNER,
                {"metadata": {"replaced_by_tracker": "tracker-1", "note": "v2"}},
            )
        assert exc_set_val.value.status_code == 403
        mock_auto_db.update_automation.assert_not_awaited()

        # 2. Ordinary row without replaced_by_tracker cannot add it via generic update
        unmarked = _row(name="custom-delta", metadata={"note": "v1"})
        mock_auto_db.get_automation = AsyncMock(return_value=unmarked)

        with pytest.raises(TargetRefused) as exc_add:
            await update_automation(
                AUTOMATION_ID,
                OWNER,
                {"metadata": {"note": "v1", "replaced_by_tracker": "tracker-1"}},
            )
        assert exc_add.value.status_code == 403
        with pytest.raises(TargetRefused) as exc_add_null:
            await update_automation(
                AUTOMATION_ID,
                OWNER,
                {"metadata": {"note": "v1", "replaced_by_tracker": None}},
            )
        assert exc_add_null.value.status_code == 403
        mock_auto_db.update_automation.assert_not_awaited()

        # 3. Generic create_automation refuses replaced_by_tracker in metadata
        mock_auto_db.create_automation = AsyncMock()
        with pytest.raises(TargetRefused) as exc_create:
            await create_automation(
                OWNER,
                AutomationCreate(
                    name="custom-delta",
                    trigger_type="cron",
                    cron_expression="0 9 * * *",
                    timezone="UTC",
                    instruction="run",
                    workspace_id=WORKSPACE_ID,
                    metadata={"replaced_by_tracker": "tracker-1"},
                ),
            )
        assert exc_create.value.status_code == 403
        mock_auto_db.create_automation.assert_not_awaited()
