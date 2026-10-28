"""A bring-up step only works a workspace that still lives on its computer.

A restart queues every workspace on the machine and works through them one at
a time, so one can move to another computer — or be deleted — while it waits,
between the step's checks, or between its restore and its sync. Ownership is
read from the database at two posts of the same judgment: a fast path when a
step is picked up, and the one that matters, inside the folder hold that
fences the step's writes. A workspace that fails either is dropped, not
failed: its next acquisition on whichever computer holds it now queues the
work where it belongs. The doubles here record every write the checks stand
in front of, so a step that should have been dropped shows up as a call.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest

from ptc_agent.core.paths import SandboxLayout
from src.server.database.conversation import ThreadPrefixes
from src.server.services.computer_manager import _bringup
from src.server.services.computer_manager._bringup import BringUp

pytestmark = pytest.mark.asyncio

ROOT = "/home/workspace"
COMPUTER = "computer-1"
OTHER = "computer-2"

_PREFIXES = ThreadPrefixes(all=frozenset({"live-A"}), open=frozenset({"live-A"}))
_RESTORED = {"restored": 0, "errors": 0, "skipped": 0, "done": True}
_OURS = {"workspace_id": "A", "computer_id": COMPUTER}
_MOVED = {"workspace_id": "A", "computer_id": OTHER}


def _sandbox() -> SimpleNamespace:
    async def adownload_file_bytes(path):
        return None

    return SimpleNamespace(
        runtime=object(),
        sandbox_id="sandbox-1",
        working_dir=ROOT,
        adownload_file_bytes=adownload_file_bytes,
    )


def _job(sandbox: SimpleNamespace) -> tuple[BringUp, list[str]]:
    """A job for ``COMPUTER`` with one workspace queued, and what it forgot."""
    forgotten: list[str] = []
    job = BringUp(COMPUTER, sandbox, "sandbox-1", ROOT, forgotten.append)
    job.add("A", urgent=False)
    return job, forgotten


@asynccontextmanager
async def _held_layout(workspace_id, root):
    # The hold's shared-lock session is another test's subject; this keeps
    # only the shape an ``async with`` needs, which an autospec cannot give
    # an async-context-manager function.
    yield SandboxLayout.for_root(root).for_workspace(workspace_id)


class _Writes:
    """Every write and probe a dropped step must not make."""

    def __init__(self) -> None:
        self.inventory = create_autospec(_bringup._inventory, return_value=(None, None))
        self.prefixes = create_autospec(
            _bringup._thread_prefixes, return_value=_PREFIXES
        )
        self.prune = create_autospec(
            _bringup._thread_dirs.prune_dead_thread_dirs,
            return_value=(_PREFIXES, True),
        )
        self.restore = create_autospec(
            _bringup.FilePersistenceService.restore_deferred,
            return_value=_RESTORED,
        )
        self.sync = create_autospec(
            _bringup.transcripts.sync_workspace, return_value={}
        )

    def patches(self, placement) -> list:
        return [
            patch.object(_bringup, "get_workspace_placement", placement),
            patch.object(_bringup, "held_workspace_layout", _held_layout),
            patch.object(_bringup, "_inventory", self.inventory),
            patch.object(_bringup, "_thread_prefixes", self.prefixes),
            patch.object(_bringup._thread_dirs, "prune_dead_thread_dirs", self.prune),
            patch.object(
                _bringup.FilePersistenceService, "restore_deferred", self.restore
            ),
            patch.object(_bringup.transcripts, "sync_workspace", self.sync),
            patch.object(_bringup.cache, "client", lambda: None),
        ]


async def _run(job: BringUp) -> None:
    job.task = asyncio.create_task(job.run())
    await job.task


async def test_a_workspace_still_on_its_computer_runs_its_pass():
    """The checks are a guard, not a gate: nothing about the pass changes."""
    writes = _Writes()
    placement = create_autospec(_bringup.get_workspace_placement, return_value=_OURS)
    job, forgotten = _job(_sandbox())
    with contextlib.ExitStack() as stack:
        for cm in writes.patches(placement):
            stack.enter_context(cm)
        await _run(job)

    assert writes.restore.await_count == 1
    assert writes.prune.await_count == 1
    assert writes.sync.await_count == 1
    assert job.pending == {}
    assert forgotten == []


async def test_a_workspace_moved_to_another_computer_is_dropped(caplog):
    """No restore, prune or sync on the machine the workspace left; the step
    goes without a retry and without its attach key."""
    writes = _Writes()
    placement = create_autospec(_bringup.get_workspace_placement, return_value=_MOVED)
    job, forgotten = _job(_sandbox())
    with contextlib.ExitStack() as stack:
        for cm in writes.patches(placement):
            stack.enter_context(cm)
        with caplog.at_level("INFO", logger=_bringup.__name__):
            await _run(job)

    assert writes.restore.await_count == 0
    assert writes.prune.await_count == 0
    assert writes.inventory.await_count == 0
    assert writes.sync.await_count == 0
    assert job.pending == {}
    assert forgotten == ["A"]
    dropped = [r for r in caplog.records if "no longer lives" in r.message]
    assert dropped, "the drop is logged"
    message = dropped[0].getMessage()
    assert "A" in message and COMPUTER in message and OTHER in message


async def test_a_deleted_workspace_is_dropped():
    """The placement read filters deleted rows, so None means just that."""
    writes = _Writes()
    placement = create_autospec(_bringup.get_workspace_placement, return_value=None)
    job, forgotten = _job(_sandbox())
    with contextlib.ExitStack() as stack:
        for cm in writes.patches(placement):
            stack.enter_context(cm)
        await _run(job)

    assert writes.restore.await_count == 0
    assert writes.prune.await_count == 0
    assert writes.inventory.await_count == 0
    assert writes.sync.await_count == 0
    assert job.pending == {}
    assert forgotten == ["A"]


async def test_a_placement_read_that_fails_writes_nothing():
    """Not knowing where the workspace lives is the drop condition too."""
    writes = _Writes()
    placement = create_autospec(
        _bringup.get_workspace_placement, side_effect=RuntimeError("pool closed")
    )
    job, forgotten = _job(_sandbox())
    with contextlib.ExitStack() as stack:
        for cm in writes.patches(placement):
            stack.enter_context(cm)
        await _run(job)

    assert writes.restore.await_count == 0
    assert writes.prune.await_count == 0
    assert writes.inventory.await_count == 0
    assert writes.sync.await_count == 0
    assert job.pending == {}
    assert forgotten == ["A"]


async def test_a_move_landing_between_the_fast_path_and_the_restore_writes_nothing():
    """The fast path passes, the move commits while the step waits on the
    stamp and the threads, and the check inside the restore's folder hold is
    the one that stops it: no probe, prune or restore runs."""
    writes = _Writes()
    placement = create_autospec(
        _bringup.get_workspace_placement, side_effect=[_OURS, _MOVED]
    )
    job, forgotten = _job(_sandbox())
    with contextlib.ExitStack() as stack:
        for cm in writes.patches(placement):
            stack.enter_context(cm)
        await _run(job)

    assert placement.await_count == 2
    assert writes.inventory.await_count == 0
    assert writes.prune.await_count == 0
    assert writes.restore.await_count == 0
    assert writes.sync.await_count == 0
    assert job.pending == {}
    assert forgotten == ["A"]


async def test_a_move_landing_between_the_restore_and_the_sync_skips_the_sync():
    """The restore runs while the workspace is still here; the move commits
    after the sync's fast path read it as ours, and the check inside the
    sync's folder hold stops the export."""
    writes = _Writes()
    placement = create_autospec(
        _bringup.get_workspace_placement, side_effect=[_OURS, _OURS, _OURS, _MOVED]
    )
    job, forgotten = _job(_sandbox())
    with contextlib.ExitStack() as stack:
        for cm in writes.patches(placement):
            stack.enter_context(cm)
        await _run(job)

    assert placement.await_count == 4
    assert writes.restore.await_count == 1
    assert writes.prune.await_count == 1
    assert writes.sync.await_count == 0
    assert job.pending == {}
    assert forgotten == ["A"]
