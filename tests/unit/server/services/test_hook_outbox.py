"""Hook outbox (v4 1.7, I5) — decision table, executors, drainer ack/nack.

``build_finalize_jobs`` is the single mapping from a run's CAS-adopted final
status to its durable post-commit effects; executors validate their own
applicability (ordinary runs no-op) and RAISE on transport failure so the
drainer's nack/backoff — not a swallowed log line — is the retry path.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.server.contracts.status import TERMINAL_STATUSES
from src.server.database.runs.outbox import (
    build_finalize_jobs,
    build_finalize_jobs_from_run_row,
)
from src.server.services.report_back.handoff import core
from src.server.services.report_back import subagent as task_report_back
from src.server.services.report_back.handoff.pointer import ClearOutcome
from src.server.services import hook_outbox
from src.server.services.hook_outbox import HookOutboxDrainer

EXECUTOR_MOD = "src.server.services.report_back.handoff.executor"
RESERVE_MOD = "src.server.services.report_back.handoff.reserve"
POINTER_MOD = "src.server.services.report_back.handoff.pointer"
WAKE_MOD = "src.server.services.report_back.handoff.wake"


@pytest.fixture(autouse=True)
def _registered_executors():
    """Mirror app setup: handler executors registered before any drainer use.

    Registration is idempotent for the same function, so autouse is safe."""
    core.register_outbox_executors()
    task_report_back.register_outbox_executors()


# ---------------------------------------------------------------------------
# Decision table
# ---------------------------------------------------------------------------


def _jobs(final_status, **kw):
    defaults = dict(
        run_id="run-1",
        thread_id="ptc-1",
        user_id="u-1",
        burst_slot_id="slot-1",
    )
    defaults.update(kw)
    return build_finalize_jobs(**defaults)(final_status)


def _types(jobs):
    return sorted(j.hook_type for j in jobs)


_RELEASES_ITS_PAIR = {
    "completed": "watch_clear",
    "error": "watch_clear",
    "cancelled": "watch_clear",
    "interrupted": None,
}


class TestBuildFinalizeJobs:
    def test_completed_ptc_gets_burst_release_and_report_back(self):
        jobs = _jobs("completed")
        assert _types(jobs) == ["burst_release", "report_back", "user_feed"]
        rb = next(j for j in jobs if j.hook_type == "report_back")
        assert rb.payload == {
            "ptc_thread_id": "ptc-1",
            "dispatch_gen": None,
            "final_status": "completed",
        }
        # No origin stamped -> falls back to the run's own thread.
        assert rb.ordering_key == "ptc-1"
        assert rb.idempotency_key == "run-1:report_back"

    def test_origin_dispatcher_thread_keys_the_whole_lifecycle_chain(self):
        """One ordering rule: every report-back-lifecycle job keys on the
        WATCHING Chief of Staff thread, so N dispatched PTCs reporting into one
        such thread serialize strictly across competing drainers."""
        for status, hook in [
            ("completed", "report_back"),
            ("interrupted", "needs_input_wake"),
            ("error", "report_back"),
            ("cancelled", "report_back"),
        ]:
            jobs = _jobs(status, dispatcher_thread_id="cos-9")
            job = next(j for j in jobs if j.hook_type == hook)
            assert job.ordering_key == "cos-9", (status, hook)

    def test_interrupted_ptc_gets_needs_input_wake(self):
        jobs = _jobs("interrupted")
        assert _types(jobs) == ["burst_release", "needs_input_wake", "user_feed"]
        wake = next(j for j in jobs if j.hook_type == "needs_input_wake")
        assert wake.payload == {"ptc_thread_id": "ptc-1"}
        assert wake.ordering_key == "ptc-1"

    @pytest.mark.parametrize("status", ["error", "cancelled"])
    def test_a_stopped_or_failed_ptc_run_reports_back_how_it_ended(self, status):
        """The watching thread promised the user a report; a stop or a
        failure is delivered like a result, and the summary's own completion
        releases the pair, so nothing tears it down here."""
        jobs = _jobs(status, origin_dispatch_gen="gen-3")
        assert _types(jobs) == ["burst_release", "report_back", "user_feed"]
        rb = next(j for j in jobs if j.hook_type == "report_back")
        assert rb.payload == {
            "ptc_thread_id": "ptc-1",
            "dispatch_gen": "gen-3",
            "final_status": status,
        }

    @pytest.mark.parametrize(
        ("role", "owed"),
        [
            (
                {},
                {
                    "completed": "report_back",
                    "error": "report_back",
                    "cancelled": "report_back",
                    "interrupted": "needs_input_wake",
                },
            ),
            (
                {"thread_id": "home-1", "report_back_ptc_thread_id": "ptc-9"},
                _RELEASES_ITS_PAIR,
            ),
        ],
        ids=["dispatched_ptc", "summary_on_home"],
    )
    def test_the_runs_role_decides_what_it_owes(self, role, owed):
        """A dispatched PTC run reports however it ends and wakes its watcher
        when it asks the user something. A summary run is the report-back
        itself, so it only releases its pair once over: if it owed another
        report, a failing summary would report on itself."""
        assert set(owed) == set(TERMINAL_STATUSES)
        for status, hook in owed.items():
            expected = ["burst_release", "user_feed", *([hook] if hook else [])]
            assert _types(_jobs(status, **role)) == sorted(expected), status

    @pytest.mark.parametrize("status", ["completed", "error", "cancelled"])
    def test_a_summary_releases_its_pair_by_the_origin_id(self, status):
        """A stopped or failed summary wakes the watcher so the card
        reconciles; a completed one is the consumption clear."""
        jobs = _jobs(
            status,
            thread_id="home-1",
            report_back_ptc_thread_id="ptc-9",
            origin_dispatch_gen="gen-3",
        )
        (wc,) = [j for j in jobs if j.hook_type == "watch_clear"]
        assert wc.payload == {
            "ptc_thread_id": "ptc-9",
            "user_id": "u-1",
            "error_wake": status != "completed",
            "dispatch_gen": "gen-3",
        }
        assert wc.idempotency_key == "run-1:watch_clear"
        # Ordered on its own thread: the summary runs on the watching thread,
        # the same chain as the report_back job that started it.
        assert wc.ordering_key == "home-1"

    def test_user_feed_rides_every_finalize_with_a_user(self):
        """run_settled is the at-least-once push behind the thread-lifecycle
        feed: PUBLIC status vocabulary, interrupt_reason only when
        interrupted, no ordering_key (independent per run)."""
        settled = next(
            j for j in _jobs("error", run_seq=42, workspace_id="ws-1")
            if j.hook_type == "user_feed"
        )
        assert settled.idempotency_key == "run-1:user_feed"
        assert settled.ordering_key is None
        assert settled.payload == {
            "user_id": "u-1",
            "thread_id": "ptc-1",
            "workspace_id": "ws-1",
            "run_id": "run-1",
            "run_seq": 42,
            "status": "failed",
            "interrupt_reason": None,
        }
        interrupted = next(
            j
            for j in _jobs("interrupted", interrupt_reason="ask_user")
            if j.hook_type == "user_feed"
        )
        assert interrupted.payload["status"] == "interrupted"
        assert interrupted.payload["interrupt_reason"] == "ask_user"
        # No user -> no feed to publish to.
        assert "user_feed" not in _types(_jobs("completed", user_id=None))

    def test_no_user_or_slot_skips_burst_release(self):
        assert "burst_release" not in _types(_jobs("completed", user_id=None))
        assert "burst_release" not in _types(
            _jobs("completed", burst_slot_id=None)
        )

    def test_idempotency_keys_are_run_scoped_and_stable(self):
        """A re-finalize (lost race, sweep retry) enqueues the same keys —
        ON CONFLICT DO NOTHING makes the effect exactly-once per run."""
        a = {j.idempotency_key for j in _jobs("error")}
        b = {j.idempotency_key for j in _jobs("error")}
        assert a == b == {
            "run-1:burst_release",
            "run-1:user_feed",
            "run-1:report_back",
        }

    def test_from_run_row_rebuilds_from_start_stamped_metadata(self):
        factory = build_finalize_jobs_from_run_row(
            {
                "conversation_response_id": "run-7",
                "conversation_thread_id": "ptc-7",
                "metadata": {
                    "msg_type": "ptc",
                    "user_id": "u-7",
                    "burst_slot_id": "slot-7",
                    "origin_home_thread_id": "cos-7",
                },
            }
        )
        jobs = factory("error")
        assert _types(jobs) == ["burst_release", "report_back", "user_feed"]
        rb = next(j for j in jobs if j.hook_type == "report_back")
        assert rb.payload["ptc_thread_id"] == "ptc-7"
        assert rb.payload["final_status"] == "error"
        # START-stamped origin routes the chain to the watching Chief of Staff thread.
        assert rb.ordering_key == "cos-7"

    def test_an_automation_turn_settles_its_firing_on_every_terminal(self):
        """The firing ends as its run did, whichever worker finalized it. A
        run a concurrent one superseded at admission was never the firing's
        turn: the firing is back in line for the thread instead."""
        row = {
            "conversation_response_id": "run-7",
            "conversation_thread_id": "home-7",
            "metadata": {
                "msg_type": "ptc",
                "user_id": "u-7",
                "workspace_id": "ws-7",
                "automation_execution_id": "exec-7",
                "automation_id": "auto-7",
            },
        }
        for status in ("completed", "interrupted", "error", "cancelled"):
            jobs = build_finalize_jobs_from_run_row(row)(status)
            settle = next(j for j in jobs if j.hook_type == "automation_settle")
            assert settle.idempotency_key == "run-7:automation_settle"
            assert settle.payload == {
                "execution_id": "exec-7",
                "automation_id": "auto-7",
                "user_id": "u-7",
                "workspace_id": "ws-7",
            }
        superseded = {**row, "metadata": {**row["metadata"], "superseded": True}}
        assert "automation_settle" not in _types(
            build_finalize_jobs_from_run_row(superseded)("cancelled")
        )
        assert "automation_settle" not in _types(_jobs("completed"))


# ---------------------------------------------------------------------------
# watch_clear executor (replaces BTM._clear_report_back_watch)
# ---------------------------------------------------------------------------


def _cache_with_origin(origin, marker=None):
    """Fake cache keyed like the executor reads it: the ptc-1 origin blob and
    (optionally) the ptc-1 admission marker; everything else reads None."""
    from src.server.services.report_back.handoff.keys import ptc_origin_key

    cache = MagicMock()
    cache.enabled = True
    cache.client = MagicMock()
    cache.client.publish = AsyncMock()
    values = {
        ptc_origin_key("ptc-1"): origin,
        "workflow:status:ptc-1": marker,
    }
    cache.get_strict = AsyncMock(side_effect=lambda key: values.get(key))
    return cache


def _guard(owned: bool = True):
    """Stand-in for fenced_job_guard yielding fixed ownership."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def guard(job_id, attempts):
        yield owned

    return patch.object(hook_outbox.outbox_db, "fenced_job_guard", guard)


class TestExecWatchClear:
    @pytest.mark.asyncio
    async def test_clears_via_origin_and_wakes_on_error(self):
        from src.server.services.report_back.handoff.keys import (
            ptc_origin_key,
            thread_wake_key,
        )

        cache = _cache_with_origin({"home_thread_id": "cos-1", "user_id": "u-1"})
        mock_clear = AsyncMock(return_value=ClearOutcome(True))
        with _guard(), patch(
            "src.utils.cache.redis_cache.get_cache_client", return_value=cache
        ), patch(
            f"{POINTER_MOD}.clear_handoff_report_back",
            mock_clear,
        ):
            await core._exec_watch_clear(
                _job(
                    hook_type="watch_clear",
                    payload={"ptc_thread_id": "ptc-1", "user_id": "u-1", "error_wake": True},
                    ordering_key="cos-1",
                )
            )

        cache.get_strict.assert_awaited_once_with(ptc_origin_key("ptc-1"))
        mock_clear.assert_awaited_once_with(
            cache, "ptc-1", "cos-1", user_id="u-1", expected_gen=None,
            refuse_if_pointer=False,
        )
        assert cache.client.publish.await_args.args[0] == thread_wake_key("cos-1")

    @pytest.mark.asyncio
    async def test_stale_key_requeues_onto_dispatcher_chain_without_teardown(self):
        """Codex round-18 P1: off the dispatching thread's chain, this teardown — and the
        orphan resolver it can escalate to — runs CONCURRENTLY with the
        chain's report_back admission; a resolver settling the pair between
        the admission's atomic pointer claim and the route consummating it
        schedules an orphan summary behind a settled pair. A stale-keyed job
        must requeue onto that chain (where the open report_back lease
        serializes it past the admission window), touching nothing here."""
        cache = _cache_with_origin({"home_thread_id": "cos-1", "user_id": "u-1"})
        mock_clear = AsyncMock()
        mock_requeue = AsyncMock(return_value="pending")
        with _guard(), patch(
            "src.utils.cache.redis_cache.get_cache_client", return_value=cache
        ), patch(
            f"{POINTER_MOD}.clear_handoff_report_back",
            mock_clear,
        ), patch.object(
            hook_outbox.outbox_db, "requeue_job_with_key", mock_requeue
        ):
            await core._exec_watch_clear(
                _job(
                    hook_type="watch_clear",
                    payload={"ptc_thread_id": "ptc-1", "user_id": "u-1", "error_wake": True},
                    ordering_key="ptc-1",
                )
            )
        mock_requeue.assert_awaited_once_with(
            "job-1",
            attempts=1,
            ordering_key="cos-1",
            max_attempts=hook_outbox.MAX_ATTEMPTS,
        )
        mock_clear.assert_not_called()
        cache.client.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dead_compensation_threads_pointer_refusal(self):
        """Codex round-20 P1: a dead report_back's compensation clear becomes
        chain head the moment its source dies (a dead row holds no lease) —
        while the source may have stalled MID-ADMISSION with the run pointer
        already claimed. The compensation payload carries refuse_if_pointer
        and the executor threads it, so pointer-first refuses cleanup (the
        admission's own lifecycle owns teardown) and cleanup-first still
        410s the late claim via the membership gate."""
        cache = _cache_with_origin({"home_thread_id": "cos-1", "user_id": "u-1"})
        mock_clear = AsyncMock(return_value=ClearOutcome(False, None))
        with _guard(), patch(
            "src.utils.cache.redis_cache.get_cache_client", return_value=cache
        ), patch(
            f"{POINTER_MOD}.clear_handoff_report_back",
            mock_clear,
        ):
            await core._exec_watch_clear(
                _job(
                    hook_type="watch_clear",
                    payload={
                        "ptc_thread_id": "ptc-1",
                        "user_id": "u-1",
                        "error_wake": True,
                        "dispatch_gen": "g-1",
                        "refuse_if_pointer": True,
                    },
                    ordering_key="cos-1",
                )
            )
        assert mock_clear.await_args.kwargs["refuse_if_pointer"] is True
        # Refusal (falsy outcome, no fencer gen) short-circuits BOTH the wake
        # and the phantom-resolver escalation — the live admission settles it.
        cache.client.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_origin_without_dispatcher_thread_executes_in_place(self):
        """No dispatching thread chain to requeue onto -> no admission to race: execute
        here rather than requeueing onto a None key."""
        cache = _cache_with_origin({"user_id": "u-1"})
        mock_clear = AsyncMock(return_value=ClearOutcome(True))
        mock_requeue = AsyncMock()
        with _guard(), patch(
            "src.utils.cache.redis_cache.get_cache_client", return_value=cache
        ), patch(
            f"{POINTER_MOD}.clear_handoff_report_back",
            mock_clear,
        ), patch.object(
            hook_outbox.outbox_db, "requeue_job_with_key", mock_requeue
        ):
            await core._exec_watch_clear(
                _job(
                    hook_type="watch_clear",
                    payload={"ptc_thread_id": "ptc-1", "user_id": "u-1", "error_wake": True},
                    ordering_key="ptc-1",
                )
            )
        mock_requeue.assert_not_awaited()
        mock_clear.assert_awaited_once_with(
            cache, "ptc-1", None, user_id="u-1", expected_gen=None,
            refuse_if_pointer=False,
        )

    @pytest.mark.asyncio
    async def test_fenced_clear_suppressed_resolution_stays_silent(self):
        """A fenced clear whose resolution the atomic resolver suppresses
        (fencer admitted / origin moved / live foreign run) must neither tear
        anything down nor misreport the live owner as failed."""
        cache = _cache_with_origin(
            {"home_thread_id": "cos-1", "user_id": "u-1", "dispatch_gen": "g-NEW"},
        )
        mock_clear = AsyncMock(return_value=ClearOutcome(False, "g-NEW"))
        mock_resolve = AsyncMock(return_value=(False, None))  # e.g. admitted
        with _guard(), patch(
            "src.utils.cache.redis_cache.get_cache_client", return_value=cache
        ), patch(
            f"{POINTER_MOD}.clear_handoff_report_back",
            mock_clear,
        ), patch(
            f"{RESERVE_MOD}.resolve_orphaned_watch",
            mock_resolve,
        ):
            await core._exec_watch_clear(
                _job(
                    hook_type="watch_clear",
                    payload={
                        "ptc_thread_id": "ptc-1",
                        "user_id": "u-1",
                        "error_wake": True,
                        "dispatch_gen": "g-OLD",
                    },
                    ordering_key="cos-1",
                )
            )
        assert mock_clear.await_args.kwargs["expected_gen"] == "g-OLD"
        # The resolver is consulted with the EXACT generation that fenced the
        # teardown — not a fresh origin read, which a newer reservation could
        # have replaced in the meantime.
        mock_resolve.assert_awaited_once_with(
            cache, "ptc-1", "cos-1", "u-1", fencer_gen="g-NEW", job_gen="g-OLD"
        )
        cache.client.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fenced_clear_by_unadmitted_reservation_still_wakes(self):
        """A fence held by a reservation that never achieved admission
        (lost-409 continuation) must NOT swallow the predecessor's only
        failure signal — the pair's client-facing state is durably resolved
        (and the phantom receipted) BEFORE the wake goes out, even though
        the teardown stays fenced."""
        from src.server.services.report_back.handoff.keys import thread_wake_key

        cache = _cache_with_origin(
            {"home_thread_id": "cos-1", "user_id": "u-1", "dispatch_gen": "g-PHANTOM"},
        )
        mock_clear = AsyncMock(return_value=ClearOutcome(False, "g-PHANTOM"))
        mock_resolve = AsyncMock(return_value=(True, "rb-1"))
        with _guard(), patch(
            "src.utils.cache.redis_cache.get_cache_client", return_value=cache
        ), patch(
            f"{POINTER_MOD}.clear_handoff_report_back",
            mock_clear,
        ), patch(
            f"{RESERVE_MOD}.resolve_orphaned_watch",
            mock_resolve,
        ):
            await core._exec_watch_clear(
                _job(
                    hook_type="watch_clear",
                    payload={
                        "ptc_thread_id": "ptc-1",
                        "user_id": "u-1",
                        "error_wake": True,
                        "dispatch_gen": "g-OLD",
                    },
                    ordering_key="cos-1",
                )
            )
        # Durable resolution BEFORE the ephemeral wake: memberships/pointer
        # fall so even a dropped nudge degrades to a poll of resolved state.
        mock_resolve.assert_awaited_once_with(
            cache,
            "ptc-1",
            "cos-1",
            "u-1",
            fencer_gen="g-PHANTOM",
            job_gen="g-OLD",
        )
        assert cache.client.publish.await_args.args[0] == thread_wake_key("cos-1")

    @pytest.mark.asyncio
    async def test_fenced_clear_without_fencer_gen_stays_silent(self):
        """Defensive: a falsy ClearOutcome with no fencing generation (Redis-
        less degradation shapes) has nothing sound to resolve against —
        never guess."""
        cache = _cache_with_origin(
            {"home_thread_id": "cos-1", "user_id": "u-1"},
        )
        mock_clear = AsyncMock(return_value=ClearOutcome(False, None))
        mock_resolve = AsyncMock()
        with _guard(), patch(
            "src.utils.cache.redis_cache.get_cache_client", return_value=cache
        ), patch(
            f"{POINTER_MOD}.clear_handoff_report_back",
            mock_clear,
        ), patch(
            f"{RESERVE_MOD}.resolve_orphaned_watch",
            mock_resolve,
        ):
            await core._exec_watch_clear(
                _job(
                    hook_type="watch_clear",
                    payload={
                        "ptc_thread_id": "ptc-1",
                        "user_id": "u-1",
                        "error_wake": True,
                        "dispatch_gen": "g-OLD",
                    },
                    ordering_key="cos-1",
                )
            )
        mock_resolve.assert_not_awaited()
        cache.client.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fenced_resolve_failure_propagates_for_nack(self):
        """The resolver must not decide on missing state: an unavailable
        Redis nacks the job for retry instead of guessing."""
        cache = _cache_with_origin(
            {"home_thread_id": "cos-1", "user_id": "u-1", "dispatch_gen": "g-NEW"},
        )
        mock_clear = AsyncMock(return_value=ClearOutcome(False, "g-NEW"))
        mock_resolve = AsyncMock(side_effect=ConnectionError("redis down"))
        with _guard(), patch(
            "src.utils.cache.redis_cache.get_cache_client", return_value=cache
        ), patch(
            f"{POINTER_MOD}.clear_handoff_report_back",
            mock_clear,
        ), patch(
            f"{RESERVE_MOD}.resolve_orphaned_watch",
            mock_resolve,
        ):
            with pytest.raises(ConnectionError):
                await core._exec_watch_clear(
                    _job(
                        hook_type="watch_clear",
                        payload={
                            "ptc_thread_id": "ptc-1",
                            "user_id": "u-1",
                            "error_wake": True,
                            "dispatch_gen": "g-OLD",
                        },
                        ordering_key="cos-1",
                    )
                )
        cache.client.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_consumption_clear_publishes_cleared_wake(self):
        """A landed consumption clear pushes the pending→idle transition so
        watchers drop the chip now instead of riding the 60s backstop. No
        error wake — the payload says ``cleared``, not ``error``."""
        cache = _cache_with_origin({"home_thread_id": "cos-1", "user_id": "u-1"})
        mock_clear = AsyncMock(return_value=ClearOutcome(True))
        with _guard(), patch(
            "src.utils.cache.redis_cache.get_cache_client", return_value=cache
        ), patch(
            f"{POINTER_MOD}.clear_handoff_report_back",
            mock_clear,
        ):
            await core._exec_watch_clear(
                _job(
                    hook_type="watch_clear",
                    payload={"ptc_thread_id": "ptc-1", "user_id": "u-1", "error_wake": False},
                    ordering_key="cos-1",
                )
            )

        mock_clear.assert_awaited_once()
        cache.client.publish.assert_awaited_once()
        published = cache.client.publish.await_args.args[1]
        assert '"cleared": true' in published
        assert "error" not in published

    @pytest.mark.asyncio
    async def test_fenced_consumption_clear_owned_by_its_fencer_stays_silent(self):
        """Fenced by a generation the resolver finds admitted or live: the
        watcher is still legitimately pending, so no wake of any kind."""
        cache = _cache_with_origin({"home_thread_id": "cos-1", "user_id": "u-1"})
        mock_clear = AsyncMock(return_value=ClearOutcome(False, "g-NEW"))
        mock_resolve = AsyncMock(return_value=(False, None))
        with _guard(), patch(
            "src.utils.cache.redis_cache.get_cache_client", return_value=cache
        ), patch(
            f"{POINTER_MOD}.clear_handoff_report_back",
            mock_clear,
        ), patch(
            f"{RESERVE_MOD}.resolve_orphaned_watch",
            mock_resolve,
        ):
            await core._exec_watch_clear(
                _job(
                    hook_type="watch_clear",
                    payload={
                        "ptc_thread_id": "ptc-1",
                        "user_id": "u-1",
                        "error_wake": False,
                        "dispatch_gen": "g-OLD",
                    },
                    ordering_key="cos-1",
                )
            )

        mock_resolve.assert_awaited_once_with(
            cache, "ptc-1", "cos-1", "u-1", fencer_gen="g-NEW", job_gen="g-OLD"
        )
        cache.client.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fenced_consumption_clear_resolves_a_phantom_fencer(self):
        """A consumption clear fenced by a never-admitted reservation has no
        run left to drain the pair, so it resolves the phantom like a failure
        clear would. The summary landed, so the wake says ``cleared``."""
        cache = _cache_with_origin(
            {"home_thread_id": "cos-1", "user_id": "u-1", "dispatch_gen": "g-PHANTOM"},
        )
        mock_clear = AsyncMock(return_value=ClearOutcome(False, "g-PHANTOM"))
        mock_resolve = AsyncMock(return_value=(True, "rb-1"))
        with _guard(), patch(
            "src.utils.cache.redis_cache.get_cache_client", return_value=cache
        ), patch(
            f"{POINTER_MOD}.clear_handoff_report_back",
            mock_clear,
        ), patch(
            f"{RESERVE_MOD}.resolve_orphaned_watch",
            mock_resolve,
        ):
            await core._exec_watch_clear(
                _job(
                    hook_type="watch_clear",
                    payload={
                        "ptc_thread_id": "ptc-1",
                        "user_id": "u-1",
                        "error_wake": False,
                        "dispatch_gen": "g-OLD",
                    },
                    ordering_key="cos-1",
                )
            )

        mock_resolve.assert_awaited_once_with(
            cache, "ptc-1", "cos-1", "u-1", fencer_gen="g-PHANTOM", job_gen="g-OLD"
        )
        cache.client.publish.assert_awaited_once()
        published = cache.client.publish.await_args.args[1]
        assert '"cleared": true' in published
        assert "error" not in published

    @pytest.mark.asyncio
    async def test_noop_without_origin(self):
        """Ordinary run, or an already-cleared watch (idempotent retry)."""
        cache = _cache_with_origin(None)
        mock_clear = AsyncMock()
        with _guard(), patch(
            "src.utils.cache.redis_cache.get_cache_client", return_value=cache
        ), patch(
            f"{POINTER_MOD}.clear_handoff_report_back",
            mock_clear,
        ):
            await core._exec_watch_clear(
                _job(
                    hook_type="watch_clear",
                    payload={"ptc_thread_id": "cos-1", "user_id": "u-1", "error_wake": True},
                )
            )
        mock_clear.assert_not_called()
        cache.client.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_clear_failure_propagates_for_nack(self):
        """Inverted from the old best-effort hook: a failed clear must raise
        so the drainer nacks and retries — swallowing would ack a leaked
        watch + per-user cap slot."""
        cache = _cache_with_origin({"home_thread_id": "cos-1"})
        with _guard(), patch(
            "src.utils.cache.redis_cache.get_cache_client", return_value=cache
        ), patch(
            f"{POINTER_MOD}.clear_handoff_report_back",
            AsyncMock(side_effect=RuntimeError("redis down")),
        ):
            with pytest.raises(RuntimeError):
                await core._exec_watch_clear(
                    _job(
                        hook_type="watch_clear",
                        payload={"ptc_thread_id": "ptc-1", "error_wake": True},
                        ordering_key="cos-1",
                    )
                )

    @pytest.mark.asyncio
    async def test_origin_read_failure_propagates_for_nack(self):
        """The origin lookup is a STRICT read: a Redis blip must raise (→
        drainer nack/retry), never degrade to "no origin" and ack a dropped
        clear."""
        cache = _cache_with_origin(None)
        cache.get_strict = AsyncMock(side_effect=ConnectionError("redis down"))
        with patch(
            "src.utils.cache.redis_cache.get_cache_client", return_value=cache
        ):
            with pytest.raises(ConnectionError):
                await core._exec_watch_clear(
                    _job(
                        hook_type="watch_clear",
                        payload={"ptc_thread_id": "ptc-1", "error_wake": True},
                    )
                )
            with pytest.raises(ConnectionError):
                await core._exec_needs_input_wake(
                    _job(
                        hook_type="needs_input_wake",
                        payload={"ptc_thread_id": "ptc-1"},
                    )
                )

    @pytest.mark.asyncio
    async def test_runtime_disabled_cache_nacks_not_acks(self):
        """Codex round-4 probe: a failed startup connect flips enabled=False
        with a client still present; a drainer booted in that state must NACK
        pending jobs (real get_strict raises), never ack them as config-off
        no-ops — Redis may come back with the effects still owed."""
        from src.server.database.runs import outbox as outbox_db
        from src.utils.cache.redis_cache import RedisCacheClient

        cache = RedisCacheClient(url="redis://unit-test-never-connects:6379/0")
        cache.enabled = False  # what connect() leaves behind on failure
        cache.client = MagicMock()  # present but unusable

        drainer = HookOutboxDrainer()
        with patch(
            "src.utils.cache.redis_cache.get_cache_client", return_value=cache
        ), patch.object(outbox_db, "ack_outbox_job", AsyncMock()) as ack, patch.object(
            outbox_db, "nack_outbox_job", AsyncMock(return_value="pending")
        ) as nack:
            await drainer._execute(
                _job(hook_type="watch_clear", payload={"ptc_thread_id": "ptc-1"})
            )
            await drainer._execute(
                _job(hook_type="needs_input_wake", payload={"ptc_thread_id": "ptc-1"})
            )
        assert nack.await_count == 2
        ack.assert_not_awaited()


# ---------------------------------------------------------------------------
# report_back executor — thin delegation, full job row (the executor needs the
# job id for lease heartbeats + the dispatched_run_id payload merge)
# ---------------------------------------------------------------------------


class TestExecReportBack:
    @pytest.mark.asyncio
    async def test_delegates_full_job_row(self):
        with patch(
            f"{EXECUTOR_MOD}.execute_report_back",
            AsyncMock(),
        ) as exec_rb:
            job = _job(hook_type="report_back", payload={"ptc_thread_id": "ptc-1"})
            await core._exec_handoff_report_back(job)
        exec_rb.assert_awaited_once_with(job)


# ---------------------------------------------------------------------------
# Drainer execute: ack on success, nack on failure, unknown type nacked
# ---------------------------------------------------------------------------


def _job(hook_type="burst_release", payload=None, ordering_key=None):
    return {
        "hook_outbox_id": "job-1",
        "hook_type": hook_type,
        "payload": payload or {},
        "run_id": "run-1",
        "attempts": 1,
        "ordering_key": ordering_key,
    }


class TestDrainerExecute:
    @pytest.mark.asyncio
    async def test_success_acks(self):
        from src.server.database.runs import outbox as outbox_db

        drainer = HookOutboxDrainer()
        with patch.dict(
            hook_outbox._EXECUTORS, {"burst_release": AsyncMock()}
        ), patch.object(outbox_db, "ack_outbox_job", AsyncMock()) as ack, patch.object(
            outbox_db, "nack_outbox_job", AsyncMock()
        ) as nack:
            await drainer._execute(_job())
        # Fenced ack: attempts is the lease-generation token.
        ack.assert_awaited_once_with("job-1", attempts=1)
        nack.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_executor_failure_nacks(self):
        from src.server.database.runs import outbox as outbox_db

        drainer = HookOutboxDrainer()
        with patch.dict(
            hook_outbox._EXECUTORS,
            {"burst_release": AsyncMock(side_effect=RuntimeError("boom"))},
        ), patch.object(outbox_db, "ack_outbox_job", AsyncMock()) as ack, patch.object(
            outbox_db, "nack_outbox_job", AsyncMock(return_value="pending")
        ) as nack:
            await drainer._execute(_job())
        nack.assert_awaited_once_with(
            "job-1", attempts=1, max_attempts=hook_outbox.MAX_ATTEMPTS
        )
        ack.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_hook_type_nacks_toward_dead(self):
        from src.server.database.runs import outbox as outbox_db

        drainer = HookOutboxDrainer()
        with patch.object(outbox_db, "ack_outbox_job", AsyncMock()) as ack, patch.object(
            outbox_db, "nack_outbox_job", AsyncMock(return_value="dead")
        ) as nack:
            await drainer._execute(_job(hook_type="not_a_hook"))
        nack.assert_awaited_once()
        ack.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_steer_out_of_attempts_is_settled_not_dead_lettered(self):
        """The nack settles a steer at the ceiling per sender; the drainer
        runs its exhaust followup, never the dead-letter close."""
        from src.server.database.runs import outbox as outbox_db

        drainer = HookOutboxDrainer()
        job = _job(hook_type="steer", payload={"target": "task", "sender": "user"})
        closed, exhausted = AsyncMock(), AsyncMock()
        with patch.dict(
            hook_outbox._EXECUTORS, {"steer": AsyncMock(side_effect=RuntimeError("boom"))}
        ), patch.dict(hook_outbox._CLOSE_FOLLOWUPS, {"steer": closed}), patch.dict(
            hook_outbox._EXHAUST_FOLLOWUPS, {"steer": exhausted}
        ), patch.object(
            outbox_db, "nack_outbox_job", AsyncMock(return_value="exhausted")
        ), patch.object(hook_outbox.logger, "error") as error:
            await drainer._execute(job)
        exhausted.assert_awaited_once_with(job)
        closed.assert_not_awaited()
        error.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_market_alert_out_of_attempts_is_held_not_dead_lettered(self):
        """The nack holds an alert at the ceiling for its thread's next turn;
        the drainer logs it as settled, never as a dead letter."""
        from src.server.database.runs import outbox as outbox_db

        drainer = HookOutboxDrainer()
        job = {**_job(hook_type="market_target"), "attempts": hook_outbox.MAX_ATTEMPTS}
        with patch.dict(
            hook_outbox._EXECUTORS,
            {"market_target": AsyncMock(side_effect=RuntimeError("boom"))},
        ), patch.object(
            outbox_db, "nack_outbox_job", AsyncMock(return_value="exhausted")
        ), patch.object(hook_outbox.logger, "error") as error, patch.object(
            drainer, "_close_followup", AsyncMock()
        ) as closed:
            await drainer._execute(job)
        closed.assert_not_awaited()
        error.assert_not_called()

    def test_the_steer_executor_registers_its_exhaust_followup(self):
        from src.server.services.report_back import steer_executor

        steer_executor.register_outbox_executors()
        assert hook_outbox._EXHAUST_FOLLOWUPS["steer"] is steer_executor._on_exhausted

    # Dead-letter compensation is inserted ATOMICALLY inside
    # nack_outbox_job / park_exhausted_jobs (data-modifying CTE) — there is
    # deliberately no drainer-level compensation step to test; the SQL-level
    # contract is exercised by the live claim-gate verification script.


# ---------------------------------------------------------------------------
# Executor registry: handlers register at startup; the outbox never imports
# them. Collisions are wiring bugs; re-registering the same function (reload,
# repeated setup) is a no-op.
# ---------------------------------------------------------------------------


class TestExecutorRegistry:
    def test_same_function_is_idempotent(self):
        core.register_outbox_executors()
        core.register_outbox_executors()
        assert (
            hook_outbox._EXECUTORS["watch_clear"]
            is core._exec_watch_clear
        )

    def test_different_executor_for_registered_type_raises(self):
        async def rogue(job):
            pass

        with pytest.raises(ValueError, match="report_back"):
            hook_outbox.register_hook_executor("report_back", rogue)

    @pytest.mark.asyncio
    async def test_ack_followup_runs_after_successful_ack(self):
        """task_report_back's post-ack cleared wake: the watch is push-driven,
        so the reconcile signal must land AFTER the row closes."""
        from src.server.database.runs import outbox as outbox_db

        drainer = HookOutboxDrainer()
        job = _job(hook_type="task_report_back", payload={"thread_id": "t-1"})
        wake_cache = MagicMock()
        with patch.dict(
            hook_outbox._EXECUTORS, {"task_report_back": AsyncMock()}
        ), patch.object(
            outbox_db, "ack_outbox_job", AsyncMock()
        ) as ack, patch(
            f"{WAKE_MOD}.publish_wake", AsyncMock()
        ) as wake, patch(
            "src.utils.cache.redis_cache.get_cache_client",
            return_value=wake_cache,
        ):
            await drainer._execute(job)
        ack.assert_awaited_once()
        wake.assert_awaited_once_with(wake_cache, "t-1", cleared=True)

    @pytest.mark.asyncio
    async def test_ack_followup_failure_never_unacks(self):
        from src.server.database.runs import outbox as outbox_db

        drainer = HookOutboxDrainer()
        job = _job(hook_type="task_report_back", payload={"thread_id": "t-1"})
        with patch.dict(
            hook_outbox._EXECUTORS, {"task_report_back": AsyncMock()}
        ), patch.object(
            outbox_db, "ack_outbox_job", AsyncMock()
        ) as ack, patch.object(
            outbox_db, "nack_outbox_job", AsyncMock()
        ) as nack, patch.dict(
            hook_outbox._CLOSE_FOLLOWUPS,
            {"task_report_back": AsyncMock(side_effect=RuntimeError("boom"))},
        ):
            await drainer._execute(job)
        ack.assert_awaited_once()
        nack.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("nacked_to", "followed_up"), [("dead", True), ("pending", False)])
    async def test_close_followup_runs_when_a_job_dies(self, nacked_to, followed_up):
        """A dead row closes as surely as an acked one: the watch reading it
        as pending has no other signal until it recycles."""
        from src.server.database.runs import outbox as outbox_db

        drainer = HookOutboxDrainer()
        job = _job(hook_type="task_report_back", payload={"thread_id": "t-1"})
        followup = AsyncMock()
        with patch.dict(
            hook_outbox._EXECUTORS,
            {"task_report_back": AsyncMock(side_effect=RuntimeError("boom"))},
        ), patch.object(
            outbox_db, "nack_outbox_job", AsyncMock(return_value=nacked_to)
        ), patch.dict(
            hook_outbox._CLOSE_FOLLOWUPS, {"task_report_back": followup}
        ):
            await drainer._execute(job)
        assert followup.await_count == (1 if followed_up else 0)


# ---------------------------------------------------------------------------
# DALTON-HOOK(handoff-keep-on-resumable): Resumable worker_lost interruptions
# keep handoff state intact so the resumed run reports back normally.
# ---------------------------------------------------------------------------


def _awaitable_fn(fn):
    """Wrap an async method as a plain non-coroutine function returning a
    Future, matching redis.asyncio client methods that return awaitables
    without satisfying inspect.iscoroutinefunction."""
    import asyncio

    def _wrapper(*args, **kwargs):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        coro = fn(*args, **kwargs)
        asyncio.ensure_future(coro).add_done_callback(
            lambda task: (
                fut.set_exception(task.exception())
                if task.exception()
                else fut.set_result(task.result())
            )
        )
        return fut

    return _wrapper


# DALTON-HOOK(handoff-keep-on-resumable): test suite for resumable handoff retention
class TestHandoffKeepOnResumable:
    @staticmethod
    def _seeded_cache(
        *,
        dispatcher_tid: str = "cos-home",
        ptc_tid: str = "ptc-dash",
        user_id: str = "u-1",
        dispatch_gen: str = "gen-1",
    ):
        from src.server.services.report_back.handoff.keys import (
            ORIGIN_KIND,
            ORIGIN_THREAD_FIELD,
            ptc_origin_key,
            user_pending_key,
            watch_key,
        )
        from tests.unit.server.handlers.chat.redis_fakes import FakeCache

        cache = FakeCache()
        origin = {
            "origin": ORIGIN_KIND,
            "report_back": True,
            ORIGIN_THREAD_FIELD: dispatcher_tid,
            "user_id": user_id,
            "dispatch_gen": dispatch_gen,
            "ptc_workspace_id": "ws-dash",
        }
        cache.kv[ptc_origin_key(ptc_tid)] = origin
        cache.client.sets[watch_key(dispatcher_tid)] = {ptc_tid}
        cache.client.sets[user_pending_key(user_id)] = {ptc_tid}
        cache.client.sismember = _awaitable_fn(cache.client.sismember)
        return cache

    # DALTON-HOOK(handoff-keep-on-resumable): normal delivery path across worker_lost -> resume
    @pytest.mark.asyncio
    async def test_worker_lost_normal_delivery_resumed_completes_before_old_watch_clear_executes(
        self,
    ):
        from contextlib import asynccontextmanager
        from unittest.mock import create_autospec

        from src.server.database.runs import lifecycle as tl_db
        from src.server.database.runs import outbox as outbox_db
        from src.server.services.report_back import notify_turn
        from src.server.services.report_back.handoff import executor
        from src.server.services.report_back.handoff.keys import (
            ptc_origin_key,
            watch_key,
        )
        from src.utils.cache import redis_cache

        cache = self._seeded_cache()
        runs_by_id = {
            "ptc-run-0": {
                "conversation_response_id": "ptc-run-0",
                "status": "error",
                "errors": [
                    "worker_lost: no live executor holds this run's writer fence"
                ],
            },
            "ptc-run-1": {
                "conversation_response_id": "ptc-run-1",
                "status": "completed",
                "errors": [],
            },
        }
        analyst_latest = dict(runs_by_id["ptc-run-0"])

        async def _fake_get_run(run_id: str):
            return dict(runs_by_id[run_id]) if run_id in runs_by_id else None

        async def _fake_latest_attempt(thread_id: str):
            if thread_id == "ptc-dash":
                return dict(analyst_latest)
            return {"conversation_response_id": "cos-idle", "status": "completed"}

        # DALTON-HOOK(handoff-keep-on-resumable): autospec test doubles
        get_cache_mock = create_autospec(
            redis_cache.get_cache_client, return_value=cache
        )
        get_run_mock = create_autospec(tl_db.get_run, side_effect=_fake_get_run)
        get_latest_mock = create_autospec(
            tl_db.get_latest_attempt, side_effect=_fake_latest_attempt
        )
        post_calls: list[dict] = []

        async def _fake_post_turn(**kwargs):
            post_calls.append(kwargs)
            idx = len(post_calls) - 1
            return ("dispatched", f"rb-run-{idx}")

        post_mock = create_autospec(
            notify_turn.post_notification_turn, side_effect=_fake_post_turn
        )
        await_terminal_mock = create_autospec(
            notify_turn.await_run_terminal, return_value="terminal"
        )
        extend_mock = create_autospec(
            outbox_db.extend_job_lease, return_value=True
        )
        merge_mock = create_autospec(
            outbox_db.merge_job_payload, return_value=True
        )
        has_live_mock = create_autospec(
            outbox_db.has_live_hand_off, return_value=False
        )
        find_by_key_mock = create_autospec(
            tl_db.find_run_by_request_key, return_value=None
        )
        ws_name_mock = create_autospec(
            executor._workspace_name, return_value="DASH"
        )

        @asynccontextmanager
        async def _owned_guard(job_id: str, attempts: int, *, lease_seconds: int = 60):
            yield True

        guard_mock = create_autospec(
            outbox_db.fenced_job_guard, side_effect=_owned_guard
        )

        # DALTON-HOOK(handoff-keep-on-resumable): round 0 resumable failure report-back
        with (
            patch.object(outbox_db, "fenced_job_guard", guard_mock),
            patch.object(redis_cache, "get_cache_client", get_cache_mock),
            patch.object(tl_db, "get_run", get_run_mock),
            patch.object(tl_db, "get_latest_attempt", get_latest_mock),
            patch.object(tl_db, "find_run_by_request_key", find_by_key_mock),
            patch.object(notify_turn, "post_notification_turn", post_mock),
            patch.object(notify_turn, "await_run_terminal", await_terminal_mock),
            patch.object(outbox_db, "extend_job_lease", extend_mock),
            patch.object(outbox_db, "merge_job_payload", merge_mock),
            patch.object(outbox_db, "has_live_hand_off", has_live_mock),
            patch.object(executor, "_workspace_name", ws_name_mock),
        ):
            job_0 = {
                "hook_outbox_id": "job-err-0",
                "hook_type": "report_back",
                "run_id": "ptc-run-0",
                "attempts": 1,
                "ordering_key": "cos-home",
                "payload": {
                    "ptc_thread_id": "ptc-dash",
                    "dispatch_gen": "gen-1",
                    "final_status": "error",
                },
            }
            await core._exec_handoff_report_back(job_0)

            assert len(post_calls) == 1
            msg_0 = post_calls[0]["body"]["messages"][0]["content"]
            assert "failed before it finished" in msg_0
            assert "运行被中断，系统会续跑，完成后会再回报" in msg_0
            origin_after_r0 = cache.kv[ptc_origin_key("ptc-dash")]
            assert origin_after_r0["awaiting_resume"]["analyst_run_id"] == "ptc-run-0"
            assert origin_after_r0["awaiting_resume"]["dispatch_gen"] == "gen-1"
            assert isinstance(origin_after_r0["awaiting_resume"]["marked_at"], float)

            # DALTON-HOOK(handoff-keep-on-resumable): resumed run completes BEFORE
            # the unmarked watch_clear from round 0 executes.
            analyst_latest.update(runs_by_id["ptc-run-1"])
            latest_calls_before_clear = get_latest_mock.await_count

            await core._exec_watch_clear(
                {
                    "hook_outbox_id": "job-clear-0",
                    "hook_type": "watch_clear",
                    "run_id": "rb-run-0",
                    "attempts": 1,
                    "ordering_key": "cos-home",
                    "payload": {
                        "ptc_thread_id": "ptc-dash",
                        "user_id": "u-1",
                        "error_wake": False,
                        "dispatch_gen": "gen-1",
                    },
                }
            )
            assert get_latest_mock.await_count == latest_calls_before_clear
            assert "ptc-dash" in cache.client.sets.get(watch_key("cos-home"), set())
            assert ptc_origin_key("ptc-dash") in cache.kv

            # Round 1: resumed analyst run's report_back revokes awaiting_resume and wakes Home.
            job_1 = {
                "hook_outbox_id": "job-ok-1",
                "hook_type": "report_back",
                "run_id": "ptc-run-1",
                "attempts": 1,
                "ordering_key": "cos-home",
                "payload": {
                    "ptc_thread_id": "ptc-dash",
                    "dispatch_gen": "gen-1",
                    "final_status": "completed",
                },
            }
            await core._exec_handoff_report_back(job_1)

            assert "awaiting_resume" not in cache.kv[ptc_origin_key("ptc-dash")]
            assert len(post_calls) == 2
            msg_1 = post_calls[1]["body"]["messages"][0]["content"]
            assert "has completed." in msg_1
            assert "运行被中断" not in msg_1
            assert any(
                '"run_id": "rb-run-1"' in payload_str
                for _, payload_str in cache.client.published
            )

            # Final watch_clear after the completion summary turn clears the pair.
            await core._exec_watch_clear(
                {
                    "hook_outbox_id": "job-clear-1",
                    "hook_type": "watch_clear",
                    "run_id": "rb-run-1",
                    "attempts": 1,
                    "ordering_key": "cos-home",
                    "payload": {
                        "ptc_thread_id": "ptc-dash",
                        "user_id": "u-1",
                        "error_wake": False,
                        "dispatch_gen": "gen-1",
                    },
                }
            )
            assert "ptc-dash" not in cache.client.sets.get(
                watch_key("cos-home"), set()
            )
            assert ptc_origin_key("ptc-dash") not in cache.kv

    # DALTON-HOOK(handoff-keep-on-resumable): awaiting_resume is stamped on ptc_origin
    # BEFORE POST, so finalize watch_clear in any order and post-POST retry preserve the pair
    @pytest.mark.asyncio
    @pytest.mark.parametrize("finalize_during_post", [True, False])
    async def test_awaiting_resume_stamped_before_post_and_survives_post_retry(
        self, finalize_during_post: bool
    ):
        from contextlib import asynccontextmanager
        from unittest.mock import create_autospec

        from src.server.database.runs import lifecycle as tl_db
        from src.server.database.runs import outbox as outbox_db
        from src.server.services.report_back import notify_turn
        from src.server.services.report_back.handoff import executor, pointer
        from src.server.services.report_back.handoff.keys import (
            ptc_origin_key,
            watch_key,
        )
        from src.utils.cache import redis_cache

        cache = self._seeded_cache()
        runs_by_id = {
            "ptc-run-0": {
                "conversation_response_id": "ptc-run-0",
                "status": "error",
                "errors": [
                    "worker_lost: no live executor holds this run's writer fence"
                ],
            },
            "ptc-run-1": {
                "conversation_response_id": "ptc-run-1",
                "status": "completed",
                "errors": [],
            },
        }
        call_order: list[str] = []
        post_count = 0

        async def _fake_post_turn(**kwargs):
            nonlocal post_count
            post_count += 1
            # Verify awaiting_resume is ALREADY on ptc_origin at the moment POST runs.
            origin_during_post = cache.kv[ptc_origin_key("ptc-dash")]
            if post_count == 1:
                assert (
                    origin_during_post["awaiting_resume"]["analyst_run_id"]
                    == "ptc-run-0"
                )
                call_order.append("post_with_awaiting_resume_already_set")
                if finalize_during_post:
                    # Summary finalize enqueues and even executes watch_clear before
                    # report_back finishes its post-POST steps.
                    await core._exec_watch_clear(
                        {
                            "hook_outbox_id": "job-clear-early",
                            "hook_type": "watch_clear",
                            "run_id": "rb-run-0",
                            "attempts": 1,
                            "ordering_key": "cos-home",
                            "payload": {
                                "ptc_thread_id": "ptc-dash",
                                "user_id": "u-1",
                                "error_wake": False,
                                "dispatch_gen": "gen-1",
                            },
                        }
                    )
                return ("dispatched", "rb-run-0")
            assert "awaiting_resume" not in origin_during_post
            call_order.append("post_after_awaiting_resume_revoked")
            return ("dispatched", "rb-run-1")

        # DALTON-HOOK(handoff-keep-on-resumable): simulate transient failure on first
        # await_run_terminal so job retries via dispatched_run_id
        terminal_calls = 0

        async def _flaky_await_terminal(*args, **kwargs):
            nonlocal terminal_calls
            terminal_calls += 1
            if terminal_calls == 1:
                raise RuntimeError("transient error after POST")
            return "terminal"

        get_cache_mock = create_autospec(
            redis_cache.get_cache_client, return_value=cache
        )
        get_run_mock = create_autospec(
            tl_db.get_run,
            side_effect=lambda run_id: dict(runs_by_id[run_id]),
        )
        get_latest_mock = create_autospec(
            tl_db.get_latest_attempt,
            return_value=dict(runs_by_id["ptc-run-0"]),
        )
        find_by_key_mock = create_autospec(
            tl_db.find_run_by_request_key, return_value=None
        )
        post_mock = create_autospec(
            notify_turn.post_notification_turn, side_effect=_fake_post_turn
        )
        await_terminal_mock = create_autospec(
            notify_turn.await_run_terminal, side_effect=_flaky_await_terminal
        )
        extend_mock = create_autospec(
            outbox_db.extend_job_lease, return_value=True
        )
        merge_mock = create_autospec(
            outbox_db.merge_job_payload, return_value=True
        )
        has_live_mock = create_autospec(
            outbox_db.has_live_hand_off, return_value=False
        )
        ws_name_mock = create_autospec(
            executor._workspace_name, return_value="DASH"
        )
        ack_mock = create_autospec(outbox_db.ack_outbox_job, return_value=True)
        nack_mock = create_autospec(
            outbox_db.nack_outbox_job, return_value="pending"
        )

        @asynccontextmanager
        async def _owned_guard(job_id: str, attempts: int, *, lease_seconds: int = 60):
            yield True

        guard_mock = create_autospec(
            outbox_db.fenced_job_guard, side_effect=_owned_guard
        )

        # DALTON-HOOK(handoff-keep-on-resumable): verify retry + watch_clear + resumed run
        drainer = HookOutboxDrainer()
        job_0 = {
            "hook_outbox_id": "job-err-0",
            "hook_type": "report_back",
            "run_id": "ptc-run-0",
            "attempts": 1,
            "ordering_key": "cos-home",
            "payload": {
                "ptc_thread_id": "ptc-dash",
                "dispatch_gen": "gen-1",
                "final_status": "error",
            },
        }
        with (
            patch.object(outbox_db, "fenced_job_guard", guard_mock),
            patch.object(redis_cache, "get_cache_client", get_cache_mock),
            patch.object(tl_db, "get_run", get_run_mock),
            patch.object(tl_db, "get_latest_attempt", get_latest_mock),
            patch.object(tl_db, "find_run_by_request_key", find_by_key_mock),
            patch.object(notify_turn, "post_notification_turn", post_mock),
            patch.object(notify_turn, "await_run_terminal", await_terminal_mock),
            patch.object(outbox_db, "extend_job_lease", extend_mock),
            patch.object(outbox_db, "merge_job_payload", merge_mock),
            patch.object(outbox_db, "has_live_hand_off", has_live_mock),
            patch.object(executor, "_workspace_name", ws_name_mock),
            patch.object(outbox_db, "ack_outbox_job", ack_mock),
            patch.object(outbox_db, "nack_outbox_job", nack_mock),
        ):
            await drainer._execute(job_0)
            nack_mock.assert_awaited_once()
            job_0["attempts"] = 2
            await drainer._execute(job_0)
            ack_mock.assert_awaited_once()

            if not finalize_during_post:
                await core._exec_watch_clear(
                    {
                        "hook_outbox_id": "job-clear-late",
                        "hook_type": "watch_clear",
                        "run_id": "rb-run-0",
                        "attempts": 1,
                        "ordering_key": "cos-home",
                        "payload": {
                            "ptc_thread_id": "ptc-dash",
                            "user_id": "u-1",
                            "error_wake": False,
                            "dispatch_gen": "gen-1",
                        },
                    }
                )
            assert "ptc-dash" in cache.client.sets.get(watch_key("cos-home"), set())
            assert (
                cache.kv[ptc_origin_key("ptc-dash")][pointer.AWAITING_RESUME_FIELD][
                    "analyst_run_id"
                ]
                == "ptc-run-0"
            )

            # Resumed run completes -> revokes awaiting_resume -> final clear succeeds
            await core._exec_handoff_report_back(
                {
                    "hook_outbox_id": "job-ok-1",
                    "hook_type": "report_back",
                    "run_id": "ptc-run-1",
                    "attempts": 1,
                    "ordering_key": "cos-home",
                    "payload": {
                        "ptc_thread_id": "ptc-dash",
                        "dispatch_gen": "gen-1",
                        "final_status": "completed",
                    },
                }
            )
            assert call_order == [
                "post_with_awaiting_resume_already_set",
                "post_after_awaiting_resume_revoked",
            ]
            await core._exec_watch_clear(
                {
                    "hook_outbox_id": "job-clear-1",
                    "hook_type": "watch_clear",
                    "run_id": "rb-run-1",
                    "attempts": 1,
                    "ordering_key": "cos-home",
                    "payload": {
                        "ptc_thread_id": "ptc-dash",
                        "user_id": "u-1",
                        "error_wake": False,
                        "dispatch_gen": "gen-1",
                    },
                }
            )
            assert "ptc-dash" not in cache.client.sets.get(
                watch_key("cos-home"), set()
            )
            assert ptc_origin_key("ptc-dash") not in cache.kv

    # DALTON-HOOK(handoff-keep-on-resumable): Codex #1 — POST succeeds, finalize inserts
    # unmarked {rb_run_id}:watch_clear, report_back exhausts retries to dead_clear, and
    # old unmarked watch_clear executes later: both keep pair until resumed run finishes
    @pytest.mark.asyncio
    async def test_dead_report_back_and_late_unmarked_watch_clear_both_preserve_handoff(
        self,
    ):
        from contextlib import asynccontextmanager
        from unittest.mock import create_autospec

        from src.server.database.runs import lifecycle as tl_db
        from src.server.database.runs import outbox as outbox_db
        from src.server.services.report_back import notify_turn
        from src.server.services.report_back.handoff import executor
        from src.server.services.report_back.handoff.keys import (
            ptc_origin_key,
            watch_key,
        )
        from src.utils.cache import redis_cache

        cache = self._seeded_cache()
        runs_by_id = {
            "ptc-run-0": {
                "conversation_response_id": "ptc-run-0",
                "status": "error",
                "errors": [
                    "worker_lost: no live executor holds this run's writer fence"
                ],
            },
            "ptc-run-1": {
                "conversation_response_id": "ptc-run-1",
                "status": "completed",
                "errors": [],
            },
        }
        get_cache_mock = create_autospec(
            redis_cache.get_cache_client, return_value=cache
        )
        get_run_mock = create_autospec(
            tl_db.get_run,
            side_effect=lambda run_id: dict(runs_by_id[run_id]),
        )
        get_latest_mock = create_autospec(
            tl_db.get_latest_attempt,
            return_value=dict(runs_by_id["ptc-run-1"]),
        )
        find_by_key_mock = create_autospec(
            tl_db.find_run_by_request_key, return_value=None
        )
        post_calls: list[dict] = []

        async def _fake_post_turn(**kwargs):
            post_calls.append(kwargs)
            return ("dispatched", f"rb-run-{len(post_calls) - 1}")

        post_mock = create_autospec(
            notify_turn.post_notification_turn, side_effect=_fake_post_turn
        )
        # DALTON-HOOK(handoff-keep-on-resumable): round 0 fails after POST at attempt 5 -> dead
        terminal_attempts = 0

        async def _await_terminal(*args, **kwargs):
            nonlocal terminal_attempts
            terminal_attempts += 1
            if terminal_attempts == 1:
                raise RuntimeError("worker failed after POST at max attempts")
            return "terminal"

        await_terminal_mock = create_autospec(
            notify_turn.await_run_terminal, side_effect=_await_terminal
        )
        extend_mock = create_autospec(
            outbox_db.extend_job_lease, return_value=True
        )
        merge_mock = create_autospec(
            outbox_db.merge_job_payload, return_value=True
        )
        has_live_mock = create_autospec(
            outbox_db.has_live_hand_off, return_value=False
        )
        ws_name_mock = create_autospec(
            executor._workspace_name, return_value="DASH"
        )
        ack_mock = create_autospec(outbox_db.ack_outbox_job, return_value=True)
        nack_mock = create_autospec(
            outbox_db.nack_outbox_job, return_value="dead"
        )

        @asynccontextmanager
        async def _owned_guard(job_id: str, attempts: int, *, lease_seconds: int = 60):
            yield True

        guard_mock = create_autospec(
            outbox_db.fenced_job_guard, side_effect=_owned_guard
        )

        drainer = HookOutboxDrainer()
        job_dead = {
            "hook_outbox_id": "job-err-dead",
            "hook_type": "report_back",
            "run_id": "ptc-run-0",
            "attempts": 5,
            "ordering_key": "cos-home",
            "payload": {
                "ptc_thread_id": "ptc-dash",
                "dispatch_gen": "gen-1",
                "final_status": "error",
            },
        }
        # DALTON-HOOK(handoff-keep-on-resumable): execute dead_clear then unmarked watch_clear
        with (
            patch.object(outbox_db, "fenced_job_guard", guard_mock),
            patch.object(redis_cache, "get_cache_client", get_cache_mock),
            patch.object(tl_db, "get_run", get_run_mock),
            patch.object(tl_db, "get_latest_attempt", get_latest_mock),
            patch.object(tl_db, "find_run_by_request_key", find_by_key_mock),
            patch.object(notify_turn, "post_notification_turn", post_mock),
            patch.object(notify_turn, "await_run_terminal", await_terminal_mock),
            patch.object(outbox_db, "extend_job_lease", extend_mock),
            patch.object(outbox_db, "merge_job_payload", merge_mock),
            patch.object(outbox_db, "has_live_hand_off", has_live_mock),
            patch.object(executor, "_workspace_name", ws_name_mock),
            patch.object(outbox_db, "ack_outbox_job", ack_mock),
            patch.object(outbox_db, "nack_outbox_job", nack_mock),
        ):
            await drainer._execute(job_dead)
            nack_mock.assert_awaited_once()
            assert (
                cache.kv[ptc_origin_key("ptc-dash")]["awaiting_resume"][
                    "analyst_run_id"
                ]
                == "ptc-run-0"
            )

            # 1) {job_id}:dead_clear executes (unmarked payload from SQL CTE)
            await core._exec_watch_clear(
                {
                    "hook_outbox_id": "job-err-dead:dead_clear",
                    "hook_type": "watch_clear",
                    "run_id": "ptc-run-0",
                    "attempts": 1,
                    "ordering_key": "cos-home",
                    "payload": {
                        "ptc_thread_id": "ptc-dash",
                        "error_wake": True,
                        "dispatch_gen": "gen-1",
                        "refuse_if_pointer": True,
                    },
                }
            )
            assert "ptc-dash" in cache.client.sets.get(watch_key("cos-home"), set())
            assert ptc_origin_key("ptc-dash") in cache.kv

            # 2) Unmarked {rb_run_id}:watch_clear from summary finalize executes later
            await core._exec_watch_clear(
                {
                    "hook_outbox_id": "rb-run-0:watch_clear",
                    "hook_type": "watch_clear",
                    "run_id": "rb-run-0",
                    "attempts": 1,
                    "ordering_key": "cos-home",
                    "payload": {
                        "ptc_thread_id": "ptc-dash",
                        "user_id": "u-1",
                        "error_wake": False,
                        "dispatch_gen": "gen-1",
                    },
                }
            )
            assert "ptc-dash" in cache.client.sets.get(watch_key("cos-home"), set())
            assert ptc_origin_key("ptc-dash") in cache.kv

            # 3) Resumed run ptc-run-1 reports back, revokes awaiting_resume, and clears
            await core._exec_handoff_report_back(
                {
                    "hook_outbox_id": "job-ok-1",
                    "hook_type": "report_back",
                    "run_id": "ptc-run-1",
                    "attempts": 1,
                    "ordering_key": "cos-home",
                    "payload": {
                        "ptc_thread_id": "ptc-dash",
                        "dispatch_gen": "gen-1",
                        "final_status": "completed",
                    },
                }
            )
            assert len(post_calls) == 2
            await core._exec_watch_clear(
                {
                    "hook_outbox_id": "rb-run-1:watch_clear",
                    "hook_type": "watch_clear",
                    "run_id": "rb-run-1",
                    "attempts": 1,
                    "ordering_key": "cos-home",
                    "payload": {
                        "ptc_thread_id": "ptc-dash",
                        "user_id": "u-1",
                        "error_wake": False,
                        "dispatch_gen": "gen-1",
                    },
                }
            )
            assert "ptc-dash" not in cache.client.sets.get(
                watch_key("cos-home"), set()
            )
            assert ptc_origin_key("ptc-dash") not in cache.kv

    # DALTON-HOOK(handoff-keep-on-resumable): Codex #2 — summary terminal-wait timeout
    # (_await_run_terminal) and drop/cap skip clearing while awaiting_resume is active
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["timeout", "cap"])
    async def test_summary_timeout_and_drop_cap_preserve_handoff_until_resumed_run(
        self, mode: str
    ):
        from contextlib import asynccontextmanager
        from unittest.mock import create_autospec

        from src.server.database.runs import lifecycle as tl_db
        from src.server.database.runs import outbox as outbox_db
        from src.server.services.report_back import notify_turn
        from src.server.services.report_back.handoff import executor
        from src.server.services.report_back.handoff.keys import (
            PTC_ORIGIN_TTL,
            ptc_origin_key,
            rb_done_key,
            rb_run_key,
            watch_key,
        )
        from src.utils.cache import redis_cache

        cache = self._seeded_cache()
        runs_by_id = {
            "ptc-run-0": {
                "conversation_response_id": "ptc-run-0",
                "status": "error",
                "errors": [
                    "worker_lost: no live executor holds this run's writer fence"
                ],
            },
            "ptc-run-1": {
                "conversation_response_id": "ptc-run-1",
                "status": "completed",
                "errors": [],
            },
        }
        get_cache_mock = create_autospec(
            redis_cache.get_cache_client, return_value=cache
        )
        get_run_mock = create_autospec(
            tl_db.get_run,
            side_effect=lambda run_id: dict(runs_by_id[run_id]),
        )
        get_latest_mock = create_autospec(
            tl_db.get_latest_attempt,
            return_value=dict(runs_by_id["ptc-run-0"]),
        )
        find_by_key_mock = create_autospec(
            tl_db.find_run_by_request_key, return_value=None
        )
        post_calls: list[dict] = []

        async def _fake_post_turn(**kwargs):
            post_calls.append(kwargs)
            if len(post_calls) == 1 and mode == "cap":
                return ("cap", None)
            return ("dispatched", f"rb-run-{len(post_calls) - 1}")

        post_mock = create_autospec(
            notify_turn.post_notification_turn, side_effect=_fake_post_turn
        )
        # DALTON-HOOK(handoff-keep-on-resumable): first summary wait times out; second succeeds
        terminal_outcomes = iter(["timeout", "terminal"])
        await_terminal_mock = create_autospec(
            notify_turn.await_run_terminal,
            side_effect=lambda *args, **kwargs: next(terminal_outcomes),
        )
        extend_mock = create_autospec(
            outbox_db.extend_job_lease, return_value=True
        )
        merge_mock = create_autospec(
            outbox_db.merge_job_payload, return_value=True
        )
        has_live_mock = create_autospec(
            outbox_db.has_live_hand_off, return_value=False
        )
        ws_name_mock = create_autospec(
            executor._workspace_name, return_value="DASH"
        )

        @asynccontextmanager
        async def _owned_guard(job_id: str, attempts: int, *, lease_seconds: int = 60):
            yield True

        guard_mock = create_autospec(
            outbox_db.fenced_job_guard, side_effect=_owned_guard
        )

        with (
            patch.object(outbox_db, "fenced_job_guard", guard_mock),
            patch.object(redis_cache, "get_cache_client", get_cache_mock),
            patch.object(tl_db, "get_run", get_run_mock),
            patch.object(tl_db, "get_latest_attempt", get_latest_mock),
            patch.object(tl_db, "find_run_by_request_key", find_by_key_mock),
            patch.object(notify_turn, "post_notification_turn", post_mock),
            patch.object(notify_turn, "await_run_terminal", await_terminal_mock),
            patch.object(outbox_db, "extend_job_lease", extend_mock),
            patch.object(outbox_db, "merge_job_payload", merge_mock),
            patch.object(outbox_db, "has_live_hand_off", has_live_mock),
            patch.object(executor, "_workspace_name", ws_name_mock),
        ):
            await core._exec_handoff_report_back(
                {
                    "hook_outbox_id": "job-err-timeout",
                    "hook_type": "report_back",
                    "run_id": "ptc-run-0",
                    "attempts": 1,
                    "ordering_key": "cos-home",
                    "payload": {
                        "ptc_thread_id": "ptc-dash",
                        "dispatch_gen": "gen-1",
                        "final_status": "error",
                    },
                }
            )
            # Neither _await_run_terminal timeout nor drop/cap cleared the pair!
            assert "ptc-dash" in cache.client.sets.get(watch_key("cos-home"), set())
            assert ptc_origin_key("ptc-dash") in cache.kv
            assert cache.client.ttls[ptc_origin_key("ptc-dash")] == PTC_ORIGIN_TTL
            if mode == "timeout":
                # DALTON-HOOK(handoff-keep-on-resumable): timeout does NOT release
                # the still-running summary turn's pointer or record it as drained;
                # only the summary turn's own watch_clear releases it.
                assert (
                    cache.kv[rb_run_key("cos-home", "ptc-dash")]["run_id"]
                    == "rb-run-0"
                )
                assert not cache.client.lists.get(rb_done_key("cos-home"))
                await core._exec_watch_clear(
                    {
                        "hook_outbox_id": "rb-run-0:watch_clear",
                        "hook_type": "watch_clear",
                        "run_id": "rb-run-0",
                        "attempts": 1,
                        "ordering_key": "cos-home",
                        "payload": {
                            "ptc_thread_id": "ptc-dash",
                            "user_id": "u-1",
                            "error_wake": False,
                            "dispatch_gen": "gen-1",
                        },
                    }
                )
                assert rb_run_key("cos-home", "ptc-dash") not in cache.kv
                assert cache.client.lists.get(rb_done_key("cos-home")) == [
                    "rb-run-0"
                ]
                assert "ptc-dash" in cache.client.sets.get(
                    watch_key("cos-home"), set()
                )
                assert ptc_origin_key("ptc-dash") in cache.kv

            # Resumed run completes -> revokes awaiting_resume -> final clear succeeds
            await core._exec_handoff_report_back(
                {
                    "hook_outbox_id": "job-ok-after-timeout",
                    "hook_type": "report_back",
                    "run_id": "ptc-run-1",
                    "attempts": 1,
                    "ordering_key": "cos-home",
                    "payload": {
                        "ptc_thread_id": "ptc-dash",
                        "dispatch_gen": "gen-1",
                        "final_status": "completed",
                    },
                }
            )
            await core._exec_watch_clear(
                {
                    "hook_outbox_id": "job-clear-after-timeout",
                    "hook_type": "watch_clear",
                    "run_id": "rb-run-1",
                    "attempts": 1,
                    "ordering_key": "cos-home",
                    "payload": {
                        "ptc_thread_id": "ptc-dash",
                        "user_id": "u-1",
                        "error_wake": False,
                        "dispatch_gen": "gen-1",
                    },
                }
            )
            assert "ptc-dash" not in cache.client.sets.get(
                watch_key("cos-home"), set()
            )
            assert ptc_origin_key("ptc-dash") not in cache.kv

    # DALTON-HOOK(handoff-keep-on-resumable): awaiting_resume expires after 24h (PTC_ORIGIN_TTL)
    @pytest.mark.asyncio
    async def test_expired_awaiting_resume_marker_clears_handoff_as_usual(self):
        import time
        from contextlib import asynccontextmanager
        from unittest.mock import create_autospec

        from src.server.database.runs import lifecycle as tl_db
        from src.server.database.runs import outbox as outbox_db
        from src.server.services.report_back.handoff import pointer
        from src.server.services.report_back.handoff.keys import (
            PTC_ORIGIN_TTL,
            ptc_origin_key,
            watch_key,
        )
        from src.utils.cache import redis_cache

        cache = self._seeded_cache()
        await pointer.mark_awaiting_resume(
            cache,
            "ptc-dash",
            analyst_run_id="ptc-run-0",
            expected_gen="gen-1",
            marked_at=time.time() - PTC_ORIGIN_TTL - 10,
        )
        get_cache_mock = create_autospec(
            redis_cache.get_cache_client, return_value=cache
        )
        get_run_mock = create_autospec(
            tl_db.get_run,
            return_value={
                "conversation_response_id": "ptc-run-0",
                "status": "error",
                "errors": ["worker_lost"],
            },
        )

        @asynccontextmanager
        async def _owned_guard(job_id: str, attempts: int, *, lease_seconds: int = 60):
            yield True

        guard_mock = create_autospec(
            outbox_db.fenced_job_guard, side_effect=_owned_guard
        )
        with (
            patch.object(outbox_db, "fenced_job_guard", guard_mock),
            patch.object(redis_cache, "get_cache_client", get_cache_mock),
            patch.object(tl_db, "get_run", get_run_mock),
        ):
            await core._exec_watch_clear(
                {
                    "hook_outbox_id": "job-clear-expired",
                    "hook_type": "watch_clear",
                    "run_id": "rb-run-0",
                    "attempts": 1,
                    "ordering_key": "cos-home",
                    "payload": {
                        "ptc_thread_id": "ptc-dash",
                        "user_id": "u-1",
                        "error_wake": False,
                        "dispatch_gen": "gen-1",
                    },
                }
            )
        get_run_mock.assert_not_awaited()
        assert "ptc-dash" not in cache.client.sets.get(watch_key("cos-home"), set())
        assert ptc_origin_key("ptc-dash") not in cache.kv

    # DALTON-HOOK(handoff-keep-on-resumable): non-resumable failure + user retry
    # delivered via real _exec_handoff_report_back writes no awaiting_resume and clears as usual
    @pytest.mark.asyncio
    async def test_non_resumable_failure_plus_user_retry_clears_handoff_as_usual(
        self,
    ):
        from contextlib import asynccontextmanager
        from unittest.mock import create_autospec

        from src.server.database.runs import lifecycle as tl_db
        from src.server.database.runs import outbox as outbox_db
        from src.server.services.report_back import notify_turn
        from src.server.services.report_back.handoff import executor
        from src.server.services.report_back.handoff.keys import (
            ptc_origin_key,
            watch_key,
        )
        from src.utils.cache import redis_cache

        cache = self._seeded_cache()
        runs_by_id = {
            "ptc-run-err": {
                "conversation_response_id": "ptc-run-err",
                "status": "error",
                "errors": ["ValueError: invalid ticker symbol"],
            },
            "ptc-run-user-retry": {
                "conversation_response_id": "ptc-run-user-retry",
                "status": "in_progress",
                "retry_of_run_id": "ptc-run-err",
                "errors": [],
            },
        }
        get_run_mock = create_autospec(
            tl_db.get_run,
            side_effect=lambda run_id: dict(runs_by_id[run_id]),
        )
        get_latest_mock = create_autospec(
            tl_db.get_latest_attempt,
            return_value=dict(runs_by_id["ptc-run-err"]),
        )
        find_by_key_mock = create_autospec(
            tl_db.find_run_by_request_key, return_value=None
        )
        post_calls: list[dict] = []

        async def _fake_post_turn(**kwargs):
            post_calls.append(kwargs)
            return ("dispatched", "rb-run-err")

        post_mock = create_autospec(
            notify_turn.post_notification_turn, side_effect=_fake_post_turn
        )
        await_terminal_mock = create_autospec(
            notify_turn.await_run_terminal, return_value="terminal"
        )
        extend_mock = create_autospec(
            outbox_db.extend_job_lease, return_value=True
        )
        merge_mock = create_autospec(
            outbox_db.merge_job_payload, return_value=True
        )
        has_live_mock = create_autospec(
            outbox_db.has_live_hand_off, return_value=False
        )
        ws_name_mock = create_autospec(
            executor._workspace_name, return_value="DASH"
        )
        get_cache_mock = create_autospec(
            redis_cache.get_cache_client, return_value=cache
        )

        @asynccontextmanager
        async def _owned_guard(job_id: str, attempts: int, *, lease_seconds: int = 60):
            yield True

        guard_mock = create_autospec(
            outbox_db.fenced_job_guard, side_effect=_owned_guard
        )
        # DALTON-HOOK(handoff-keep-on-resumable): deliver ordinary failure through
        # _exec_handoff_report_back, assert awaiting_resume is not written, then clear
        with (
            patch.object(outbox_db, "fenced_job_guard", guard_mock),
            patch.object(redis_cache, "get_cache_client", get_cache_mock),
            patch.object(tl_db, "get_run", get_run_mock),
            patch.object(tl_db, "get_latest_attempt", get_latest_mock),
            patch.object(tl_db, "find_run_by_request_key", find_by_key_mock),
            patch.object(notify_turn, "post_notification_turn", post_mock),
            patch.object(notify_turn, "await_run_terminal", await_terminal_mock),
            patch.object(outbox_db, "extend_job_lease", extend_mock),
            patch.object(outbox_db, "merge_job_payload", merge_mock),
            patch.object(outbox_db, "has_live_hand_off", has_live_mock),
            patch.object(executor, "_workspace_name", ws_name_mock),
        ):
            job_err = {
                "hook_outbox_id": "job-err-nonresumable",
                "hook_type": "report_back",
                "run_id": "ptc-run-err",
                "attempts": 1,
                "ordering_key": "cos-home",
                "payload": {
                    "ptc_thread_id": "ptc-dash",
                    "dispatch_gen": "gen-1",
                    "final_status": "error",
                },
            }
            await core._exec_handoff_report_back(job_err)

            assert len(post_calls) == 1
            msg = post_calls[0]["body"]["messages"][0]["content"]
            assert "failed before it finished" in msg
            assert "运行被中断" not in msg
            assert "awaiting_resume" not in cache.kv[ptc_origin_key("ptc-dash")]

            await core._exec_watch_clear(
                {
                    "hook_outbox_id": "job-clear-err",
                    "hook_type": "watch_clear",
                    "run_id": "rb-run-err",
                    "attempts": 1,
                    "ordering_key": "cos-home",
                    "payload": {
                        "ptc_thread_id": "ptc-dash",
                        "user_id": "u-1",
                        "error_wake": False,
                        "dispatch_gen": "gen-1",
                    },
                }
            )
        assert "ptc-dash" not in cache.client.sets.get(watch_key("cos-home"), set())
        assert ptc_origin_key("ptc-dash") not in cache.kv

    # DALTON-HOOK(handoff-keep-on-resumable): cancelled / user stop clears as usual
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "run_row",
        [
            {
                "conversation_response_id": "ptc-run-cancel",
                "status": "cancelled",
                "errors": ["worker_lost"],
            },
            {
                "conversation_response_id": "ptc-run-user-stop",
                "status": "error",
                "errors": ["worker_lost"],
                "cancel_requested_at": "2026-10-26T00:00:00Z",
            },
            {
                "conversation_response_id": "ptc-run-user-meta",
                "status": "error",
                "errors": ["worker_lost"],
                "metadata": {"cancelled_by_user": True},
            },
        ],
    )
    async def test_cancelled_or_user_stopped_clears_handoff_as_usual(
        self, run_row
    ):
        from contextlib import asynccontextmanager
        from unittest.mock import create_autospec

        from src.server.database.runs import lifecycle as tl_db
        from src.server.database.runs import outbox as outbox_db
        from src.server.services.report_back.handoff import executor
        from src.server.services.report_back.handoff.keys import (
            ptc_origin_key,
            watch_key,
        )
        from src.utils.cache import redis_cache

        cache = self._seeded_cache()
        get_run_mock = create_autospec(tl_db.get_run, return_value=run_row)
        get_cache_mock = create_autospec(
            redis_cache.get_cache_client, return_value=cache
        )

        @asynccontextmanager
        async def _owned_guard(job_id: str, attempts: int, *, lease_seconds: int = 60):
            yield True

        guard_mock = create_autospec(
            outbox_db.fenced_job_guard, side_effect=_owned_guard
        )
        # DALTON-HOOK(handoff-keep-on-resumable): cancelled runs are never resumable
        with (
            patch.object(outbox_db, "fenced_job_guard", guard_mock),
            patch.object(redis_cache, "get_cache_client", get_cache_mock),
            patch.object(tl_db, "get_run", get_run_mock),
        ):
            assert (
                await executor.is_resumable_analyst_failure(
                    run_row["conversation_response_id"]
                )
                is False
            )
            await core._exec_watch_clear(
                {
                    "hook_outbox_id": "job-clear-cancel",
                    "hook_type": "watch_clear",
                    "run_id": "rb-run-cancel",
                    "attempts": 1,
                    "ordering_key": "cos-home",
                    "payload": {
                        "ptc_thread_id": "ptc-dash",
                        "user_id": "u-1",
                        "error_wake": True,
                        "dispatch_gen": "gen-1",
                    },
                }
            )
        assert "ptc-dash" not in cache.client.sets.get(watch_key("cos-home"), set())
        assert ptc_origin_key("ptc-dash") not in cache.kv

    # DALTON-HOOK(handoff-keep-on-resumable): closed DB pool raises for drainer nack
    @pytest.mark.asyncio
    async def test_db_pool_not_open_raises_and_nacks_without_clearing(self):
        from unittest.mock import create_autospec

        from src.server.database.runs import lifecycle as tl_db
        from src.server.database.runs import outbox as outbox_db
        from src.server.services.report_back.handoff.keys import (
            ptc_origin_key,
            watch_key,
        )
        from src.utils.cache import redis_cache

        cache = self._seeded_cache()
        get_cache_mock = create_autospec(
            redis_cache.get_cache_client, return_value=cache
        )
        get_latest_mock = create_autospec(
            tl_db.get_latest_attempt,
            return_value={"conversation_response_id": "cos-idle", "status": "completed"},
        )
        has_live_mock = create_autospec(
            outbox_db.has_live_hand_off, return_value=False
        )
        get_run_mock = create_autospec(
            tl_db.get_run,
            side_effect=RuntimeError(
                "Conversation database pool is not open. Ensure lifespan initializes it."
            ),
        )
        ack_mock = create_autospec(outbox_db.ack_outbox_job, return_value=True)
        nack_mock = create_autospec(
            outbox_db.nack_outbox_job, return_value="pending"
        )

        drainer = HookOutboxDrainer()
        # DALTON-HOOK(handoff-keep-on-resumable): pool-not-open nacks instead of acking
        with (
            patch.object(redis_cache, "get_cache_client", get_cache_mock),
            patch.object(tl_db, "get_latest_attempt", get_latest_mock),
            patch.object(outbox_db, "has_live_hand_off", has_live_mock),
            patch.object(tl_db, "get_run", get_run_mock),
            patch.object(outbox_db, "ack_outbox_job", ack_mock),
            patch.object(outbox_db, "nack_outbox_job", nack_mock),
        ):
            await drainer._execute(
                {
                    "hook_outbox_id": "job-err-pool",
                    "hook_type": "report_back",
                    "run_id": "ptc-run-0",
                    "attempts": 1,
                    "ordering_key": "cos-home",
                    "payload": {
                        "ptc_thread_id": "ptc-dash",
                        "dispatch_gen": "gen-1",
                        "final_status": "error",
                    },
                }
            )

        nack_mock.assert_awaited_once()
        ack_mock.assert_not_awaited()
        assert "ptc-dash" in cache.client.sets.get(watch_key("cos-home"), set())
        assert ptc_origin_key("ptc-dash") in cache.kv

    # DALTON-HOOK(handoff-keep-on-resumable): NotOwed exit logs INFO with context
    @pytest.mark.asyncio
    async def test_not_owed_exit_logs_info(self, caplog):
        from src.server.services.report_back.handoff import executor
        from src.server.services.report_back.handoff.keys import watch_key

        cache = self._seeded_cache()
        cache.client.sets[watch_key("cos-home")].clear()

        job = {
            "hook_outbox_id": "job-cleared",
            "hook_type": "report_back",
            "ordering_key": "cos-home",
            "payload": {
                "ptc_thread_id": "ptc-dash",
                "dispatch_gen": "gen-1",
                "final_status": "completed",
            },
        }
        with caplog.at_level(
            "INFO", logger="src.server.handlers.chat_handler"
        ):
            verdict = await executor.analyst_report_verdict(cache, job)

        assert isinstance(verdict, executor.NotOwed)
        assert verdict.reason == "its pair was already cleared"
        assert any(
            "its pair was already cleared" in rec.message
            and "cos-home" in rec.message
            and "ptc-dash" in rec.message
            and "gen-1" in rec.message
            for rec in caplog.records
        )

    # DALTON-HOOK(handoff-keep-on-resumable): Round 5 #2 — marked g1 -> provisional
    # reserve (g2) -> old clear for g1 -> rollback of g2 keeps both the pair and marker
    @pytest.mark.asyncio
    async def test_awaiting_resume_survives_provisional_reserve_old_clear_and_rollback(
        self,
    ):
        from contextlib import asynccontextmanager
        from unittest.mock import create_autospec

        from src.server.database.runs import lifecycle as tl_db
        from src.server.database.runs import outbox as outbox_db
        from src.server.services.report_back.handoff import pointer, reserve
        from src.server.services.report_back.handoff.keys import (
            ptc_origin_key,
            ptc_teardown_tombstone_key,
            user_pending_key,
            watch_key,
        )
        from src.utils.cache import redis_cache

        cache = self._seeded_cache()
        await pointer.mark_awaiting_resume(
            cache,
            "ptc-dash",
            analyst_run_id="ptc-run-0",
            expected_gen="gen-1",
        )

        get_cache_mock = create_autospec(
            redis_cache.get_cache_client, return_value=cache
        )
        get_run_mock = create_autospec(
            tl_db.get_run,
            return_value={
                "conversation_response_id": "ptc-run-0",
                "status": "error",
                "errors": ["worker_lost: no live executor holds this run's writer fence"],
            },
        )
        has_gen_mock = create_autospec(
            tl_db.thread_has_dispatch_gen, return_value=False
        )
        active_run_mock = create_autospec(
            tl_db.get_active_run, return_value=None
        )

        @asynccontextmanager
        async def _owned_guard(job_id: str, attempts: int, *, lease_seconds: int = 60):
            yield True

        guard_mock = create_autospec(
            outbox_db.fenced_job_guard, side_effect=_owned_guard
        )

        # DALTON-HOOK(handoff-keep-on-resumable): exercise reserve -> fenced old clear -> rollback
        with (
            patch.object(redis_cache, "get_cache_client", get_cache_mock),
            patch.object(tl_db, "get_run", get_run_mock),
            patch.object(tl_db, "thread_has_dispatch_gen", has_gen_mock),
            patch.object(tl_db, "get_active_run", active_run_mock),
            patch.object(outbox_db, "fenced_job_guard", guard_mock),
        ):
            async with reserve.reserve(
                "cos-home", "ptc-dash", "ws-dash", "fws-1", "u-1"
            ) as slot:
                assert slot.error is None
                # RESERVE_LUA preserved awaiting_resume on the new generation's origin
                origin_mid = cache.kv[ptc_origin_key("ptc-dash")]
                assert origin_mid["dispatch_gen"] == slot.dispatch_gen
                assert (
                    origin_mid["awaiting_resume"]["analyst_run_id"]
                    == "ptc-run-0"
                )
                assert origin_mid["awaiting_resume"]["dispatch_gen"] == "gen-1"

                # Old watch_clear for gen-1 arrives while provisional gen-2 holds origin
                await core._exec_watch_clear(
                    {
                        "hook_outbox_id": "job-clear-old-gen1",
                        "hook_type": "watch_clear",
                        "run_id": "rb-run-0",
                        "attempts": 1,
                        "ordering_key": "cos-home",
                        "payload": {
                            "ptc_thread_id": "ptc-dash",
                            "user_id": "u-1",
                            "error_wake": False,
                            "dispatch_gen": "gen-1",
                        },
                    }
                )
                # Tombstone was recorded by the gen fence, and orphan resolve was suppressed
                assert "gen-1" in cache.client.sets.get(
                    ptc_teardown_tombstone_key("ptc-dash"), set()
                )
                assert "ptc-dash" in cache.client.sets.get(
                    watch_key("cos-home"), set()
                )
                # Exit without slot.commit() -> triggers ROLLBACK_RESERVE_LUA

        # After rollback: pair and awaiting_resume marker are both preserved!
        assert "ptc-dash" in cache.client.sets.get(watch_key("cos-home"), set())
        assert "ptc-dash" in cache.client.sets.get(user_pending_key("u-1"), set())
        restored_origin = cache.kv.get(ptc_origin_key("ptc-dash"))
        assert isinstance(restored_origin, dict)
        assert restored_origin["dispatch_gen"] == "gen-1"
        assert (
            restored_origin["awaiting_resume"]["analyst_run_id"] == "ptc-run-0"
        )
        assert (
            ptc_teardown_tombstone_key("ptc-dash") not in cache.client.sets
        )

    # DALTON-HOOK(handoff-keep-on-resumable): Round 5 #4a — strict GET raises -> no teardown
    @pytest.mark.asyncio
    async def test_clear_handoff_strict_get_failure_raises_without_teardown(self):
        from unittest.mock import create_autospec

        from src.server.services.report_back.handoff import pointer
        from src.server.services.report_back.handoff.keys import (
            ptc_origin_key,
            user_pending_key,
            watch_key,
        )

        cache = self._seeded_cache()
        eval_spy = create_autospec(
            cache.client.eval, side_effect=cache.client.eval
        )
        get_strict_mock = create_autospec(
            cache.get_strict,
            side_effect=ConnectionError("redis strict read timeout"),
        )
        with (
            patch.object(cache, "get_strict", get_strict_mock),
            patch.object(cache.client, "eval", eval_spy),
        ):
            with pytest.raises(ConnectionError, match="redis strict read timeout"):
                await pointer.clear_handoff_report_back(
                    cache,
                    "ptc-dash",
                    "cos-home",
                    expected_gen="gen-1",
                )

        eval_spy.assert_not_awaited()
        assert "ptc-dash" in cache.client.sets.get(watch_key("cos-home"), set())
        assert "ptc-dash" in cache.client.sets.get(user_pending_key("u-1"), set())
        assert ptc_origin_key("ptc-dash") in cache.kv

    # DALTON-HOOK(handoff-keep-on-resumable): Round 5 #4b — another worker stamps
    # awaiting_resume after Python pre-read and right before Lua eval -> script refuses
    @pytest.mark.asyncio
    async def test_interleaved_mark_before_teardown_and_orphan_lua_refuses_clear(
        self,
    ):
        from unittest.mock import create_autospec

        from src.server.database.runs import lifecycle as tl_db
        from src.server.services.report_back.handoff import pointer, reserve
        from src.server.services.report_back.handoff.keys import (
            ptc_origin_key,
            user_pending_key,
            watch_key,
        )

        cache = self._seeded_cache()
        real_get_strict = cache.get_strict

        # 1) During clear_handoff_report_back, get_strict reads unmarked origin, and
        # another worker stamps awaiting_resume right before GATED_TEARDOWN_LUA runs.
        async def _get_strict_then_rival_marks(key: str):
            val = await real_get_strict(key)
            if key == ptc_origin_key("ptc-dash"):
                assert "awaiting_resume" not in val
                await pointer.mark_awaiting_resume(
                    cache,
                    "ptc-dash",
                    analyst_run_id="ptc-run-0",
                    expected_gen="gen-1",
                )
            return val

        get_strict_mock = create_autospec(
            cache.get_strict, side_effect=_get_strict_then_rival_marks
        )
        run_row = {
            "conversation_response_id": "ptc-run-0",
            "status": "error",
            "errors": ["worker_lost: no live executor holds this run's writer fence"],
        }
        get_run_mock = create_autospec(
            tl_db.get_run, side_effect=lambda _rid: dict(run_row)
        )
        has_gen_mock = create_autospec(
            tl_db.thread_has_dispatch_gen, return_value=False
        )
        active_run_mock = create_autospec(
            tl_db.get_active_run, return_value=None
        )

        # DALTON-HOOK(handoff-keep-on-resumable): Lua refuses both teardown and orphan resolve
        with (
            patch.object(cache, "get_strict", get_strict_mock),
            patch.object(tl_db, "get_run", get_run_mock),
            patch.object(tl_db, "thread_has_dispatch_gen", has_gen_mock),
            patch.object(tl_db, "get_active_run", active_run_mock),
        ):
            outcome = await pointer.clear_handoff_report_back(
                cache,
                "ptc-dash",
                "cos-home",
                expected_gen="gen-1",
            )
            assert not outcome.cleared
            assert outcome.awaiting_resume is True
            assert "ptc-dash" in cache.client.sets.get(
                watch_key("cos-home"), set()
            )
            assert "ptc-dash" in cache.client.sets.get(
                user_pending_key("u-1"), set()
            )

            # 2) Same for resolve_orphaned_watch: ORPHAN_RESOLVE_LUA checks awaiting_resume in Lua
            resolved, _ = await reserve.resolve_orphaned_watch(
                cache,
                "ptc-dash",
                "cos-home",
                "u-1",
                fencer_gen="gen-1",
                job_gen="gen-1",
            )
            assert resolved is False
            assert "ptc-dash" in cache.client.sets.get(
                watch_key("cos-home"), set()
            )

            # 3) Once Postgres reports the run is no longer resumable (cancelled),
            # clear_handoff_report_back conditionally revokes awaiting_resume and tears down.
            run_row["cancel_requested_at"] = "2026-10-26T00:00:00Z"

        with patch.object(tl_db, "get_run", get_run_mock):
            outcome_after_cancel = await pointer.clear_handoff_report_back(
                cache,
                "ptc-dash",
                "cos-home",
                expected_gen="gen-1",
            )
        assert outcome_after_cancel.cleared is True
        assert ptc_origin_key("ptc-dash") not in cache.kv
        assert "ptc-dash" not in cache.client.sets.get(
            watch_key("cos-home"), set()
        )

    # DALTON-HOOK(handoff-keep-on-resumable): verify AnalystReport.resumable on
    # interim vs terminal reports and _reported_runs extension across resumable error runs
    @pytest.mark.asyncio
    async def test_report_source_resumable_flag_and_continued_run_chain(self):
        from unittest.mock import create_autospec

        from src.dalton.database import runs as dalton_runs_db
        from src.server.database.runs import lifecycle as tl_db
        from src.server.database.runs import outbox as outbox_db
        from src.server.services.report_back.handoff import executor

        runs_by_id = {
            "ptc-run-init": {
                "conversation_response_id": "ptc-run-init",
                "status": "interrupted",
                "errors": [],
            },
            "ptc-run-0": {
                "conversation_response_id": "ptc-run-0",
                "status": "error",
                "errors": [
                    "worker_lost: no live executor holds this run's writer fence"
                ],
            },
            "ptc-run-ordinary-err": {
                "conversation_response_id": "ptc-run-ordinary-err",
                "status": "error",
                "errors": ["Model rate limit exceeded"],
            },
            "ptc-run-1": {
                "conversation_response_id": "ptc-run-1",
                "status": "interrupted",
                "errors": [],
            },
            "ptc-run-2": {
                "conversation_response_id": "ptc-run-2",
                "status": "completed",
                "errors": [],
            },
        }
        predecessor_map: dict[str, dict | None] = {
            "ptc-run-2": runs_by_id["ptc-run-1"],
            "ptc-run-1": runs_by_id["ptc-run-0"],
            "ptc-run-0": runs_by_id["ptc-run-init"],
            "ptc-run-init": None,
            "ptc-run-ordinary-err": None,
        }

        async def _fake_get_run(run_id: str):
            return dict(runs_by_id[run_id]) if run_id in runs_by_id else None

        async def _fake_continued(thread_id: str, run_id: str):
            if run_id == "ptc-run-2":
                return ["ptc-run-1", "ptc-run-2"]
            return [run_id]

        async def _fake_prev(thread_id: str, run_id: str):
            row = predecessor_map.get(run_id)
            return dict(row) if row else None

        # DALTON-HOOK(handoff-keep-on-resumable): autospec test doubles
        get_run_mock = create_autospec(tl_db.get_run, side_effect=_fake_get_run)
        continued_mock = create_autospec(
            tl_db.get_continued_runs, side_effect=_fake_continued
        )
        prev_mock = create_autospec(
            dalton_runs_db.get_previous_run_on_thread, side_effect=_fake_prev
        )
        taken_mock = create_autospec(
            outbox_db.list_taken_hand_offs, return_value=[]
        )
        ws_name_mock = create_autospec(
            executor._workspace_name, return_value="DASH"
        )

        def _make_owed(final_status: str, run_id: str) -> executor.OwedAnalystReport:
            return executor.OwedAnalystReport(
                origin={"ptc_workspace_id": "ws-dash"},
                dispatcher_thread_id="cos-home",
                ptc_thread_id="ptc-dash",
                final_status=final_status,
                run_id=run_id,
            )

        with (
            patch.object(tl_db, "get_run", get_run_mock),
            patch.object(tl_db, "get_continued_runs", continued_mock),
            patch.object(
                dalton_runs_db, "get_previous_run_on_thread", prev_mock
            ),
            patch.object(outbox_db, "list_taken_hand_offs", taken_mock),
            patch.object(executor, "_workspace_name", ws_name_mock),
        ):
            # 1) Interim resumable failure: resumable is True
            _, interim_src = await executor.report_message(
                _make_owed("error", "ptc-run-0")
            )
            assert interim_src.resumable is True
            assert interim_src.model_dump(exclude_none=True)["resumable"] is True
            assert interim_src.run_ids == ["ptc-run-init", "ptc-run-0"]

            # 2) Non-resumable error: resumable is None
            _, ordinary_err_src = await executor.report_message(
                _make_owed("error", "ptc-run-ordinary-err")
            )
            assert ordinary_err_src.resumable is None
            assert "resumable" not in ordinary_err_src.model_dump(
                exclude_none=True
            )
            assert ordinary_err_src.run_ids == ["ptc-run-ordinary-err"]

            # 3) Final completed report after resumable error: resumable is None,
            # and run_ids chains back through ptc-run-0 and ptc-run-init
            _, final_src = await executor.report_message(
                _make_owed("completed", "ptc-run-2")
            )
            assert final_src.resumable is None
            assert "resumable" not in final_src.model_dump(exclude_none=True)
            assert final_src.run_ids == [
                "ptc-run-init",
                "ptc-run-0",
                "ptc-run-1",
                "ptc-run-2",
            ]

            # 4) Preceding run is an ordinary (non-resumable) error: not chained
            predecessor_map["ptc-run-1"] = runs_by_id["ptc-run-ordinary-err"]
            _, after_ordinary_err_src = await executor.report_message(
                _make_owed("completed", "ptc-run-2")
            )
            assert after_ordinary_err_src.run_ids == ["ptc-run-1", "ptc-run-2"]

            # 5) DB failure when reading preceding run: falls back to upstream chain
            prev_mock.side_effect = RuntimeError("db down")
            _, fallback_src = await executor.report_message(
                _make_owed("completed", "ptc-run-2")
            )
            assert fallback_src.run_ids == ["ptc-run-1", "ptc-run-2"]





class TestDrainerQuotas:
    @pytest.mark.asyncio
    async def test_a_burst_on_an_idle_drainer_stays_inside_its_quota(self):
        """The burst a quota exists for arrives while nothing is in flight, so
        the first claim must not be able to fill every slot with one type."""
        import asyncio

        from src.server.database.runs import outbox as outbox_db

        due = [_job(hook_type="market_target") for _ in range(12)]
        due += [_job(hook_type="steer") for _ in range(4)]

        async def claim(limit, *, excluded_hook_types=None, **_):
            skip = set(excluded_hook_types or [])
            picked = [j for j in due if j["hook_type"] not in skip][:limit]
            for job in picked:
                due.remove(job)
            return picked

        drainer = HookOutboxDrainer()
        hold = asyncio.Event()

        async def execute(job):
            await hold.wait()

        async def stop_waiting():
            drainer._stopping.set()

        with patch.object(
            outbox_db, "park_exhausted_jobs", AsyncMock(return_value=[])
        ), patch.object(
            outbox_db, "revive_dead_cleanup_jobs", AsyncMock(return_value=[])
        ), patch.object(
            outbox_db, "revive_dead_alerts", AsyncMock(return_value=[])
        ), patch.object(
            outbox_db, "close_unowed_waiting_alerts", AsyncMock(return_value=[])
        ), patch.object(
            outbox_db, "purge_terminal_jobs", AsyncMock(return_value=0)
        ), patch.object(
            outbox_db, "claim_outbox_jobs", side_effect=claim
        ), patch.object(drainer, "_execute", side_effect=execute), patch.object(
            drainer, "_wait_for_nudge", side_effect=stop_waiting
        ):
            await drainer._loop()
            running = list(drainer._inflight.values())
            hold.set()
            await asyncio.gather(*drainer._inflight)

        assert running.count("market_target") == hook_outbox.MARKET_TARGET_INFLIGHT_QUOTA
        assert running.count("steer") == 4


class TestAlertSweeps:
    """Market-target alerts: an older build's dead ones made due again,
    untakeable ones closed, each on its own drainer clock rather than every
    pass."""

    @staticmethod
    def _sweeps(**overrides):
        from src.server.database.runs import outbox as outbox_db

        mocks = {
            "park_exhausted_jobs": AsyncMock(return_value=[]),
            "revive_dead_cleanup_jobs": AsyncMock(return_value=[]),
            "revive_dead_alerts": AsyncMock(return_value=[]),
            "close_unowed_waiting_alerts": AsyncMock(return_value=[]),
            "purge_terminal_jobs": AsyncMock(return_value=0),
            "claim_outbox_jobs": AsyncMock(return_value=[]),
            **overrides,
        }
        return mocks, [patch.object(outbox_db, name, m) for name, m in mocks.items()]

    @staticmethod
    async def _passes(drainer, n: int):
        left = [n]

        async def wait():
            left[0] -= 1
            if left[0] <= 0:
                drainer._stopping.set()

        with patch.object(drainer, "_wait_for_nudge", side_effect=wait):
            await drainer._loop()

    @pytest.mark.asyncio
    async def test_both_sweeps_run_once_per_interval(self):
        import contextlib

        drainer = HookOutboxDrainer()
        mocks, patches = self._sweeps(
            revive_dead_alerts=AsyncMock(
                return_value=[{"hook_outbox_id": "a-1", "ordering_key": "k"}]
            ),
            close_unowed_waiting_alerts=AsyncMock(
                return_value=[{"hook_outbox_id": "a-2", "ordering_key": "k"}]
            ),
        )
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            warning = stack.enter_context(patch.object(hook_outbox.logger, "warning"))
            await self._passes(drainer, 3)
        assert mocks["park_exhausted_jobs"].await_count == 3
        mocks["revive_dead_alerts"].assert_awaited_once()
        mocks["close_unowed_waiting_alerts"].assert_awaited_once()
        assert any("a-1" in str(c.args[0]) for c in warning.call_args_list)

    @pytest.mark.asyncio
    async def test_the_close_runs_on_a_shorter_clock_than_the_revive(self):
        """A dead alert must stay dead ten minutes before it qualifies, so
        the revive runs every ten minutes; the close keeps its minute."""
        import contextlib

        drainer = HookOutboxDrainer()
        mocks, patches = self._sweeps()
        passes = [0]

        async def wait():
            passes[0] += 1
            if passes[0] == 1:
                # A minute on: the close is due again, the revive is not.
                drainer._next_alert_close = 0.0
            else:
                drainer._stopping.set()

        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(patch.object(drainer, "_wait_for_nudge", side_effect=wait))
            await drainer._loop()
        assert mocks["close_unowed_waiting_alerts"].await_count == 2
        mocks["revive_dead_alerts"].assert_awaited_once()
        assert hook_outbox.ALERT_CLOSE_INTERVAL == 60.0
        assert hook_outbox.ALERT_REVIVE_INTERVAL == 600.0
        assert drainer._next_alert_revive - drainer._next_alert_close == pytest.approx(
            hook_outbox.ALERT_REVIVE_INTERVAL - hook_outbox.ALERT_CLOSE_INTERVAL, abs=5
        )

    @pytest.mark.asyncio
    async def test_a_failed_revive_does_not_skip_the_close(self):
        import contextlib

        drainer = HookOutboxDrainer()
        mocks, patches = self._sweeps(
            revive_dead_alerts=AsyncMock(side_effect=RuntimeError("db down"))
        )
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            error = stack.enter_context(patch.object(hook_outbox.logger, "error"))
            await self._passes(drainer, 1)
        mocks["close_unowed_waiting_alerts"].assert_awaited_once()
        error.assert_called_once()

    @pytest.mark.asyncio
    async def test_an_alert_the_park_sweep_holds_is_not_dead_lettered(self):
        import contextlib

        drainer = HookOutboxDrainer()
        held = {
            "hook_outbox_id": "a-1",
            "hook_type": "market_target",
            "status": "pending",
            "attempts": hook_outbox.MAX_ATTEMPTS,
            "payload": {"exhausted": True},
        }
        _, patches = self._sweeps(park_exhausted_jobs=AsyncMock(return_value=[held]))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            error = stack.enter_context(patch.object(hook_outbox.logger, "error"))
            exhausted = stack.enter_context(
                patch.object(drainer, "_exhausted", AsyncMock())
            )
            closed = stack.enter_context(
                patch.object(drainer, "_close_followup", AsyncMock())
            )
            await self._passes(drainer, 1)
        exhausted.assert_awaited_once_with(held)
        closed.assert_not_awaited()
        error.assert_not_called()
