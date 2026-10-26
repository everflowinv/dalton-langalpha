"""A minted market-data token reaches the sandbox only through the row's
fence and generation, and the sweep writes each computer from one worker."""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ptc_agent.core.sandbox._shared import REPLACE_UNLESS_NEWER_SCRIPT
from src.server.database import computer_data_tokens as db
from src.server.services import data_token_sweep, data_tokens
from src.server.services.data_token_sweep import DataTokenSweeper
from src.server.services.workspace_manager import WorkspaceManager

COMPUTER_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "user-1"
SANDBOX_ID = "sandbox-1"
EXPIRES_AT = datetime(2026, 10, 18, 13, 0, 0, 123456, tzinfo=UTC)


ISSUED_AT_MS = 1_792_310_400_123


async def _push(saved: db.Saved, *, written: bool = True):
    write = AsyncMock(return_value=written)
    revoke = AsyncMock(return_value=True)
    save = AsyncMock(return_value=saved)
    mark_held = AsyncMock(return_value=True)
    minted = data_tokens.Minted("tok", ISSUED_AT_MS, EXPIRES_AT)
    with (
        patch.object(data_tokens, "_mint", AsyncMock(return_value=minted)),
        patch.object(data_tokens, "_revoke_upstream", revoke),
        patch.object(db, "save", save),
        patch.object(db, "mark_held", mark_held),
    ):
        pushed = await data_tokens.push(
            COMPUTER_ID, USER_ID, SANDBOX_ID, write, generation=4
        )
    return pushed, write, revoke, mark_held, save


class TestPush:
    @pytest.mark.asyncio
    async def test_a_token_the_fence_refuses_is_dropped_without_revoking(self):
        """Revoking is per computer, so it would end the token the sandbox
        holds now, which the refused one never replaced."""
        pushed, write, revoke, mark_held, _ = await _push(db.Saved(False, None))

        assert pushed is False
        revoke.assert_not_awaited()
        write.assert_not_awaited()
        mark_held.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_save_is_fenced_on_the_sandbox_it_was_minted_for(self):
        _, _, _, _, save = await _push(db.Saved(True, 5))

        save.assert_awaited_once_with(COMPUTER_ID, USER_ID, SANDBOX_ID, EXPIRES_AT, 4)

    @pytest.mark.asyncio
    async def test_a_moved_generation_drops_the_token_without_revoking(self):
        pushed, write, revoke, mark_held, _ = await _push(db.Saved(True, None))

        assert pushed is False
        revoke.assert_not_awaited()
        write.assert_not_awaited()
        mark_held.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_saved_token_is_written_then_held(self):
        pushed, write, revoke, mark_held, _ = await _push(db.Saved(True, 5))

        assert pushed is True
        revoke.assert_not_awaited()
        payload = write.await_args.args[0]
        assert set(payload) == {"access_token", "iat_ms", "expires_at", "ginlix_data_url"}
        assert payload["access_token"] == "tok"
        assert payload["iat_ms"] == ISSUED_AT_MS
        assert payload["expires_at"] == "2026-10-18T13:00:00Z"
        mark_held.assert_awaited_once_with(COMPUTER_ID, 5, SANDBOX_ID)

    @pytest.mark.asyncio
    async def test_an_unconfirmed_write_is_not_held(self):
        pushed, _, _, mark_held, _ = await _push(db.Saved(True, 5), written=False)

        assert pushed is False
        mark_held.assert_not_awaited()


class TestReplaceUnlessNewer:
    """The script the sandbox runs to swap the token file in."""

    def _run(self, tmp_path, held: dict | None, issued: int):
        src, dst = tmp_path / "incoming", tmp_path / "tokens.json"
        src.write_text(json.dumps({"access_token": "new", "iat_ms": issued}))
        if held is not None:
            dst.write_text(json.dumps(held))
        done = subprocess.run(
            [sys.executable, "-c", REPLACE_UNLESS_NEWER_SCRIPT, str(src), str(dst), str(issued)],
            capture_output=True,
        )
        return done.returncode, src, json.loads(dst.read_text())

    def test_a_later_issue_replaces_even_with_an_earlier_expiry(self, tmp_path):
        held = {"access_token": "old", "iat_ms": 100, "expires_at": "2026-10-19T00:00:00Z"}

        code, src, now = self._run(tmp_path, held, issued=200)

        assert code == 0
        assert now["access_token"] == "new"
        assert not src.exists()

    def test_an_earlier_issue_is_discarded_and_reported(self, tmp_path):
        """A write that stalled across a newer one must not put back a token a
        stop may have revoked, nor be recorded as held."""
        code, src, now = self._run(tmp_path, {"access_token": "held", "iat_ms": 300}, issued=200)

        assert code != 0
        assert now["access_token"] == "held"
        assert not src.exists()

    def test_a_file_without_an_issue_time_is_replaced(self, tmp_path):
        code, _, now = self._run(tmp_path, {"access_token": "legacy"}, issued=200)

        assert code == 0
        assert now["access_token"] == "new"


class TestDue:
    def test_due_until_this_sandbox_holds_one_with_time_left(self):
        later = datetime.now(UTC) + timedelta(minutes=50)
        soon = datetime.now(UTC) + timedelta(minutes=10)

        assert data_tokens.due(db.DataTokenRow(), SANDBOX_ID)
        assert data_tokens.due(db.DataTokenRow(later, None, 3), SANDBOX_ID)
        assert data_tokens.due(db.DataTokenRow(later, "other", 3), SANDBOX_ID)
        assert data_tokens.due(db.DataTokenRow(soon, SANDBOX_ID, 3), SANDBOX_ID)
        assert not data_tokens.due(db.DataTokenRow(later, SANDBOX_ID, 3), SANDBOX_ID)


def _row() -> dict:
    return {
        "computer_id": COMPUTER_ID,
        "user_id": USER_ID,
        "provider_ref": SANDBOX_ID,
        "kind": "daytona",
        "provider_config": None,
        "root_dir": "/home/workspace",
        "generation": 0,
        "is_always_on": True,
        "last_activity": None,
    }


def _conn(acquired: bool, executed: list[str]):
    @asynccontextmanager
    async def get_db_connection():
        cur = MagicMock()
        cur.fetchone = AsyncMock(return_value=(acquired,))

        async def execute(sql, params=None):
            executed.append(sql)
            return cur

        @asynccontextmanager
        async def cursor():
            yield cur

        cur.execute = AsyncMock(side_effect=execute)
        conn = MagicMock()
        conn.execute = AsyncMock(side_effect=execute)
        conn.cursor = cursor
        yield conn

    return get_db_connection


class TestSweepClaim:
    @pytest.mark.asyncio
    async def test_a_computer_claimed_elsewhere_is_left_alone(self):
        sweeper = DataTokenSweeper()
        locked = AsyncMock()
        executed: list[str] = []

        with (
            patch("src.server.database.pool.get_db_connection", _conn(False, executed)),
            patch.object(sweeper, "_write_locked", locked),
        ):
            assert await sweeper._sweep_one(_row()) is False

        locked.assert_not_awaited()
        assert not any("pg_advisory_unlock" in sql for sql in executed)

    @pytest.mark.asyncio
    async def test_the_claim_is_released_after_the_write(self):
        sweeper = DataTokenSweeper()
        locked = AsyncMock(side_effect=RuntimeError("sandbox unreachable"))
        executed: list[str] = []

        with (
            patch("src.server.database.pool.get_db_connection", _conn(True, executed)),
            patch.object(sweeper, "_write_locked", locked),
            pytest.raises(RuntimeError),
        ):
            await sweeper._sweep_one(_row())

        locked.assert_awaited_once_with(COMPUTER_ID, _row())
        assert any("pg_try_advisory_lock" in sql for sql in executed)
        assert any("pg_advisory_unlock" in sql for sql in executed)

    @pytest.mark.asyncio
    async def test_a_row_written_since_the_listing_is_not_minted_again(self):
        sweeper = DataTokenSweeper()
        held = db.DataTokenRow(
            datetime.now(UTC) + timedelta(minutes=55), SANDBOX_ID, 2
        )
        push = AsyncMock()

        with (
            patch.object(db, "load", AsyncMock(return_value=held)),
            patch.object(data_tokens, "push", push),
        ):
            assert await sweeper._write_locked(COMPUTER_ID, _row()) is False

        push.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_sandbox_paused_during_the_mint_is_not_written(self):
        # Attaching resumes a paused E2B sandbox, which would undo the stop.
        from ptc_agent.core.sandbox.runtime import RuntimeState

        sweeper = DataTokenSweeper()
        runtime = MagicMock()
        runtime.get_state = AsyncMock(return_value=RuntimeState.RUNNING)
        runtime.refresh_state = AsyncMock(return_value=RuntimeState.STOPPED)
        runtime.fetch_working_dir = AsyncMock(return_value="/home/workspace")

        @asynccontextmanager
        async def detached(*_a, **_k):
            yield runtime

        manager = MagicMock(_detached_runtime=detached)
        publish = AsyncMock(return_value=True)
        mark_held = AsyncMock()
        minted = data_tokens.Minted("tok", ISSUED_AT_MS, EXPIRES_AT)

        with (
            patch.object(db, "load", AsyncMock(return_value=db.DataTokenRow())),
            patch(
                "src.server.services.computer_manager.ComputerManager.get_instance",
                return_value=manager,
            ),
            patch.object(data_tokens, "_mint", AsyncMock(return_value=minted)),
            patch.object(db, "save", AsyncMock(return_value=db.Saved(True, 1))),
            patch.object(db, "mark_held", mark_held),
            patch("ptc_agent.core.sandbox._shared.publish_private_file", publish),
        ):
            assert await sweeper._write_locked(COMPUTER_ID, _row()) is False

        publish.assert_not_awaited()
        mark_held.assert_not_awaited()


class TestSweepPass:
    @staticmethod
    def _sweeper_with(pages: list[list[dict]]):
        sweeper = DataTokenSweeper()
        claim = AsyncMock(side_effect=pages)
        patches = (
            patch.object(db, "claim_due", claim),
            patch.object(sweeper, "_sweep_one", AsyncMock(return_value=True)),
        )
        return sweeper, claim, patches

    @pytest.mark.asyncio
    async def test_a_pass_claims_pages_until_one_comes_back_short(self):
        full = [_row()] * data_token_sweep.SWEEP_BATCH_LIMIT
        sweeper, claim, patches = self._sweeper_with([full, [_row()] * 3])

        with patches[0], patches[1]:
            assert await sweeper.sweep_once() == data_token_sweep.SWEEP_BATCH_LIMIT + 3

        assert claim.await_count == 2
        assert claim.await_args.kwargs["retry_after"] == data_token_sweep.RETRY_AFTER

    @pytest.mark.asyncio
    async def test_a_pass_ends_after_its_page_bound(self):
        full = [_row()] * data_token_sweep.SWEEP_BATCH_LIMIT
        sweeper, claim, patches = self._sweeper_with(
            [full] * (data_token_sweep.SWEEP_MAX_PAGES + 1)
        )

        with patches[0], patches[1]:
            await sweeper.sweep_once()

        assert claim.await_count == data_token_sweep.SWEEP_MAX_PAGES


class TestSweepSparesIdleComputers:
    """Writing into a sandbox counts as activity at its provider, so renewing
    an idle computer's token would keep it from ever auto-stopping."""

    IDLE_LIMIT_S = 3000

    @staticmethod
    def _executor(active: bool):
        executor = MagicMock()
        executor.get_instance.return_value.has_active_tasks_for_computer = AsyncMock(
            return_value=active
        )
        return patch("src.server.services.runs.executor.LocalRunExecutor", executor)

    @classmethod
    def _manager(cls):
        manager = SimpleNamespace(_idle_timeout_for=lambda row: cls.IDLE_LIMIT_S)
        return patch(
            "src.server.services.computer_manager.ComputerManager.get_instance",
            return_value=manager,
        )

    @classmethod
    def _idle_row(cls, idle_s: float) -> dict:
        used = datetime.now(UTC) - timedelta(seconds=idle_s)
        return {**_row(), "is_always_on": False, "last_activity": used}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("idle_s", "kept"), [(IDLE_LIMIT_S - 60, True), (IDLE_LIMIT_S + 60, False)]
    )
    async def test_recent_use_is_judged_by_the_providers_idle_limit(
        self, idle_s, kept
    ):
        sweeper = DataTokenSweeper()

        with self._manager(), self._executor(False):
            assert await sweeper._keeps_running(self._idle_row(idle_s)) is kept

    @pytest.mark.asyncio
    async def test_an_idle_computer_with_nothing_running_is_not_written(self):
        sweeper = DataTokenSweeper()
        locked = AsyncMock()
        executed: list[str] = []

        with (
            self._manager(),
            self._executor(False),
            patch("src.server.database.pool.get_db_connection", _conn(True, executed)),
            patch.object(sweeper, "_write_locked", locked),
        ):
            assert await sweeper._sweep_one(self._idle_row(4 * 3600)) is False

        locked.assert_not_awaited()
        assert executed == []

    @pytest.mark.asyncio
    async def test_an_idle_computer_with_work_in_flight_is_written(self):
        sweeper = DataTokenSweeper()
        locked = AsyncMock(return_value=True)

        with (
            self._manager(),
            self._executor(True),
            patch("src.server.database.pool.get_db_connection", _conn(True, [])),
            patch.object(sweeper, "_write_locked", locked),
        ):
            assert await sweeper._sweep_one(self._idle_row(4 * 3600)) is True


class TestTheAcquireKeepsTheToken:
    @pytest.mark.asyncio
    async def test_a_warm_acquire_writes_the_token_when_it_runs_low(self):
        """No asset sync runs on a warm acquire, and the sweep leaves an idle
        computer's token to lapse, so the acquire is what renews it."""

        @asynccontextmanager
        async def in_use(_workspace_id):
            yield

        session = SimpleNamespace(sandbox=SimpleNamespace(runtime=object()))
        binding = SimpleNamespace(computer_id=COMPUTER_ID, root_dir="/home/workspace", dir_name=None)
        manager = SimpleNamespace(
            _acquire_session=AsyncMock(return_value=session),
            resolve_binding=AsyncMock(return_value=binding),
            _keep_data_token=AsyncMock(),
            _settle_folders=AsyncMock(),
            _ensure_project_attached=AsyncMock(),
        )

        with patch("src.server.services.workspace_manager.workspace_folder_in_use", in_use):
            got = await WorkspaceManager.get_session_for_workspace(
                manager, "ws-1", user_id=USER_ID
            )

        assert got is session
        manager._keep_data_token.assert_awaited_once_with(COMPUTER_ID, USER_ID, session.sandbox)
