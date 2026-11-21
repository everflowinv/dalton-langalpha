"""Ownership gating for caller-supplied automation targets.

Locks the cross-tenant contract: a ``workspace_id`` / ``conversation_thread_id``
naming something the caller does not own is rejected before any row is written.
The gate lives in the lifecycle because the REST router, the agent tools and
the automation files all write through it.
"""

import uuid
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
from fastapi import Request
from starlette.datastructures import Headers

from src.server.database.home_workspace import get_home_workspace_id
from src.server.database.workspace import HomeWorkspaceTaken
from src.server.dependencies.principal import Principal
from src.server.services.automations import lifecycle
from src.server.services.automations.lifecycle import (
    AutomationRefusal,
    TargetRefused,
    create_automation,
    update_automation,
)
from src.server.models.automation import AutomationCreate


def _stub_request() -> Request:
    # DALTON-HOOK(automation-model): #656 switched the routes to
    # principal-shaped params; these imports and this request stub serve the
    # adapted call sites below.
    req = create_autospec(Request, instance=True)
    req.__len__.return_value = 1
    req.headers = Headers({})
    return req

OWNER = "user-owner"
OTHER = "user-other"
AUTOMATION_ID = str(uuid.uuid4())
WORKSPACE_ID = str(uuid.uuid4())
THREAD_ID = str(uuid.uuid4())
HOME = get_home_workspace_id(OWNER)

_THREAD_OWNER = "src.server.database.conversation.get_thread_owner_id"
_THREAD_META = "src.server.database.conversation.get_thread_auth_meta"


def _thread_in(workspace_id):
    return AsyncMock(return_value={"user_id": OWNER, "workspace_id": uuid.UUID(workspace_id)})


@pytest.fixture(autouse=True)
def _home(monkeypatch):
    """Home's row, which a write naming Home makes sure of, is the caller's."""
    monkeypatch.setattr(lifecycle, "_home", AsyncMock(return_value=HOME))


def _create_data(**overrides):
    # Home, whose row the fixture above stands in for.
    data = {
        "name": "daily digest",
        "instruction": "summarize the watchlist",
        "trigger_type": "cron",
        "cron_expression": "0 9 * * 1-5",
        "timezone": "UTC",
        "workspace_id": HOME,
    }
    data.update(overrides)
    return AutomationCreate(**data)


def _current_row(**overrides):
    row = {
        "automation_id": AUTOMATION_ID,
        "user_id": OWNER,
        "trigger_type": "cron",
        "cron_expression": "0 9 * * 1-5",
        "timezone": "UTC",
        "status": "active",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


class TestCreateTargetOwnership:
    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    @patch("src.server.services.automations.lifecycle.get_workspace")
    async def test_rejects_workspace_owned_by_another_user(
        self, mock_get_workspace, mock_auto_db,
    ):
        mock_get_workspace.return_value = {
            "workspace_id": WORKSPACE_ID, "user_id": OTHER,
        }

        with pytest.raises(TargetRefused) as exc:
            await create_automation(
                OWNER, _create_data(workspace_id=WORKSPACE_ID),
            )

        assert exc.value.status_code == 403
        assert exc.value.field == "workspace_id"
        mock_auto_db.create_automation.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    @patch("src.server.services.automations.lifecycle.get_workspace")
    async def test_rejects_workspace_that_does_not_exist(
        self, mock_get_workspace, mock_auto_db,
    ):
        mock_get_workspace.return_value = None

        with pytest.raises(TargetRefused) as exc:
            await create_automation(
                OWNER, _create_data(workspace_id=WORKSPACE_ID),
            )

        assert exc.value.status_code == 404
        assert exc.value.field == "workspace_id"
        mock_auto_db.create_automation.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    @patch(_THREAD_OWNER, new_callable=AsyncMock)
    async def test_rejects_pinned_thread_owned_by_another_user(
        self, mock_thread_owner, mock_auto_db,
    ):
        mock_thread_owner.return_value = OTHER

        with pytest.raises(TargetRefused) as exc:
            await create_automation(
                OWNER,
                _create_data(
                    thread_strategy="continue", conversation_thread_id=THREAD_ID,
                ),
            )

        assert exc.value.status_code == 403
        assert exc.value.field == "conversation_thread_id"
        mock_auto_db.create_automation.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    @patch(_THREAD_OWNER, new_callable=AsyncMock)
    async def test_rejects_pinned_thread_that_does_not_exist(
        self, mock_thread_owner, mock_auto_db,
    ):
        mock_thread_owner.return_value = None

        with pytest.raises(TargetRefused) as exc:
            await create_automation(
                OWNER,
                _create_data(
                    thread_strategy="continue", conversation_thread_id=THREAD_ID,
                ),
            )

        assert exc.value.status_code == 404
        assert exc.value.field == "conversation_thread_id"
        mock_auto_db.create_automation.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    @patch(_THREAD_OWNER, new_callable=AsyncMock)
    @patch(_THREAD_META, new=_thread_in(WORKSPACE_ID))
    @patch("src.server.services.automations.lifecycle.get_workspace")
    async def test_accepts_targets_the_caller_owns(
        self, mock_get_workspace, mock_thread_owner, mock_auto_db,
    ):
        mock_get_workspace.return_value = {
            "workspace_id": WORKSPACE_ID, "user_id": OWNER,
        }
        mock_thread_owner.return_value = OWNER
        mock_auto_db.create_automation = AsyncMock(
            return_value={"automation_id": AUTOMATION_ID}
        )

        await create_automation(
            OWNER,
            _create_data(
                workspace_id=WORKSPACE_ID,
                thread_strategy="continue",
                conversation_thread_id=THREAD_ID,
            ),
        )

        mock_auto_db.create_automation.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    @patch("src.server.services.automations.lifecycle.get_workspace")
    async def test_skips_lookup_when_no_targets_supplied(
        self, mock_get_workspace, mock_auto_db,
    ):
        mock_auto_db.create_automation = AsyncMock(
            return_value={"automation_id": AUTOMATION_ID}
        )

        await create_automation(OWNER, _create_data())

        mock_get_workspace.assert_not_called()


# ---------------------------------------------------------------------------
# update — the second door onto the same columns
# ---------------------------------------------------------------------------


class TestUpdateTargetOwnership:
    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    @patch("src.server.services.automations.lifecycle.get_workspace")
    async def test_rejects_repointing_at_another_users_workspace(
        self, mock_get_workspace, mock_auto_db,
    ):
        mock_auto_db.get_automation = AsyncMock(return_value=_current_row())
        mock_get_workspace.return_value = {
            "workspace_id": WORKSPACE_ID, "user_id": OTHER,
        }

        with pytest.raises(TargetRefused) as exc:
            await update_automation(
                AUTOMATION_ID, OWNER, {"workspace_id": WORKSPACE_ID},
            )

        assert exc.value.status_code == 403
        assert exc.value.field == "workspace_id"
        mock_auto_db.update_automation.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    @patch(_THREAD_OWNER, new_callable=AsyncMock)
    async def test_rejects_repointing_at_another_users_thread(
        self, mock_thread_owner, mock_auto_db,
    ):
        mock_auto_db.get_automation = AsyncMock(return_value=_current_row())
        mock_thread_owner.return_value = OTHER

        with pytest.raises(TargetRefused) as exc:
            await update_automation(
                AUTOMATION_ID, OWNER, {"conversation_thread_id": THREAD_ID},
            )

        assert exc.value.status_code == 403
        assert exc.value.field == "conversation_thread_id"
        mock_auto_db.update_automation.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    @patch("src.server.services.automations.lifecycle.get_workspace")
    async def test_untargeted_update_skips_the_lookup(
        self, mock_get_workspace, mock_auto_db,
    ):
        mock_auto_db.get_automation = AsyncMock(return_value=_current_row())
        mock_auto_db.update_automation = AsyncMock(return_value=_current_row())

        await update_automation(AUTOMATION_ID, OWNER, {"name": "renamed"})

        mock_get_workspace.assert_not_called()
        mock_auto_db.update_automation.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    @patch("src.server.services.automations.lifecycle.get_workspace")
    async def test_missing_automation_returns_none_without_checking_targets(
        self, mock_get_workspace, mock_auto_db,
    ):
        mock_auto_db.get_automation = AsyncMock(return_value=None)

        result = await update_automation(
            AUTOMATION_ID, OWNER, {"workspace_id": WORKSPACE_ID},
        )

        assert result is None
        mock_get_workspace.assert_not_called()


# ---------------------------------------------------------------------------
# A pinned thread is where the automation runs
# ---------------------------------------------------------------------------


class TestPinnedThreadPlace:
    """Runs continue in the pinned thread, so the thread must be in the
    workspace the row names, or the row would say one place and run in another."""

    @pytest.fixture(autouse=True)
    def _owned(self):
        workspace = AsyncMock(
            side_effect=lambda ws, conn=None: {"workspace_id": ws, "user_id": OWNER}
        )
        with (
            patch("src.server.services.automations.lifecycle.get_workspace", new=workspace),
            patch(_THREAD_OWNER, new=AsyncMock(return_value=OWNER)),
        ):
            yield

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    async def test_create_refuses_a_pin_in_another_workspace(self, mock_auto_db):
        with patch(_THREAD_META, new=_thread_in(WORKSPACE_ID)):
            with pytest.raises(AutomationRefusal) as exc:
                await create_automation(
                    OWNER,
                    _create_data(thread_strategy="continue", conversation_thread_id=THREAD_ID),
                )

        assert exc.value.field == "conversation_thread_id"
        assert str(exc.value) == (
            f"thread {THREAD_ID} is in workspace {WORKSPACE_ID}, but the automation "
            "runs in the user's Home: pin a thread from the user's Home, or run the "
            f"automation in workspace {WORKSPACE_ID}"
        )
        mock_auto_db.create_automation.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    async def test_update_refuses_a_new_pin_in_another_workspace(self, mock_auto_db):
        mock_auto_db.get_automation = AsyncMock(
            return_value=_current_row(workspace_id=HOME)
        )

        with patch(_THREAD_META, new=_thread_in(WORKSPACE_ID)):
            with pytest.raises(AutomationRefusal) as exc:
                await update_automation(
                    AUTOMATION_ID, OWNER, {"conversation_thread_id": THREAD_ID},
                )

        assert exc.value.field == "conversation_thread_id"
        mock_auto_db.update_automation.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    async def test_restating_the_stored_pin_checks_nothing(self, mock_auto_db):
        """Restating the stored pin without a move checks nothing, so an edit
        to anything else still saves."""
        mock_auto_db.get_automation = AsyncMock(
            return_value=_current_row(
                workspace_id=HOME,
                thread_strategy="continue",
                conversation_thread_id=uuid.UUID(THREAD_ID),
            )
        )
        mock_auto_db.update_automation = AsyncMock(return_value=_current_row())
        meta = _thread_in(WORKSPACE_ID)

        with patch(_THREAD_META, new=meta):
            await update_automation(
                AUTOMATION_ID,
                OWNER,
                {"name": "renamed", "workspace_id": HOME, "conversation_thread_id": THREAD_ID},
            )

        meta.assert_not_awaited()
        mock_auto_db.update_automation.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    async def test_switching_to_continue_checks_a_pin_left_on_the_row(self, mock_auto_db):
        """A row that started a new thread each run can hold a pin nothing
        checked; continuing it would run one place and name another."""
        mock_auto_db.get_automation = AsyncMock(
            return_value=_current_row(
                workspace_id=HOME,
                thread_strategy="new",
                conversation_thread_id=uuid.UUID(THREAD_ID),
            )
        )

        with patch(_THREAD_META, new=_thread_in(WORKSPACE_ID)):
            with pytest.raises(AutomationRefusal) as exc:
                await update_automation(
                    AUTOMATION_ID, OWNER, {"thread_strategy": "continue"},
                )

        assert exc.value.field == "conversation_thread_id"
        mock_auto_db.update_automation.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    async def test_a_pin_on_a_row_with_no_workspace_asks_for_one(self, mock_auto_db):
        """A row whose workspace was deleted runs nowhere, so a pin there
        needs a workspace named with it."""
        mock_auto_db.get_automation = AsyncMock(
            return_value=_current_row(workspace_id=None, thread_strategy="new")
        )

        with patch(_THREAD_META, new=_thread_in(WORKSPACE_ID)):
            with pytest.raises(AutomationRefusal) as exc:
                await update_automation(
                    AUTOMATION_ID,
                    OWNER,
                    {"thread_strategy": "continue", "conversation_thread_id": THREAD_ID},
                )

        assert exc.value.field == "workspace_id"
        assert str(exc.value) == lifecycle._NO_WORKSPACE
        mock_auto_db.update_automation.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    async def test_a_move_to_the_pinned_threads_workspace_keeps_the_pin(self, mock_auto_db):
        mock_auto_db.get_automation = AsyncMock(
            return_value=_current_row(
                workspace_id=HOME,
                thread_strategy="continue",
                conversation_thread_id=uuid.UUID(THREAD_ID),
            )
        )
        mock_auto_db.update_automation = AsyncMock(return_value=_current_row())

        with patch(_THREAD_META, new=_thread_in(WORKSPACE_ID)):
            await update_automation(
                AUTOMATION_ID,
                OWNER,
                {"workspace_id": WORKSPACE_ID, "conversation_thread_id": THREAD_ID},
            )

        written = mock_auto_db.update_automation.call_args.kwargs
        assert (written["workspace_id"], written["conversation_thread_id"]) == (
            WORKSPACE_ID,
            THREAD_ID,
        )


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------


class TestHome:
    """Home may be named before the user ever opened it, as a turn may name
    it; an account merge can leave its id on another account's row."""

    @pytest.fixture(autouse=True)
    def _home(self):
        """The real one, over the module's stand-in."""

    @pytest.fixture
    def upsert(self, monkeypatch):
        upsert = AsyncMock()
        monkeypatch.setattr(lifecycle, "get_or_create_home_workspace", upsert)
        return upsert

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    @patch("src.server.services.automations.lifecycle.get_workspace")
    async def test_home_is_made_when_the_user_has_none(
        self, mock_get_workspace, mock_auto_db, upsert,
    ):
        mock_get_workspace.return_value = None
        mock_auto_db.create_automation = AsyncMock(
            return_value={"automation_id": AUTOMATION_ID}
        )

        await create_automation(OWNER, _create_data())

        assert upsert.await_args.args == (OWNER,)
        assert mock_auto_db.create_automation.call_args.kwargs["workspace_id"] == HOME

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    @patch("src.server.services.automations.lifecycle.get_workspace")
    async def test_an_existing_home_is_not_written_again(
        self, mock_get_workspace, mock_auto_db, upsert,
    ):
        mock_get_workspace.return_value = {"workspace_id": HOME, "user_id": OWNER}
        mock_auto_db.create_automation = AsyncMock(
            return_value={"automation_id": AUTOMATION_ID}
        )

        await create_automation(OWNER, _create_data())

        upsert.assert_not_awaited()
        mock_auto_db.create_automation.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("held", ["row", "upsert"])
    @patch("src.server.services.automations.lifecycle.auto_db")
    @patch("src.server.services.automations.lifecycle.get_workspace")
    async def test_home_held_by_another_account_is_refused(
        self, mock_get_workspace, mock_auto_db, upsert, held,
    ):
        if held == "row":
            mock_get_workspace.return_value = {"workspace_id": HOME, "user_id": OTHER}
        else:
            mock_get_workspace.return_value = None
            upsert.side_effect = HomeWorkspaceTaken(HOME, OWNER)

        with pytest.raises(TargetRefused) as exc:
            await create_automation(OWNER, _create_data())

        assert (exc.value.status_code, exc.value.field) == (403, "workspace_id")
        mock_auto_db.create_automation.assert_not_called()


# DALTON-HOOK(automation-model): explicit null clears llm_model while omitted llm_model leaves it untouched
class TestAutomationModelUpdate:
    @pytest.mark.asyncio
    async def test_explicit_null_llm_model_clears_stored_model(self, monkeypatch):
        from datetime import datetime, timezone
        from unittest.mock import create_autospec
        from src.dalton.notify import dispatch as notify_dispatch
        from src.server.app import automations as automations_app

        now = datetime.now(timezone.utc)
        full_row = _current_row(
            name="daily digest",
            description=None,
            trigger_config=None,
            next_run_at=now,
            last_run_at=None,
            instruction="summarize the watchlist",
            workspace_id=uuid.UUID(HOME),
            llm_model="claude-sonnet-4-5",
            thread_strategy="new",
            conversation_thread_id=None,
            max_failures=3,
            failure_count=0,
            delivery_config=None,
            created_at=now,
            updated_at=now,
        )
        get_mock = create_autospec(
            lifecycle.auto_db.get_automation,
            return_value=full_row,
        )
        update_mock = create_autospec(
            lifecycle.auto_db.update_automation,
            return_value={**full_row, "llm_model": None},
        )
        notify_mock = create_autospec(
            notify_dispatch.populate_automation_notify_state,
            return_value=None,
        )
        monkeypatch.setattr(lifecycle.auto_db, "get_automation", get_mock)
        monkeypatch.setattr(lifecycle.auto_db, "update_automation", update_mock)
        monkeypatch.setattr(
            notify_dispatch, "populate_automation_notify_state", notify_mock
        )

        # DALTON-HOOK(company-automations): the route body is the raw payload.
        # DALTON-HOOK(automation-model): #656 made the route take a principal
        # (WorkspaceSendPrincipal) instead of a bare user_id; a session
        # principal carries the same owner.
        resp = await automations_app.update_automation(
            automation_id=uuid.UUID(AUTOMATION_ID),
            body={"llm_model": None},
            principal=Principal(user_id=OWNER, auth="session"),
            http_request=_stub_request(),
        )

        update_mock.assert_awaited_once()
        written = update_mock.call_args.kwargs
        assert "llm_model" in written
        assert written["llm_model"] is None
        assert resp.llm_model is None

    @pytest.mark.asyncio
    async def test_omitted_llm_model_leaves_stored_model_untouched(self, monkeypatch):
        from datetime import datetime, timezone
        from unittest.mock import create_autospec
        from src.dalton.notify import dispatch as notify_dispatch
        from src.server.app import automations as automations_app

        now = datetime.now(timezone.utc)
        full_row = _current_row(
            name="daily digest",
            description=None,
            trigger_config=None,
            next_run_at=now,
            last_run_at=None,
            instruction="summarize the watchlist",
            workspace_id=uuid.UUID(HOME),
            llm_model="claude-sonnet-4-5",
            thread_strategy="new",
            conversation_thread_id=None,
            max_failures=3,
            failure_count=0,
            delivery_config=None,
            created_at=now,
            updated_at=now,
        )
        get_mock = create_autospec(
            lifecycle.auto_db.get_automation,
            return_value=full_row,
        )
        update_mock = create_autospec(
            lifecycle.auto_db.update_automation,
            return_value={**full_row, "name": "renamed"},
        )
        notify_mock = create_autospec(
            notify_dispatch.populate_automation_notify_state,
            return_value=None,
        )
        monkeypatch.setattr(lifecycle.auto_db, "get_automation", get_mock)
        monkeypatch.setattr(lifecycle.auto_db, "update_automation", update_mock)
        monkeypatch.setattr(
            notify_dispatch, "populate_automation_notify_state", notify_mock
        )

        # DALTON-HOOK(company-automations): the route body is the raw payload.
        # DALTON-HOOK(automation-model): same principal-shaped call as above.
        resp = await automations_app.update_automation(
            automation_id=uuid.UUID(AUTOMATION_ID),
            body={"name": "renamed"},
            principal=Principal(user_id=OWNER, auth="session"),
            http_request=_stub_request(),
        )

        update_mock.assert_awaited_once()
        written = update_mock.call_args.kwargs
        assert "llm_model" not in written
        assert written["name"] == "renamed"
        assert resp.llm_model == "claude-sonnet-4-5"


class TestHomeStandInIsNotATarget:
    """A run in Home's stand-in would be an analyst's, with no away mark."""

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    @patch("src.server.services.automations.lifecycle.get_workspace")
    async def test_create_refuses_the_stand_in(self, mock_get_workspace, mock_auto_db):
        from src.server.database.home_workspace import get_home_stand_in_id

        with pytest.raises(TargetRefused) as exc:
            await create_automation(
                OWNER, _create_data(workspace_id=get_home_stand_in_id(OWNER).upper()),
            )

        assert (exc.value.status_code, exc.value.field) == (404, "workspace_id")
        mock_auto_db.create_automation.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.server.services.automations.lifecycle.auto_db")
    @patch("src.server.services.automations.lifecycle.get_workspace")
    async def test_update_refuses_the_stand_in(self, mock_get_workspace, mock_auto_db):
        from src.server.database.home_workspace import get_home_stand_in_id

        mock_auto_db.get_automation = AsyncMock(return_value=_current_row())

        with pytest.raises(TargetRefused) as exc:
            await update_automation(
                AUTOMATION_ID, OWNER, {"workspace_id": get_home_stand_in_id(OWNER)},
            )

        assert (exc.value.status_code, exc.value.field) == (404, "workspace_id")
        mock_auto_db.update_automation.assert_not_called()


class TestPhase1Row6WebWriteAndDeliveryWorkspace:
    """§3.2 Row 6 & G3: non-Home workspace_id and _delivery_workspace accept web write
    when Dalton auth is enabled, fail closed when disabled, while pinned threads stay owner-only."""

    @pytest.mark.asyncio
    async def test_require_target_ownership_and_delivery_workspace_gate(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        from uuid import UUID
        from unittest.mock import create_autospec
        from fastapi import HTTPException
        from src.dalton import settings as dalton_settings
        from src.dalton import sharing as dalton_sharing
        from src.server.app import automations as app_automations
        from src.server.services.automations import lifecycle as lc_mod

        ws_row = {"workspace_id": WORKSPACE_ID, "user_id": OTHER}
        orig_lc_get_ws = lc_mod.get_workspace
        monkeypatch.setattr(
            lc_mod,
            "get_workspace",
            create_autospec(orig_lc_get_ws, return_value=ws_row),
        )
        monkeypatch.setattr(
            app_automations,
            "get_workspace",
            create_autospec(orig_lc_get_ws, return_value=ws_row),
        )

        # G3 / Item 15: auth disabled OR coverage_units disabled -> fails closed with 403
        monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: False)
        monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: False)
        with pytest.raises(TargetRefused) as exc_off:
            await lc_mod.require_target_ownership(OWNER, WORKSPACE_ID, None)
        assert (exc_off.value.status_code, exc_off.value.field) == (403, "workspace_id")

        with pytest.raises(HTTPException) as exc_del_off:
            await app_automations._delivery_workspace(OWNER, UUID(WORKSPACE_ID))
        assert exc_del_off.value.status_code == 403

        monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: True)
        with pytest.raises(TargetRefused) as exc_units_off:
            await lc_mod.require_target_ownership(OWNER, WORKSPACE_ID, None)
        assert (exc_units_off.value.status_code, exc_units_off.value.field) == (403, "workspace_id")
        with pytest.raises(HTTPException) as exc_del_units_off:
            await app_automations._delivery_workspace(OWNER, UUID(WORKSPACE_ID))
        assert exc_del_units_off.value.status_code == 403

        # Both coverage_units + auth enabled & web write allowed: passes for workspace, still rejects foreign thread
        monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)
        monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: True)
        req_mock = create_autospec(dalton_sharing.require_workspace_access, return_value=ws_row)
        monkeypatch.setattr(dalton_sharing, "require_workspace_access", req_mock)

        await lc_mod.require_target_ownership(OWNER, WORKSPACE_ID, None)
        assert req_mock.await_args.kwargs["channel"] == "web"
        assert req_mock.await_args.kwargs["need"] == "write"

        del_ws = await app_automations._delivery_workspace(OWNER, UUID(WORKSPACE_ID))
        assert del_ws == WORKSPACE_ID

        # Pinned thread ownership remains strictly thread.user_id == caller
        monkeypatch.setattr(
            lc_mod,
            "require_thread_owner",
            create_autospec(
                lc_mod.require_thread_owner,
                side_effect=HTTPException(status_code=403, detail="Thread belongs to another user"),
            ),
        )
        with pytest.raises(TargetRefused) as exc_thread:
            await lc_mod.require_target_ownership(OWNER, WORKSPACE_ID, THREAD_ID)
        assert (exc_thread.value.status_code, exc_thread.value.field) == (
            403,
            "conversation_thread_id",
        )

        # ColleagueAccessRevokedError wraps cleanly
        req_mock.side_effect = dalton_sharing.ColleagueAccessRevokedError("revoked")
        with pytest.raises(TargetRefused) as exc_rev:
            await lc_mod.require_target_ownership(OWNER, WORKSPACE_ID, None)
        assert exc_rev.value.status_code == 403
        with pytest.raises(HTTPException) as exc_del_rev:
            await app_automations._delivery_workspace(OWNER, UUID(WORKSPACE_ID))
        assert exc_del_rev.value.status_code == 403


class TestItem9And17ManagedUnitLockAndLeadInTx:
    """Items 9 & 17: create/update/resume/restore/trigger lock unit FOR SHARE inside write tx and reject non-lead."""

    @pytest.mark.asyncio
    async def test_trigger_and_lifecycle_managed_unit_reject_non_lead_before_write_in_tx_order(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        from contextlib import asynccontextmanager
        from fastapi import HTTPException
        from psycopg import AsyncCursor
        from src.dalton import settings as dalton_settings
        from src.dalton.database import coverage_units as cu_db
        from src.server.database import automation_executions as exec_db

        monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)
        monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: True)

        events: list[str] = []
        inserts: list[str] = []

        class _TxConn:
            @asynccontextmanager
            async def transaction(self):
                events.append("BEGIN")
                yield self
                events.append("COMMIT")

            @asynccontextmanager
            async def cursor(self, **_kw):
                cur = create_autospec(AsyncCursor, instance=True)

                async def _exec(sql, params=None):
                    s = " ".join(str(sql).split())
                    if "FROM automations" in s and "FOR UPDATE" in s:
                        events.append("LOCK_AUTOMATIONS_FOR_UPDATE")
                        cur.fetchone.return_value = {
                            "automation_id": AUTOMATION_ID,
                            "user_id": OWNER,
                            "workspace_id": WORKSPACE_ID,
                            "name": "dalton-nvda-watch",
                            "metadata": {"dalton_kind": "company_watch", "ticker": "NVDA"},
                            "status": "active",
                            "deleted_at": None,
                        }
                    elif "FROM automations" in s:
                        cur.fetchone.return_value = {"workspace_id": WORKSPACE_ID}
                    elif "FROM dalton_coverage_units" in s:
                        events.append(
                            "LOCK_UNIT_FOR_SHARE" if "FOR SHARE" in s else "READ_UNIT"
                        )
                        cur.fetchone.return_value = {
                            "unit_id": "u-unit-1",
                            "workspace_id": WORKSPACE_ID,
                            "owner_user_id": OWNER,
                            "lead_analyst_user_id": OTHER,
                            "status": "active",
                            "kind": "company",
                            "ticker": "NVDA",
                        }
                    elif "INSERT INTO automation_executions" in s:
                        inserts.append(s)
                        events.append("INSERT_EXECUTION")

                cur.execute.side_effect = _exec
                yield cur

        tx_conn = _TxConn()

        @asynccontextmanager
        async def _get_conn(c=None):
            yield c if c is not None else tx_conn

        monkeypatch.setattr(exec_db, "get_db_connection", _get_conn)
        monkeypatch.setattr(cu_db, "get_db_connection", _get_conn)

        with pytest.raises(HTTPException) as exc_trig:
            await exec_db.create_execution_for_live_parent(
                AUTOMATION_ID, OWNER, __import__("datetime").datetime.now(__import__("datetime").timezone.utc), "srv-1"
            )
        assert exc_trig.value.status_code == 403
        # Rejects before inserting pending execution, and acquires unit FOR SHARE before automations FOR UPDATE
        assert inserts == []
        assert events.index("LOCK_UNIT_FOR_SHARE") < events.index("LOCK_AUTOMATIONS_FOR_UPDATE")

        # Also test create_automation rejects non-lead managed task inside tx
        monkeypatch.setattr(
            "src.server.database.pool.get_db_connection",
            _get_conn,
        )
        ws_row = {"workspace_id": WORKSPACE_ID, "user_id": OWNER}
        monkeypatch.setattr(lifecycle, "get_workspace", AsyncMock(return_value=ws_row))
        from src.dalton import sharing as dalton_sharing

        monkeypatch.setattr(
            dalton_sharing, "require_workspace_access", AsyncMock(return_value=ws_row)
        )
        monkeypatch.setattr(lifecycle.auto_db, "create_automation", AsyncMock())
        with pytest.raises(TargetRefused) as exc_create:
            await create_automation(
                OWNER,
                _create_data(
                    name="dalton-nvda-watch",
                    workspace_id=WORKSPACE_ID,
                    metadata={"dalton_kind": "company_watch", "ticker": "NVDA"},
                ),
            )
        assert exc_create.value.status_code == 403
        lifecycle.auto_db.create_automation.assert_not_awaited()
