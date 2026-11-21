"""Where an automation's turn runs: the row it names, routed as any turn's."""

from unittest.mock import AsyncMock, patch

import pytest

from src.server.database.home_workspace import get_home_workspace_id
from src.server.services.automation_executor import AutomationExecutor, _Place, _turn_place
from src.server.services.turn_runtime import turn_route

_USER = "user-1"
_WS = "11111111-1111-1111-1111-111111111111"
_THREAD = "22222222-2222-2222-2222-222222222222"
_HOME = get_home_workspace_id(_USER)
_PROJECT = {"workspace_id": _WS, "user_id": _USER, "status": "running"}
_HOME_ROW = {"workspace_id": _HOME, "user_id": _USER, "status": "unbound"}


def _automation(**overrides):
    return {
        "user_id": _USER,
        "workspace_id": _HOME,
        "thread_strategy": "continue",
        "conversation_thread_id": _THREAD,
        **overrides,
    }


async def _route(automation, *, thread_meta=None, workspace=None):
    with (
        patch(
            "src.server.database.conversation.get_thread_auth_meta",
            new=AsyncMock(return_value=thread_meta),
        ),
        patch(
            "src.server.database.workspace.get_workspace",
            new=AsyncMock(return_value=workspace),
        ),
    ):
        route = turn_route(_USER, (await _turn_place(automation)).workspace)
    return route.workspace_id, route.role


@pytest.mark.asyncio
async def test_continuing_a_workspace_thread_runs_that_workspaces_analyst():
    assert await _route(
        _automation(workspace_id=_WS),
        thread_meta={"user_id": _USER, "workspace_id": _WS},
        workspace=_PROJECT,
    ) == (_WS, "analyst")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "automation, thread_meta",
    [
        (_automation(), {"user_id": _USER, "workspace_id": _HOME}),
        (_automation(conversation_thread_id=None), None),
        (_automation(thread_strategy="new"), {"user_id": _USER, "workspace_id": _WS}),
    ],
    ids=["home-thread", "not-pinned-yet", "new-thread-each-run"],
)
async def test_any_other_firing_of_a_home_automation_runs_in_home(automation, thread_meta):
    assert await _route(
        automation, thread_meta=thread_meta, workspace=_HOME_ROW
    ) == (_HOME, "chief_of_staff")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workspace, expected",
    [(_PROJECT, (_WS, "analyst")), (_HOME_ROW, (_HOME, "chief_of_staff"))],
    ids=["workspace", "chief-of-staff-home"],
)
async def test_a_firing_runs_in_its_stored_workspace(workspace, expected):
    automation = _automation(workspace_id=workspace["workspace_id"])
    assert await _route(automation, workspace=workspace) == expected


@pytest.mark.asyncio
async def test_a_firing_whose_workspace_is_gone_fails_before_any_thread():
    with pytest.raises(ValueError, match="may have been deleted"):
        await _route(_automation(workspace_id=_WS), workspace=None)



# One rule for every row: its stored workspace, continuing the pin only there.

_OTHER_WS = "33333333-3333-3333-3333-333333333333"
_OTHER = {"workspace_id": _OTHER_WS, "user_id": _USER, "status": "running"}


async def _place_by_id(automation, *, thread_meta, rows):
    """As ``_route``, with each workspace read answered by its id, and the
    thread the run continues (None starts one)."""
    with (
        patch(
            "src.server.database.conversation.get_thread_auth_meta",
            new=AsyncMock(return_value=thread_meta),
        ),
        patch(
            "src.server.database.workspace.get_workspace",
            new=AsyncMock(side_effect=lambda ws_id: rows.get(ws_id)),
        ),
    ):
        place = await _turn_place(automation)
    route = turn_route(_USER, place.workspace)
    return route.workspace_id, route.role, place.thread_id


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [_WS, _HOME], ids=["workspace", "home"])
@pytest.mark.parametrize(
    "thread_meta",
    [
        {"user_id": _USER, "workspace_id": _OTHER_WS},
        {"user_id": _USER, "workspace_id": "44444444-4444-4444-4444-444444444444"},
        {"user_id": "someone-else", "workspace_id": _WS},
    ],
    ids=["another-workspace", "a-deleted-workspace", "another-account"],
)
async def test_a_pin_outside_the_stored_workspace_starts_a_thread_there(stored, thread_meta):
    """The automation runs where it lives; only a thread there is continued."""
    rows = {_WS: _PROJECT, _HOME: _HOME_ROW, _OTHER_WS: _OTHER}
    expected = (_WS, "analyst") if stored == _WS else (_HOME, "chief_of_staff")
    assert await _place_by_id(
        _automation(workspace_id=stored), thread_meta=thread_meta, rows=rows
    ) == (*expected, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "automation, expected",
    [
        (_automation(workspace_id=_WS), (_WS, "analyst")),
        (_automation(workspace_id=_HOME), (_HOME, "chief_of_staff")),
    ],
    ids=["workspace-row", "home-row"],
)
async def test_a_pin_in_the_stored_workspace_is_continued(automation, expected):
    thread_meta = {"user_id": _USER, "workspace_id": automation["workspace_id"]}
    assert await _place_by_id(
        automation, thread_meta=thread_meta, rows={_WS: _PROJECT, _HOME: _HOME_ROW}
    ) == (*expected, _THREAD)


@pytest.mark.asyncio
async def test_a_pinned_firing_whose_workspace_is_gone_still_fails():
    automation = _automation(workspace_id=_WS)
    with pytest.raises(ValueError, match="may have been deleted"):
        await _place_by_id(
            automation,
            thread_meta={"user_id": _USER, "workspace_id": _OTHER_WS},
            rows={_HOME: _HOME_ROW},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "automation, expected",
    [
        (_automation(workspace_id=_WS), (_WS, "analyst")),
        (_automation(workspace_id=_HOME), (_HOME, "chief_of_staff")),
    ],
    ids=["workspace-row", "home-row"],
)
async def test_a_pinned_thread_that_is_gone_is_made_again_where_the_row_lives(automation, expected):
    assert await _place_by_id(
        automation, thread_meta=None, rows={_WS: _PROJECT, _HOME: _HOME_ROW}
    ) == (*expected, _THREAD)


@pytest.mark.asyncio
async def test_a_run_that_leaves_its_pin_behind_pins_the_thread_it_starts():
    """So the next run continues the new thread, not the one it left."""
    executor = AutomationExecutor()
    automation = _automation(workspace_id=_WS, automation_id="auto-1")
    with (
        patch("src.server.services.automation_executor.auto_db") as auto_db,
        patch.object(executor, "_precreate_titled_thread", new=AsyncMock()),
    ):
        auto_db.update_automation = AsyncMock()
        thread_id = await executor._resolve_thread(automation, _Place(_PROJECT, None), _WS)

    assert thread_id != _THREAD
    auto_db.update_automation.assert_awaited_once_with(
        "auto-1", _USER, conversation_thread_id=thread_id
    )


@pytest.mark.asyncio
async def test_phase1_colleague_executor_web_write_precreate_and_revoked_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    """§3.2 Rows 1-3, G2, G3, G4:
    - Auth enabled + web='write': _stored_workspace & _resolve_thread allow colleague;
      _precreate_titled_thread explicitly passes automation.user_id.
    - ColleagueAccessRevokedError / HTTPException(403/404) map to ValueError cleanly.
    - Auth disabled (G3): non-owner colleague fails closed with ValueError.
    """
    from unittest.mock import create_autospec
    from fastapi import HTTPException
    from src.dalton import settings as dalton_settings
    from src.dalton import sharing as dalton_sharing
    from src.server.database import conversation as conv_db
    from src.server.database import workspace as ws_db
    from src.server.services import automation_executor as exec_mod

    ws_row = {"workspace_id": _WS, "user_id": "owner-b", "status": "running"}
    auto = _automation(
        automation_id="auto-1",
        name="Morning Brief",
        workspace_id=_WS,
        user_id="colleague-a",
        conversation_thread_id=None,
    )

    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, return_value=ws_row),
    )

    # G3 / Item 15: auth disabled OR coverage_units disabled -> non-owner fails closed
    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: False)
    monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: False)
    with pytest.raises(ValueError, match="does not belong to the automation owner"):
        await exec_mod._stored_workspace(auto)
    monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: True)
    with pytest.raises(ValueError, match="does not belong to the automation owner"):
        await exec_mod._stored_workspace(auto)
    executor = exec_mod.AutomationExecutor()
    with pytest.raises(ValueError, match="does not belong to the automation owner"):
        await executor._resolve_thread(auto, _Place(ws_row, None), _WS)

    # Both coverage_units + auth enabled and colleague web write allowed -> passes, and _precreate_titled_thread passes user_id
    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: True)
    req_mock = create_autospec(dalton_sharing.require_workspace_access, return_value=ws_row)
    monkeypatch.setattr(dalton_sharing, "require_workspace_access", req_mock)
    create_thread_mock = create_autospec(conv_db.create_thread, return_value=None)
    monkeypatch.setattr(conv_db, "create_thread", create_thread_mock)

    resolved_ws = await exec_mod._stored_workspace(auto)
    assert resolved_ws["workspace_id"] == _WS
    assert req_mock.await_args.kwargs["channel"] == "web"
    assert req_mock.await_args.kwargs["need"] == "write"

    await executor._precreate_titled_thread(auto, "thread-new-1", _WS)
    assert create_thread_mock.await_args.kwargs["user_id"] == "colleague-a"

    # G4: ColleagueAccessRevokedError / HTTPException(403/404) -> ValueError
    for exc in (
        dalton_sharing.ColleagueAccessRevokedError("revoked"),
        HTTPException(status_code=403, detail="forbidden"),
        HTTPException(status_code=404, detail="missing"),
    ):
        req_mock.side_effect = exc
        with pytest.raises(ValueError, match="does not belong to the automation owner"):
            await exec_mod._stored_workspace(auto)
        with pytest.raises(ValueError, match="does not belong to the automation owner"):
            await executor._resolve_thread(auto, _Place(ws_row, None), _WS)


@pytest.mark.asyncio
async def test_phase1_c3_handed_thread_and_post_wait_reverification_and_run_metadata(
    monkeypatch: pytest.MonkeyPatch,
):
    """§3.2 Row 3 & C3:
    - Cross-user _run_turn stamps workspace_owner_id and origin_channel='web'.
    - handed_thread path refuses thread whose workspace_id or user_id differs from automation.
    - _run_turn post-wait refuses drifted thread/owner before starting another workflow turn.
    """
    from unittest.mock import create_autospec
    from fastapi import HTTPException
    from src.dalton import settings as dalton_settings
    from src.dalton import sharing as dalton_sharing
    from src.server.database import conversation as conv_db
    from src.server.database import workspace as ws_db
    from src.server.services import automation_executor as exec_mod

    ws_row = {"workspace_id": _WS, "user_id": "owner-b", "status": "running"}
    auto = _automation(
        automation_id="auto-c3",
        name="Morning Brief",
        instruction="Run brief",
        workspace_id=_WS,
        user_id="colleague-a",
        conversation_thread_id=None,
    )
    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: True)
    orig_get_ws = ws_db.get_workspace
    monkeypatch.setattr(
        ws_db, "get_workspace", create_autospec(orig_get_ws, return_value=ws_row)
    )
    monkeypatch.setattr(
        dalton_sharing,
        "require_workspace_access",
        create_autospec(dalton_sharing.require_workspace_access, return_value=ws_row),
    )

    executor = exec_mod.AutomationExecutor()
    # C3 handed_thread mismatch -> ValueError
    orig_get_meta = conv_db.get_thread_auth_meta
    get_meta_mock = create_autospec(
        orig_get_meta,
        return_value={"workspace_id": "ws-other", "user_id": "colleague-a"},
    )
    monkeypatch.setattr(conv_db, "get_thread_auth_meta", get_meta_mock)
    with pytest.raises(ValueError, match="does not match automation workspace or owner"):
        await exec_mod._verify_thread_matches_automation(
            "t-handed", auto, expected_workspace_id=_WS
        )

    # C3 _run_turn cross-user metadata + post-wait drift rejection
    captured_calls: list[dict] = []

    async def _turn_stream(**kwargs):
        captured_calls.append(kwargs)
        yield "event: done\ndata: {}\n\n"

    monkeypatch.setattr(
        "src.server.handlers.chat.astream_ptc_workflow",
        _turn_stream,
    )
    route = await turn_route("colleague-a", ws_row).bind("colleague-a", ws_row)
    firing = exec_mod._Firing(
        automation=auto,
        workspace_owner_id="owner-b",
        route=route,
        thread_id="t-colleague",
    )
    get_meta_mock.return_value = {"workspace_id": _WS, "user_id": "colleague-a"}
    ok = await executor._run_turn("exec-c3", firing, has_byok=True, deadline=10**9)
    assert ok is True
    assert len(captured_calls) == 1
    assert captured_calls[0]["run_metadata"]["workspace_owner_id"] == "owner-b"
    assert captured_calls[0]["run_metadata"]["origin_channel"] == "web"
    assert captured_calls[0]["extra_configurable"]["workspace_owner_id"] == "owner-b"

    # When _run_turn catches 403/ColleagueAccessRevokedError from astream_ptc_workflow -> ValueError (G4)
    async def _turn_403(**kwargs):
        raise HTTPException(status_code=403, detail="revoked mid-start")
        yield

    monkeypatch.setattr("src.server.handlers.chat.astream_ptc_workflow", _turn_403)
    with pytest.raises(ValueError, match="revoked mid-start"):
        await executor._run_turn("exec-c3-deny", firing, has_byok=True, deadline=10**9)

    # Post-wait drift in thread auth meta raises ValueError
    async def _turn_lost(**kwargs):
        from src.server.database.runs.lifecycle import RunSlotBusyError
        raise RunSlotBusyError("t-colleague")
        yield

    monkeypatch.setattr("src.server.handlers.chat.astream_ptc_workflow", _turn_lost)
    wait_mock = create_autospec(executor._wait_for_thread, return_value=auto)
    monkeypatch.setattr(executor, "_wait_for_thread", wait_mock)
    get_meta_mock.return_value = {"workspace_id": _WS, "user_id": "someone-else"}
    with pytest.raises(ValueError, match="does not match automation workspace or owner"):
        await executor._run_turn("exec-c3-drift", firing, has_byok=True, deadline=10**9)

    # Item 11: post-wait workspace move refuses reusing old thread without calling workflow again
    ws_other = "ws-other-999"
    ws_other_row = {"workspace_id": ws_other, "user_id": "colleague-a", "status": "running"}
    moved_auto = {**auto, "workspace_id": ws_other}

    async def _get_ws_switch(wid, **_kw):
        return ws_other_row if str(wid) == ws_other else ws_row

    monkeypatch.setattr(
        ws_db, "get_workspace", create_autospec(orig_get_ws, side_effect=_get_ws_switch)
    )
    wait_mock.return_value = moved_auto
    get_meta_mock.return_value = {"workspace_id": _WS, "user_id": "colleague-a"}
    with pytest.raises(ValueError, match="workspace changed while waiting"):
        await executor._run_turn("exec-c3-ws-drift", firing, has_byok=True, deadline=10**9)

    # Item 11: post-wait pinned thread change refuses reusing old thread
    rebound_pin_auto = {**auto, "thread_strategy": "continue", "conversation_thread_id": "t-new-pin"}
    wait_mock.return_value = rebound_pin_auto
    with pytest.raises(ValueError, match="pinned thread changed while waiting"):
        await executor._run_turn("exec-c3-pin-drift", firing, has_byok=True, deadline=10**9)


@pytest.mark.asyncio
async def test_phase1_handed_thread_execute_refuses_mismatched_thread_and_settles_cleanly(
    monkeypatch: pytest.MonkeyPatch,
):
    """Item 16 / C3: real AutomationExecutor.execute() with handed_thread rejects wrong workspace and settles FAILED."""
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock, create_autospec
    from src.dalton import settings as dalton_settings
    from src.server.database import automation_executions as exec_db
    from src.server.database import conversation as conv_db
    from src.server.database import workspace as ws_db
    from src.server.services import automation_executor as exec_mod

    ws_row = {"workspace_id": _WS, "user_id": "owner-a", "status": "running"}
    auto = _automation(
        automation_id="11111111-1111-1111-1111-111111111111",
        name="Morning Brief",
        instruction="Run brief",
        workspace_id=_WS,
        user_id="owner-a",
        conversation_thread_id=None,
    )
    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: True)
    orig_get_ws = ws_db.get_workspace
    monkeypatch.setattr(
        ws_db, "get_workspace", create_autospec(orig_get_ws, return_value=ws_row)
    )
    monkeypatch.setattr(
        conv_db,
        "get_thread_auth_meta",
        create_autospec(
            conv_db.get_thread_auth_meta,
            return_value={"workspace_id": "ws-stale-other", "user_id": "owner-a"},
        ),
    )
    monkeypatch.setattr(
        exec_db,
        "begin_execution_if_not_deleted",
        create_autospec(
            exec_db.begin_execution_if_not_deleted,
            return_value={
                "conversation_thread_id": "t-handed-bad",
                "conversation_response_id": None,
                "scheduled_at": datetime.now(timezone.utc),
                "wait_reason": None,
                "awaits_computer": False,
                "checked_computer_id": None,
            },
        ),
    )
    monkeypatch.setattr(
        exec_db, "get_execution_for_settlement", AsyncMock(return_value={"conversation_thread_id": "t-handed-bad"})
    )
    monkeypatch.setattr(exec_mod, "park_for_computer", AsyncMock(return_value=False))
    monkeypatch.setattr(exec_mod, "_credentials", AsyncMock(return_value=(True, True)))
    monkeypatch.setattr(exec_mod, "enforce_credit_limit", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "src.server.services.account.read_standing",
        AsyncMock(return_value=type("S", (), {"refuse_if_suspended": lambda self: None})()),
    )
    settle_mock = create_autospec(exec_mod.settle, return_value=True)
    monkeypatch.setattr(exec_mod, "settle", settle_mock)

    executor = exec_mod.AutomationExecutor()
    monkeypatch.setattr(executor, "_start_delivery_unstarted", AsyncMock())
    await executor.execute(auto, "exec-handed-bad")

    settle_mock.assert_awaited_once()
    call_args = settle_mock.await_args
    assert call_args.args[2] == exec_mod.Outcome.FAILED
    assert "does not match automation workspace or owner" in (call_args.kwargs.get("error") or "")


@pytest.mark.asyncio
async def test_post_wait_thread_strategy_changed_from_continue_to_new_settles_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Item 11 + Round 5 Item 7: cancelling fixed/continue thread while waiting
    settles as Outcome.SKIPPED (skip_reason='user') instead of raising ValueError/FAILED."""
    from unittest.mock import create_autospec

    from src.server.database import conversation as conv_db
    from src.server.database import workspace as ws_db
    from src.server.services import automation_executor as exec_mod
    from src.server.services.turn_runtime import turn_route

    ws_id = "11111111-1111-1111-1111-111111111111"
    ws_row = {"workspace_id": ws_id, "user_id": "owner-a", "status": "running"}
    auto = {
        "automation_id": "auto-r4-11",
        "user_id": "owner-a",
        "workspace_id": ws_id,
        "name": "continue-task",
        "instruction": "run",
        "thread_strategy": "continue",
        "conversation_thread_id": "t-pinned-old",
        "trigger_type": "cron",
        "delivery_config": {},
        "metadata": {},
    }
    monkeypatch.setattr(
        ws_db, "get_workspace", create_autospec(ws_db.get_workspace, return_value=ws_row)
    )
    monkeypatch.setattr(
        conv_db,
        "get_thread_auth_meta",
        create_autospec(
            conv_db.get_thread_auth_meta,
            return_value={"workspace_id": ws_id, "user_id": "owner-a"},
        ),
    )
    monkeypatch.setattr(
        "src.server.services.account.read_standing",
        lambda *_a, **_kw: _async_val(type("S", (), {"refuse_if_suspended": lambda self: None})()),
    )

    async def _turn_lost(**kwargs):
        from src.server.database.runs.lifecycle import RunSlotBusyError

        raise RunSlotBusyError("t-pinned-old")
        yield

    monkeypatch.setattr("src.server.handlers.chat.astream_ptc_workflow", _turn_lost)
    executor = exec_mod.AutomationExecutor()
    fresh_new_strategy = {
        **auto,
        "thread_strategy": "new",
        "conversation_thread_id": None,
    }
    wait_mock = create_autospec(executor._wait_for_thread, return_value=fresh_new_strategy)
    monkeypatch.setattr(executor, "_wait_for_thread", wait_mock)

    # Round 6 Item 4: go through REAL settlement.settle (stubbing only DB layer & delete-check)
    from src.server.services import automation_settlement as settlement_mod
    from src.server.services.automation_outcomes import Outcome, POLICIES

    assert POLICIES[Outcome.SKIPPED].from_statuses == ("waiting",)

    exec_rows: dict[str, dict[str, str]] = {
        "exec-r4-11": {"status": "running", "skip_reason": ""},
        "exec-r5-pin-cleared": {"status": "running", "skip_reason": ""},
        "exec-r6-already-done": {"status": "completed", "skip_reason": ""},
    }

    async def _fake_settle_execution(
        execution_id,
        *,
        automation_id,
        from_statuses,
        to,
        skip_reason=None,
        **_kw,
    ):
        row = exec_rows[execution_id]
        if row["status"] not in from_statuses:
            return None
        prior = row["status"]
        row["status"] = to
        row["skip_reason"] = skip_reason or ""
        return {
            "automation_execution_id": execution_id,
            "automation_id": automation_id,
            "status": to,
            "settled_from": prior,
            "conversation_thread_id": "t-pinned-old",
            "conversation_response_id": None,
            "delivery": None,
            "result_excerpt": None,
        }

    monkeypatch.setattr(settlement_mod.exec_db, "settle_execution", _fake_settle_execution)
    monkeypatch.setattr(
        "src.dalton.database.automation_recycle.automation_is_soft_deleted",
        create_autospec(
            __import__("src.dalton.database.automation_recycle", fromlist=["automation_is_soft_deleted"]).automation_is_soft_deleted,
            return_value=False,
        ),
    )

    route = await turn_route("owner-a", ws_row).bind("owner-a", ws_row)
    firing = exec_mod._Firing(
        automation=auto,
        workspace_owner_id="owner-a",
        route=route,
        thread_id="t-pinned-old",
    )
    ran = await executor._run_turn("exec-r4-11", firing, has_byok=True, deadline=10**9)
    assert ran is False
    assert exec_rows["exec-r4-11"] == {"status": "skipped", "skip_reason": "user"}

    # Also test clearing pinned thread while keeping strategy -> settles as SKIPPED
    wait_mock.return_value = {**auto, "thread_strategy": "continue", "conversation_thread_id": None}
    firing2 = exec_mod._Firing(
        automation=dict(auto),
        workspace_owner_id="owner-a",
        route=route,
        thread_id="t-pinned-old",
    )
    ran_cleared = await executor._run_turn("exec-r5-pin-cleared", firing2, has_byok=True, deadline=10**9)
    assert ran_cleared is False
    assert exec_rows["exec-r5-pin-cleared"] == {"status": "skipped", "skip_reason": "user"}

    # If execution already moved out of 'running', settle returns False and executor logs warning
    warnings: list[str] = []
    monkeypatch.setattr(
        exec_mod.logger,
        "warning",
        lambda msg, *a, **kw: warnings.append(msg % a if a else str(msg)),
    )
    wait_mock.return_value = dict(fresh_new_strategy)
    firing3 = exec_mod._Firing(
        automation=dict(auto),
        workspace_owner_id="owner-a",
        route=route,
        thread_id="t-pinned-old",
    )
    ran_done = await executor._run_turn("exec-r6-already-done", firing3, has_byok=True, deadline=10**9)
    assert ran_done is False
    assert exec_rows["exec-r6-already-done"]["status"] == "completed"
    assert any("exec-r6-already-done" in w for w in warnings)


async def _async_val(val):
    return val
