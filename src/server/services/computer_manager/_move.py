"""Seam: moving a workspace to another of its owner's computers.

One file of the ComputerManager split; see the package __init__.

A move is claimed on a ``workspace_moves`` row, which keeps turns and every
session away from the workspace until the move ends. The runner backs the
folder up from the computer it is on, sets it aside there, and binds the
workspace to the target in one transaction. The target lays the folder again
from the mirror the first time the workspace is opened there, as it does after
a rebuild. The folder set aside stays on the old computer until the files are
back in place and ``LEFTOVERS_KEEP_DAYS`` have passed, because the mirror
leaves out dependency and version-control folders.
"""

import asyncio
import logging
import shlex
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Dict, Literal, Mapping, Optional

from psycopg.errors import DeadlockDetected, SerializationFailure

from ptc_agent.core.paths import workspace_root

from src.server.database.computer import (
    DEFAULT_ROOT_DIR,
    get_computer,
    update_computer_activity,
)
from src.server.database.sql_fences import MOVE_EARLY_PHASES, MOVE_HEARTBEAT_SECONDS
from src.server.database.workspace import get_workspace_dir_name
from src.server.database.workspace_folders import (
    LEFTOVERS_DIR,
    hold_workspace_folder,
    is_top_level,
    workspace_folders_lock,
)
from src.server.database.workspace_move import (
    advance_workspace_move,
    commit_workspace_move,
    fail_stale_early_moves,
    finish_workspace_move,
    flag_rollback_restore,
    get_workspace_move,
    heartbeat_workspace_move,
    list_moves_with_leftovers,
    mark_leftovers_removed,
    mark_move_restored,
    stage_workspace_move,
    take_over_stale_moves,
)
from src.server.models.computer import ComputerStatus
from src.server.services.computer_errors import ComputerBusyError
from src.server.services.computer_manager._folders import _run_folder_script
from src.server.services.workspace_move import (
    MoveSourceUnavailable,
    move_error,
    move_error_detail,
)
from src.server.services.workspace_status_pubsub import (
    publish_workspace_binding_change,
)

logger = logging.getLogger(__name__)

# A commit locks computer rows before the workspace, as a machine write does;
# one Postgres still ends is safe to run again whole, as nothing outlives it.
_COMMIT_ATTEMPTS = 3
# Removing a node_modules tree runs longer than the default exec allows.
_LEFTOVERS_RM_TIMEOUT_S = 300
# How long a failed move keeps trying to put its folder back before the
# workspace is restored from the mirror, which lacks .git and dependencies.
# Counted from staging, since the backup before it can outlast the window.
_PUT_BACK_GIVE_UP_S = 30 * 60
# How long setting the folder aside waits for a short holder to let it go,
# such as the backup that runs after every turn, before the move fails busy.
_FOLDER_HOLD_WAIT_S = 30
_FOLDER_HOLD_RETRY_S = 0.5


@asynccontextmanager
async def _folder_held(computer_id: str, workspace_id: str) -> AsyncIterator[Any]:
    """The computer's folder lock with this workspace's folder held.

    Both are taken only when free, as a settle takes them, but a move does not
    come back later as a settle does: it would fail a user who moved right
    after a turn, while that turn's backup still reads the folder. So it waits
    a while, letting the folder lock go between tries so a settle can run.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _FOLDER_HOLD_WAIT_S
    while True:
        async with workspace_folders_lock(computer_id) as conn:
            if conn is None:
                busy = "the folder lock is busy"
            elif await hold_workspace_folder(conn, workspace_id):
                yield conn
                return
            else:
                busy = "a file change holds the folder"
        if loop.time() >= deadline:
            raise ComputerBusyError(busy)
        await asyncio.sleep(_FOLDER_HOLD_RETRY_S)


def _leftovers_folder(move_id: str) -> str:
    return f"{LEFTOVERS_DIR}/move-{move_id}"


def _running_ref(computer: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The sandbox to reach, only while the computer is running one."""
    if computer is None or computer.get("status") != ComputerStatus.RUNNING:
        return None
    return str(computer["provider_ref"]) if computer.get("provider_ref") else None


class WorkspaceMoveMixin:
    """Workspace moves for ComputerManager."""

    move_heartbeat_s: float = MOVE_HEARTBEAT_SECONDS

    @asynccontextmanager
    async def _move_heartbeat(
        self, move_id: str, runner_id: str, source_id: str
    ) -> AsyncIterator[asyncio.Event]:
        """Keep the move's row fresh and its source computer awake while it runs.

        The idle sweep reads the computer's activity stamp, and a stop between
        the backup and the commit would leave nothing to put the folder back
        on. Stops once the row no longer answers to this runner, and sets the
        event it yields.
        """
        lost = asyncio.Event()

        async def renew() -> None:
            while True:
                try:
                    await update_computer_activity(source_id)
                except Exception as e:
                    logger.warning("Could not keep computer %s awake: %s", source_id, e)
                await asyncio.sleep(self.move_heartbeat_s)
                try:
                    if not await heartbeat_workspace_move(move_id, runner_id):
                        logger.warning("Move %s is no longer this runner's", move_id)
                        lost.set()
                        return
                except Exception as e:
                    logger.warning("Could not renew move %s: %s", move_id, e)

        task = asyncio.create_task(renew())
        try:
            yield lost
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    async def _unless_lost(work: Awaitable[Any], lost: asyncio.Event) -> bool:
        """Await ``work``; False, with it cancelled, once the move stops being ours.

        A cancelled move learns of it at its next write to the row, and a
        backup can run for minutes holding the source's decision lock, which a
        new move or a spec change there would wait on.
        """
        task = asyncio.ensure_future(work)
        waiter = asyncio.ensure_future(lost.wait())
        try:
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pending in (task, waiter):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, waiter, return_exceptions=True)
        if task.cancelled():
            return False
        task.result()
        return True

    async def run_accepted_move(self, move: Mapping[str, Any]) -> None:
        """Run a move accepted behind a 202; the row is its only report."""
        try:
            await self._run_move(move)
        except Exception:
            logger.exception(
                "Move %s of workspace %s failed", move["move_id"], move["workspace_id"]
            )

    async def _run_move(self, move: Mapping[str, Any]) -> None:
        """Back up, set aside and commit under the source's decision lock.

        The lock keeps a spec change or a retirement from rebuilding the source
        mid-move, which would lay the folder again from the mirror. Whatever
        stops the run, cancellation included, ends the row before it returns.
        """
        move_id = str(move["move_id"])
        runner = str(move["runner_id"])
        source = str(move["source_computer_id"])
        async with self._move_heartbeat(move_id, runner, source) as lost:
            try:
                session = await self._wake_move_source(source, str(move["user_id"]))
                async with self._machine_decision_lock(source) as acquired:
                    if not acquired:
                        raise ComputerBusyError("the source computer is busy")
                    staged = await self._back_up_and_set_aside(move, session, lost)
                    committed = await self._commit_move(staged) if staged else None
            except BaseException as exc:
                await asyncio.shield(self._abort_move(move_id, runner, exc))
                raise
            if committed is not None:
                await self._finish_committed_move(committed)

    async def _wake_move_source(self, source: str, user_id: str) -> Any:
        try:
            session = await self.wake_computer(source, user_id=user_id)
        except Exception as e:
            raise MoveSourceUnavailable(f"computer {source} did not start: {e}") from e
        if getattr(session, "sandbox", None) is None:
            raise MoveSourceUnavailable(f"computer {source} has no sandbox attached")
        return session

    async def _back_up_and_set_aside(
        self, move: Mapping[str, Any], session: Any, lost: asyncio.Event
    ) -> Optional[Dict[str, Any]]:
        """The staged row once the folder is backed up and out of its name; None if not ours."""
        move_id = str(move["move_id"])
        runner = str(move["runner_id"])
        workspace_id = str(move["workspace_id"])
        source = str(move["source_computer_id"])
        if await advance_workspace_move(move_id, runner, "backing_up") is None:
            return None
        computer = await get_computer(source)
        provider_ref = _running_ref(computer)
        if provider_ref is None:
            raise MoveSourceUnavailable(f"computer {source} is not running")
        backup = self.backup_project_files(
            workspace_id,
            computer_id=source,
            strict=True,
            expected_sandbox_id=provider_ref,
            session=session,
        )
        if not await self._unless_lost(backup, lost):
            return None
        return await self._set_folder_aside(
            move,
            session.sandbox.runtime,
            computer.get("root_dir") or DEFAULT_ROOT_DIR,
        )

    async def _set_folder_aside(
        self, move: Mapping[str, Any], runtime: Any, root: str
    ) -> Optional[Dict[str, Any]]:
        """Move the folder under ``_internal/leftovers`` on the source.

        Under the computer's folder lock and the workspace's folder hold, as a
        settle moves a folder, so no file change lands in between. The row is
        staged before the script runs: a runner that dies mid-script leaves a
        row the sweep rolls back, and a cancel can no longer reach a move whose
        folder may be gone.
        """
        move_id = str(move["move_id"])
        runner = str(move["runner_id"])
        workspace_id = str(move["workspace_id"])
        away = _leftovers_folder(move_id)
        tombstone = f"move-{move_id}"
        async with _folder_held(str(move["source_computer_id"]), workspace_id) as conn:
            folder = await get_workspace_dir_name(workspace_id, conn=conn)
            if not folder or not is_top_level(folder):
                raise ComputerBusyError(f"the folder is mid-move ({folder!r})")
            staged = await stage_workspace_move(
                move_id, runner, source_folder=folder, leftovers_path=away
            )
            if staged is None:
                return None
            report = await _run_folder_script(
                runtime,
                root,
                moves=[],
                tombstones=[
                    {"id": tombstone, "dir": folder, "source": folder, "staged": False}
                ],
                prune=False,
            )
            moved = report.get("tombstones") or {}
            if not isinstance(moved, dict) or tombstone not in moved:
                raise RuntimeError(
                    "; ".join(map(str, report.get("errors") or ["no report"]))
                )
            if moved[tombstone] is None:
                # Never laid on this computer: nothing is left behind.
                return await advance_workspace_move(
                    move_id, runner, "staged", leftovers_path=None
                )
            if moved[tombstone] != away:
                raise RuntimeError(f"folder script reported {moved[tombstone]!r}")
        return staged

    async def _commit_move(self, staged: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        move_id = str(staged["move_id"])
        for attempt in range(1, _COMMIT_ATTEMPTS + 1):
            try:
                return await commit_workspace_move(move_id, str(staged["runner_id"]))
            except (DeadlockDetected, SerializationFailure) as e:
                if attempt == _COMMIT_ATTEMPTS:
                    raise
                logger.info("Commit of move %s retried after %s", move_id, e)
        return None

    async def _abort_move(self, move_id: str, runner: str, exc: BaseException) -> None:
        """End a run that stopped short, from the phase the row actually reached."""
        try:
            row = await get_workspace_move(move_id)
            if row is None or str(row["runner_id"]) != runner:
                return
            if row["phase"] in MOVE_EARLY_PHASES:
                await finish_workspace_move(
                    move_id, runner, "failed", error=move_error(exc)
                )
            elif row["phase"] == "staged":
                await self._roll_back_move(row, move_error(exc))
            elif row["phase"] == "committed":
                # The commit landed and only its answer was lost.
                await self._finish_committed_move(row)
                return
            logger.warning("Move %s ended at %s: %r", move_id, row["phase"], exc)
        except Exception:
            logger.exception("Could not end move %s; the sweep recovers it", move_id)

    async def _roll_back_move(
        self, row: Mapping[str, Any], error: Dict[str, Any]
    ) -> None:
        """Put a staged move's folder back, or have the workspace restored where it is.

        A put-back that failed for now leaves the row staged, still fencing
        the workspace, for the sweep to try again once the runner's heartbeat
        lapses: the mirror lacks the folder's .git and dependencies, which
        then survive only in leftovers that are removed in days.
        """
        move_id = str(row["move_id"])
        outcome = await self._put_folder_back(row)
        if outcome == "back":
            await finish_workspace_move(
                move_id, str(row["runner_id"]), "failed", error=error, leftovers_path=None
            )
            return
        age = (datetime.now(timezone.utc) - row["staged_at"]).total_seconds()
        if outcome == "later" and age < _PUT_BACK_GIVE_UP_S:
            logger.warning("Move %s left staged to put its folder back later", move_id)
            return
        # Ahead of the finish, which lifts the fence: an attach between the two
        # would take the empty folder as current.
        await flag_rollback_restore(move_id, str(row["workspace_id"]))
        await finish_workspace_move(move_id, str(row["runner_id"]), "failed", error=error)

    async def _put_folder_back(
        self, row: Mapping[str, Any]
    ) -> Literal["back", "later", "never"]:
        """Put the folder under its name again, starting its computer if stopped.

        ``back`` when it is there, or never left; ``never`` when it cannot be,
        with the computer gone, a folder laid under the name since, or the
        folder in neither place. The row records the leftovers before the
        script that makes them runs, so a folder still under its name with no
        leftovers never left.
        """
        away = row.get("leftovers_path")
        folder = row.get("source_folder")
        if not away:
            return "back"
        if not folder or not is_top_level(folder):
            return "never"
        source = str(row["source_computer_id"])
        computer = await get_computer(source)
        if computer is None or computer.get("status") == ComputerStatus.DELETED:
            return "never"
        if _running_ref(computer) is None:
            try:
                await self.wake_computer(source, user_id=str(row["user_id"]))
            except Exception as e:
                logger.warning("Could not start computer %s to put a folder back: %s", source, e)
                return "later"
            computer = await get_computer(source)
        provider_ref = _running_ref(computer)
        if provider_ref is None:
            return "later"
        root = computer.get("root_dir") or DEFAULT_ROOT_DIR
        src = shlex.quote(workspace_root(root, away))
        dst = shlex.quote(workspace_root(root, folder))
        try:
            async with self._detached_runtime(
                provider_ref, binding=self._binding_from_computer("", computer)
            ) as runtime, workspace_folders_lock(source) as conn:
                if conn is None or not await hold_workspace_folder(
                    conn, str(row["workspace_id"])
                ):
                    return "later"
                # Never over a folder something laid under the name since.
                result = await runtime.exec(
                    f"if [ -e {src} ]; then if [ -e {dst} ]; then exit 3; fi; "
                    f"mv {src} {dst}; elif [ ! -e {dst} ]; then exit 3; fi"
                )
        except Exception as e:
            logger.warning("Could not put back the folder of move %s: %s", row["move_id"], e)
            return "later"
        if result.exit_code == 0:
            return "back"
        return "never" if result.exit_code == 3 else "later"

    async def _finish_committed_move(self, row: Mapping[str, Any]) -> None:
        """Converge the computers on a committed move; every step is safe to repeat."""
        move_id = str(row["move_id"])
        workspace_id = str(row["workspace_id"])
        target = await get_computer(str(row["target_computer_id"]))
        if target is not None:
            try:
                await publish_workspace_binding_change(
                    workspace_id, str(target["status"]), str(target["computer_id"])
                )
            except Exception as e:
                logger.warning("Could not publish the move of %s: %s", workspace_id, e)
        source = await get_computer(str(row["source_computer_id"]))
        if source is not None:
            await self._refresh_source_grants(source, str(row["user_id"]))
            from src.server.services.preview_capabilities import revoke

            # Revocation is per sandbox, so a sibling's preview mints a fresh
            # link on its next open, as after a workspace delete.
            await revoke(source.get("provider_ref"))
        self._forget_project(workspace_id)
        await finish_workspace_move(move_id, str(row["runner_id"]), "done")
        logger.info("Move %s of workspace %s done", move_id, workspace_id)

    async def _refresh_source_grants(self, computer: Mapping[str, Any], user_id: str) -> None:
        """Drop the moved workspace's egress grants from the source's map now.

        Best effort: the database already denies a retired grant, and a stopped
        computer reads its map afresh when it starts.
        """
        provider_ref = _running_ref(computer)
        if provider_ref is None:
            return
        from src.server.services.egress.session_binding import refresh_computer_grant_map

        try:
            async with self._detached_runtime(
                provider_ref, binding=self._binding_from_computer("", computer)
            ) as runtime:
                await refresh_computer_grant_map(
                    runtime,
                    root=computer.get("root_dir") or DEFAULT_ROOT_DIR,
                    computer_id=str(computer["computer_id"]),
                    user_id=user_id,
                )
        except Exception:
            logger.warning(
                "Could not refresh the egress map of computer %s after a move",
                computer["computer_id"],
                exc_info=True,
            )

    async def reap_workspace_moves(self) -> None:
        """Recover moves whose runner died, and clear old computers of what moves left.

        Every worker runs this; a takeover hands each stale move to one of them.
        """
        try:
            await fail_stale_early_moves()
            for row in await take_over_stale_moves():
                try:
                    if row["phase"] == "committed":
                        await self._finish_committed_move(row)
                    else:
                        await self._roll_back_move(row, move_error_detail("interrupted"))
                except Exception:
                    logger.exception("Could not recover move %s", row["move_id"])
            for row in await list_moves_with_leftovers():
                try:
                    await self._reap_move_leftovers(row)
                except Exception as e:
                    logger.warning(
                        "Could not clear the leftovers of move %s: %s", row["move_id"], e
                    )
        except Exception:
            logger.exception("Workspace move sweep failed")

    async def _reap_move_leftovers(self, row: Mapping[str, Any]) -> None:
        """Act on a folder a move left, as the listing decided.

        Only on a computer already running: none is started for this.
        """
        move_id = str(row["move_id"])
        if row["action"] == "source_gone":
            # Deleted with its sandbox, and the folder with it.
            await mark_leftovers_removed(move_id)
            return
        if row["action"] == "restored":
            await mark_move_restored(move_id)
            return
        away = row["leftovers_path"]
        if not away.startswith(f"{LEFTOVERS_DIR}/move-"):
            logger.warning("Move %s records leftovers at %r; left alone", move_id, away)
            return
        source = str(row["source_computer_id"])
        computer = await get_computer(source)
        provider_ref = _running_ref(computer)
        if provider_ref is None:
            return
        root = computer.get("root_dir") or DEFAULT_ROOT_DIR
        # Not waited for: another worker holding it is likely removing these.
        async with self._detached_runtime(
            provider_ref, binding=self._binding_from_computer("", computer)
        ) as runtime, workspace_folders_lock(source, wait_s=0) as conn:
            if conn is None:
                return
            result = await runtime.exec(
                f"rm -rf {shlex.quote(workspace_root(root, away))}",
                timeout=_LEFTOVERS_RM_TIMEOUT_S,
            )
        if result.exit_code != 0:
            raise RuntimeError(f"rm exited {result.exit_code}: {(result.stderr or '')[:200]}")
        await mark_leftovers_removed(move_id)
        logger.info("Removed %s left on computer %s by move %s", away, source, move_id)
