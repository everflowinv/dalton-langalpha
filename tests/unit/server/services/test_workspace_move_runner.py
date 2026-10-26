"""The move runner where it stops short: a lost row, and a folder to put back.

A runner that loses its row mid-backup lets go of the source at once rather
than when the backup ends. A rollback restores from the mirror only once the
folder cannot come back, because the mirror lacks the folder's .git and
dependencies; until then the row stays staged and the sweep tries again.
"""

import asyncio
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.server.services.computer_manager import ComputerManager
from src.server.services.computer_errors import ComputerBusyError
from src.server.services.computer_manager._move import _PUT_BACK_GIVE_UP_S, _folder_held
from src.server.services.workspace_move import move_from_row

_MOVE = "src.server.services.computer_manager._move"
MOVE_ID = "66666666-6666-4666-8666-666666666666"
SOURCE = "77777777-7777-4777-8777-777777777777"
ROOT = "/data/root"
AWAY = f"_internal/leftovers/move-{MOVE_ID}"


def _staged(age_s=60, **overrides):
    """Staged ``age_s`` ago, and claimed long before: a long backup sits between."""
    staged_at = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    row = {
        "move_id": MOVE_ID,
        "runner_id": "runner-1",
        "user_id": "user-1",
        "workspace_id": "ws-1",
        "source_computer_id": SOURCE,
        "source_folder": "Research",
        "leftovers_path": AWAY,
        "phase": "staged",
        "created_at": staged_at - timedelta(seconds=2 * _PUT_BACK_GIVE_UP_S),
        "staged_at": staged_at,
    }
    row.update(overrides)
    return row


def _computer(status="running"):
    return {
        "computer_id": SOURCE,
        "user_id": "user-1",
        "kind": "docker",
        "status": status,
        "provider_ref": "sb-old",
        "root_dir": ROOT,
    }


def _manager():
    return ComputerManager(SimpleNamespace(sandbox=SimpleNamespace(provider="docker")))


@pytest.fixture
def rollback():
    """A manager whose put-back answers as the test sets it, and the marks it makes."""
    manager = _manager()
    state = SimpleNamespace(outcome="back")

    async def put_back(row):
        return state.outcome

    manager._put_folder_back = put_back
    with (
        patch(f"{_MOVE}.finish_workspace_move", AsyncMock()) as finish,
        patch(f"{_MOVE}.flag_rollback_restore", AsyncMock()) as flag,
    ):
        yield SimpleNamespace(manager=manager, state=state, finish=finish, flag=flag)


@pytest.mark.asyncio
async def test_a_folder_put_back_ends_the_move_with_nothing_left(rollback):
    await rollback.manager._roll_back_move(_staged(), {"code": "busy"})

    rollback.flag.assert_not_awaited()
    rollback.finish.assert_awaited_once_with(
        MOVE_ID, "runner-1", "failed", error={"code": "busy"}, leftovers_path=None
    )


@pytest.mark.asyncio
async def test_a_put_back_that_can_succeed_later_leaves_the_move_staged(rollback):
    rollback.state.outcome = "later"

    await rollback.manager._roll_back_move(_staged(), {"code": "busy"})

    rollback.flag.assert_not_awaited()
    rollback.finish.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "age_s"),
    [("never", 60), ("later", _PUT_BACK_GIVE_UP_S + 60)],
    ids=["cannot", "gave-up"],
)
async def test_the_mirror_restores_only_a_folder_that_cannot_come_back(
    rollback, outcome, age_s
):
    rollback.state.outcome = outcome

    await rollback.manager._roll_back_move(_staged(age_s), {"code": "busy"})

    rollback.flag.assert_awaited_once_with(MOVE_ID, "ws-1")
    rollback.finish.assert_awaited_once_with(
        MOVE_ID, "runner-1", "failed", error={"code": "busy"}
    )


@pytest.fixture
def put_back():
    """A manager whose source sandbox runs the put-back script with a set exit code."""
    manager = _manager()
    state = SimpleNamespace(exit_code=0, computers=[_computer()])
    runtime = MagicMock()
    runtime.exec = AsyncMock(
        side_effect=lambda command: SimpleNamespace(exit_code=state.exit_code)
    )

    @asynccontextmanager
    async def detached(provider_ref, *, binding=None):
        yield runtime

    @asynccontextmanager
    async def folders_lock(computer_id):
        yield object()

    manager._detached_runtime = detached
    manager.wake_computer = AsyncMock()
    with (
        patch(f"{_MOVE}.get_computer", AsyncMock(side_effect=lambda _: state.computers.pop(0))),
        patch(f"{_MOVE}.workspace_folders_lock", folders_lock),
        patch(f"{_MOVE}.hold_workspace_folder", AsyncMock(return_value=True)),
    ):
        yield SimpleNamespace(manager=manager, state=state, runtime=runtime)


@pytest.mark.asyncio
async def test_a_stopped_source_is_started_to_take_its_folder_back(put_back):
    put_back.state.computers = [_computer("stopped"), _computer()]

    assert await put_back.manager._put_folder_back(_staged()) == "back"

    put_back.manager.wake_computer.assert_awaited_once_with(SOURCE, user_id="user-1")
    (command,) = put_back.runtime.exec.await_args.args
    assert f"mv {ROOT}/{AWAY} {ROOT}/Research" in command


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("computers", "exit_code", "outcome"),
    [
        ([_computer("deleted")], 0, "never"),
        ([_computer()], 3, "never"),
        ([_computer()], 1, "later"),
        ([_computer("stopped"), _computer("stopped")], 0, "later"),
    ],
    ids=["computer-gone", "name-taken", "exec-failed", "did-not-start"],
)
async def test_a_put_back_says_whether_trying_again_can_help(
    put_back, computers, exit_code, outcome
):
    put_back.state.computers = computers
    put_back.state.exit_code = exit_code

    assert await put_back.manager._put_folder_back(_staged()) == outcome


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("aside", "under_name", "outcome"),
    [(True, False, "back"), (False, True, "back"), (True, True, "never"), (False, False, "never")],
    ids=["set-aside", "never-left", "name-taken", "gone-from-both"],
)
async def test_a_put_back_reads_where_the_folder_is(
    put_back, tmp_path, aside, under_name, outcome
):
    """The script itself, run by sh: the row records the leftovers before the
    folder leaves, so a folder still under its name never left, and only a
    ``never`` restores the mirror over it."""
    put_back.state.computers = [{**_computer(), "root_dir": str(tmp_path)}]
    put_back.runtime.exec.side_effect = lambda command: SimpleNamespace(
        exit_code=subprocess.run(["sh", "-c", command]).returncode
    )
    if aside:
        (tmp_path / AWAY).mkdir(parents=True)
        (tmp_path / AWAY / "notes.txt").write_text("aside")
    if under_name:
        (tmp_path / "Research").mkdir()

    assert await put_back.manager._put_folder_back(_staged()) == outcome
    if aside and not under_name:
        assert (tmp_path / "Research" / "notes.txt").read_text() == "aside"


@pytest.mark.asyncio
async def test_a_runner_that_loses_its_row_stops_its_backup_at_once():
    """Held under the source's decision lock, a backup left running would keep
    a new move or a spec change on that computer waiting until it ended."""
    manager = _manager()
    started = asyncio.Event()

    async def backup(*args, **kwargs):
        started.set()
        await asyncio.sleep(3600)

    manager.backup_project_files = backup
    manager._set_folder_aside = AsyncMock()
    lost = asyncio.Event()
    move = _staged(phase="preparing")
    with (
        patch(f"{_MOVE}.advance_workspace_move", AsyncMock(return_value={"phase": "backing_up"})),
        patch(f"{_MOVE}.get_computer", AsyncMock(return_value=_computer())),
    ):
        task = asyncio.create_task(
            manager._back_up_and_set_aside(move, SimpleNamespace(sandbox=MagicMock()), lost)
        )
        await started.wait()
        lost.set()
        assert await asyncio.wait_for(task, timeout=1) is None
    manager._set_folder_aside.assert_not_awaited()


@pytest.fixture
def folder_hold():
    """The folder lock always free, and the folder held as the test sets it."""
    state = SimpleNamespace(holds=[], tries=0)

    @asynccontextmanager
    async def folders_lock(computer_id):
        yield object()

    async def hold(conn, workspace_id):
        state.tries += 1
        return state.holds.pop(0) if state.holds else False

    with (
        patch(f"{_MOVE}.workspace_folders_lock", folders_lock),
        patch(f"{_MOVE}.hold_workspace_folder", hold),
        patch(f"{_MOVE}._FOLDER_HOLD_RETRY_S", 0),
    ):
        yield state


@pytest.mark.asyncio
async def test_setting_aside_waits_out_the_backup_after_a_turn(folder_hold):
    """A turn's backup holds the folder as it ends; a move right after it waits."""
    folder_hold.holds = [False, False, True]

    async with _folder_held(SOURCE, "ws-1") as conn:
        assert conn is not None
    assert folder_hold.tries == 3


@pytest.mark.asyncio
async def test_a_folder_held_past_the_wait_fails_the_move_busy(folder_hold):
    with patch(f"{_MOVE}._FOLDER_HOLD_WAIT_S", 0):
        with pytest.raises(ComputerBusyError, match="a file change holds the folder"):
            async with _folder_held(SOURCE, "ws-1"):
                pass


def test_an_incomplete_backup_keeps_its_own_sentence_and_files():
    error = {
        "code": "backup_incomplete",
        "message": "2 files couldn't be saved, so nothing was moved.",
        "files": [{"path": "a.bin", "reason": "unreadable", "size": 12}],
    }
    row = {
        **_staged(),
        "target_computer_id": "88888888-8888-4888-8888-888888888888",
        "make_primary": False,
        "phase": "failed",
        "error": error,
        "stale": False,
    }

    assert move_from_row(row).error.model_dump() == error
