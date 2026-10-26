"""The sweep acting on a folder a workspace move left on its old computer.

The listing decides what each row needs (pinned on Postgres in
``tests/integration/test_workspace_move_db.py``); these pin that the sweep
does only that. A row whose computer is gone, or whose files just came back,
is a mark and never a sandbox call. A removal runs only on a computer already
running, under its folder lock, on a path only a move sets aside, and is
marked only once the folder is gone.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.server.services.computer_manager import ComputerManager
from src.server.services.computer_manager._move import _LEFTOVERS_RM_TIMEOUT_S

_MOVE = "src.server.services.computer_manager._move"
MOVE_ID = "66666666-6666-4666-8666-666666666666"
SOURCE = "77777777-7777-4777-8777-777777777777"
ROOT = "/data/root"
AWAY = f"_internal/leftovers/move-{MOVE_ID}"


def _row(action, **overrides):
    row = {
        "move_id": MOVE_ID,
        "source_computer_id": SOURCE,
        "leftovers_path": AWAY,
        "finished_at": None,
        "action": action,
    }
    row.update(overrides)
    return row


def _computer(**overrides):
    computer = {
        "computer_id": SOURCE,
        "user_id": "user-1",
        "kind": "docker",
        "status": "running",
        "provider_ref": "sb-old",
        "root_dir": ROOT,
    }
    computer.update(overrides)
    return computer


@pytest.fixture
def sweep():
    """A manager whose sandbox, folder lock and marks record what they were asked, in order."""
    events = []
    state = SimpleNamespace(lock_conn=object(), exit_code=0)
    runtime = MagicMock()

    async def run(command, timeout=None):
        events.append(("exec", command, timeout))
        return SimpleNamespace(exit_code=state.exit_code, stderr="rm: refused")

    runtime.exec = AsyncMock(side_effect=run)

    @asynccontextmanager
    async def detached(provider_ref, *, binding=None):
        events.append(("runtime", provider_ref))
        yield runtime

    @asynccontextmanager
    async def folders_lock(computer_id, *, wait_s=None):
        events.append(("lock", computer_id, wait_s))
        yield state.lock_conn
        events.append(("unlock", computer_id))

    async def removed(move_id):
        events.append(("removed", move_id))

    manager = ComputerManager(SimpleNamespace(sandbox=SimpleNamespace(provider="docker")))
    manager._detached_runtime = detached
    with (
        patch(f"{_MOVE}.get_computer", AsyncMock(return_value=_computer())) as computer,
        patch(f"{_MOVE}.workspace_folders_lock", folders_lock),
        patch(f"{_MOVE}.mark_leftovers_removed", AsyncMock(side_effect=removed)) as gone,
        patch(f"{_MOVE}.mark_move_restored", AsyncMock()) as restored,
    ):
        yield SimpleNamespace(
            manager=manager,
            runtime=runtime,
            events=events,
            state=state,
            get_computer=computer,
            mark_removed=gone,
            mark_restored=restored,
        )


@pytest.mark.asyncio
async def test_a_folder_gone_with_its_computer_is_marked_without_a_sandbox(sweep):
    await sweep.manager._reap_move_leftovers(_row("source_gone"))

    sweep.mark_removed.assert_awaited_once_with(MOVE_ID)
    sweep.mark_restored.assert_not_awaited()
    sweep.get_computer.assert_not_awaited()
    sweep.runtime.exec.assert_not_awaited()
    assert sweep.events == [("removed", MOVE_ID)]


@pytest.mark.asyncio
async def test_files_back_in_place_start_the_wait_without_a_sandbox(sweep):
    await sweep.manager._reap_move_leftovers(_row("restored"))

    sweep.mark_restored.assert_awaited_once_with(MOVE_ID)
    sweep.mark_removed.assert_not_awaited()
    sweep.get_computer.assert_not_awaited()
    sweep.runtime.exec.assert_not_awaited()
    assert sweep.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "Research",
        "_internal/leftovers/ws-a",
        "_internal/leftovers",
        f"{ROOT}/_internal/leftovers/move-{MOVE_ID}",
    ],
    ids=["live-folder", "delete-leftovers", "leftovers-root", "absolute"],
)
async def test_a_removal_outside_a_moves_leftovers_is_left_alone(sweep, path):
    await sweep.manager._reap_move_leftovers(_row("remove", leftovers_path=path))

    sweep.get_computer.assert_not_awaited()
    sweep.runtime.exec.assert_not_awaited()
    sweep.mark_removed.assert_not_awaited()
    sweep.mark_restored.assert_not_awaited()
    assert sweep.events == []


@pytest.mark.asyncio
async def test_a_removal_runs_under_the_folder_lock_then_marks_the_row(sweep):
    await sweep.manager._reap_move_leftovers(_row("remove"))

    sweep.get_computer.assert_awaited_once_with(SOURCE)
    rm = ("exec", f"rm -rf {ROOT}/{AWAY}", _LEFTOVERS_RM_TIMEOUT_S)
    assert ("runtime", "sb-old") in sweep.events
    # Never waited for: a holder elsewhere is likely removing the same folder.
    assert [e for e in sweep.events if e[0] != "runtime"] == [
        ("lock", SOURCE, 0),
        rm,
        ("unlock", SOURCE),
        ("removed", MOVE_ID),
    ]
    sweep.mark_restored.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "computer",
    [
        _computer(status="stopped"),
        _computer(status="starting"),
        _computer(provider_ref=None),
        None,
    ],
    ids=["stopped", "starting", "no-sandbox", "row-gone"],
)
async def test_a_removal_waits_for_a_computer_already_running(sweep, computer):
    """No computer is started to clear a folder; the row stays listed for later."""
    sweep.get_computer.return_value = computer

    await sweep.manager._reap_move_leftovers(_row("remove"))

    sweep.runtime.exec.assert_not_awaited()
    sweep.mark_removed.assert_not_awaited()
    assert sweep.events == []


@pytest.mark.asyncio
async def test_a_busy_folder_lock_defers_the_removal(sweep):
    sweep.state.lock_conn = None

    await sweep.manager._reap_move_leftovers(_row("remove"))

    sweep.runtime.exec.assert_not_awaited()
    sweep.mark_removed.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_removal_is_not_marked(sweep):
    sweep.state.exit_code = 1

    with pytest.raises(RuntimeError):
        await sweep.manager._reap_move_leftovers(_row("remove"))

    sweep.runtime.exec.assert_awaited_once()
    sweep.mark_removed.assert_not_awaited()
