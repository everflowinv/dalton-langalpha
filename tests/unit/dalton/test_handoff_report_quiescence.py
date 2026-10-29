"""R1 fix1: a hand-off "completed" report waits for the analyst thread by
RESCHEDULING itself, never by holding a drainer slot; at the wait cap it
delivers an honest unfinished report instead of "has completed".

The executor probe (quiescence) reads the ledger through stubbed modules; the
reschedule/outbox seam is stubbed at the outbox module. All doubles are
autospecced or hand-written; no real network or database is touched.
"""

from __future__ import annotations

import time
from contextlib import ExitStack, asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import create_autospec, patch

import pytest

from src.server.database.runs import (
    command_runs as cr_db,
    lifecycle as tl_db,
    outbox as outbox_db,
    subagent_runs as sr_db,
)
from src.server.services.report_back import notify_turn, steer
from src.server.services.report_back.handoff import executor, keys, pointer
from tests.unit.server.handlers.chat.redis_fakes import (
    FakeCache as _FakeCache,
    seed_dispatched as _seed_dispatched,
)


def _job(ptc: str, **payload_extra) -> dict:
    return {
        "hook_outbox_id": "job-q",
        "hook_type": "report_back",
        "payload": {"ptc_thread_id": ptc, **payload_extra},
        "run_id": "ptc-run-1",
        "attempts": 3,
        "ordering_key": "cos-1",
    }


def _run_row(status: str) -> dict:
    return {"conversation_response_id": "rb-run", "status": status}


class _QuiescenceHarness:
    """Every collaborator of execute_report_back patched in one place, except
    the quiescence probe, whose ledger reads the tests drive."""

    def __init__(
        self,
        cache: _FakeCache,
        *,
        run_statuses=("completed",),
        lease_ok=True,
        open_run_rows=None,
    ):
        self.cache = cache
        self.post = create_autospec(executor._post_report_back)
        self.post.return_value = ("dispatched", "rb-run")
        self.get_run = create_autospec(
            tl_db.get_run,
            side_effect=[_run_row(s) if s else None for s in run_statuses],
        )
        self.extend_lease = create_autospec(
            outbox_db.extend_job_lease, return_value=lease_ok
        )
        self.merge_payload = create_autospec(outbox_db.merge_job_payload)
        self.reschedule = create_autospec(outbox_db.reschedule_claimed_job)
        self.reschedule.return_value = "pending"
        self.requeue = create_autospec(outbox_db.requeue_job_with_key)
        rows = open_run_rows if open_run_rows is not None else []
        self.open_runs = create_autospec(
            sr_db.list_open_runs_for_thread, return_value=list(rows)
        )
        self.open_jobs = create_autospec(
            outbox_db.list_open_notification_jobs, return_value=[]
        )
        self.running_commands = create_autospec(
            cr_db.list_running, return_value=[]
        )
        self.latest_attempt = create_autospec(tl_db.get_latest_attempt)
        self.latest_attempt.return_value = None

        @asynccontextmanager
        async def _guard(job_id, attempts):
            yield True

        self.fenced_guard = _guard

    @contextmanager
    def _stack(self):
        with ExitStack() as stack:
            for ctx in (
                patch("src.utils.cache.redis_cache.get_cache_client",
                      return_value=self.cache),
                patch.object(executor, "_post_report_back", self.post),
                patch.object(tl_db, "get_run", self.get_run),
                patch.object(notify_turn, "park_while_thread_busy",
                             create_autospec(notify_turn.park_while_thread_busy,
                                             return_value=False)),
                patch.object(outbox_db, "extend_job_lease", self.extend_lease),
                patch.object(outbox_db, "merge_job_payload", self.merge_payload),
                patch.object(outbox_db, "reschedule_claimed_job", self.reschedule),
                patch.object(outbox_db, "requeue_job_with_key", self.requeue),
                patch.object(outbox_db, "fenced_job_guard", self.fenced_guard),
                patch.object(outbox_db, "has_live_hand_off",
                             create_autospec(outbox_db.has_live_hand_off,
                                             return_value=False)),
                patch.object(tl_db, "run_has_dispatch_gen",
                             create_autospec(tl_db.run_has_dispatch_gen,
                                             return_value=False)),
                patch.object(notify_turn, "_TERMINAL_POLL", 0.0),
                # The probe's reads: the unit under test keeps these real.
                patch.object(sr_db, "list_open_runs_for_thread", self.open_runs),
                patch.object(outbox_db, "list_open_notification_jobs", self.open_jobs),
                patch.object(cr_db, "list_running", self.running_commands),
                patch.object(tl_db, "get_latest_attempt", self.latest_attempt),
                patch.object(tl_db, "find_run_by_request_key",
                             create_autospec(tl_db.find_run_by_request_key,
                                             return_value=None)),
            ):
                stack.enter_context(ctx)
            yield

    async def run(self, job: dict) -> None:
        with self._stack():
            await executor.execute_report_back(job)


def _seeded() -> tuple[_FakeCache, str, str]:
    cache = _FakeCache()
    dispatcher, ptc = "cos-1", "ptc-1"
    _seed_dispatched(cache, dispatcher, [ptc])
    return cache, dispatcher, ptc


# ---------------------------------------------------------------------------
# The hold: no report and no slot while the analyst thread still works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_reschedules_while_a_subagent_run_is_open():
    """Not quiescent: the job is rescheduled (30s..5min, at the SAME attempt
    number — a wait spends no delivery-failure budget) and the claim returns
    at once, freeing the drainer slot. Nothing posts and nothing tears down."""
    cache, dispatcher, ptc = _seeded()
    h = _QuiescenceHarness(
        cache, open_run_rows=[{"task_run_id": "tr-1", "status": "in_progress"}]
    )
    await h.run(_job(ptc, final_status="completed"))

    h.post.assert_not_awaited()
    h.reschedule.assert_awaited_once()
    args = h.reschedule.await_args
    assert args.kwargs["attempts"] == 3  # the claim's own token, not burned
    assert 30.0 <= args.kwargs["delay_seconds"] <= 300.0
    # The wait anchor is durable before the reschedule; the reschedule count
    # is the SQL's own atomic payload bump, not a merge (no double-write).
    patch_dict = h.merge_payload.await_args.args[1]
    assert set(patch_dict) == {"quiescence_wait_since"}
    # The pair is untouched — the pending chip on the watcher stays up.
    assert ptc in cache.client.sets[keys.watch_key(dispatcher)]
    assert f"ptc_origin:{ptc}" in cache.kv

    # fix2: every reschedule renews the pair's Redis keys. fix3: the renewal
    # only ever EXTENDS, and its floor is the reserve-time TTL constant —
    # the shared watch/user-pending keys of a sibling pair are never
    # trampled down to minutes.
    from src.server.services.report_back.handoff import keys as hb_keys

    floor = float(hb_keys.PTC_ORIGIN_TTL)
    assert cache.client.ttls[hb_keys.ptc_origin_key(ptc)] >= floor
    assert cache.client.ttls[hb_keys.watch_key(dispatcher)] >= floor
    assert cache.client.ttls[hb_keys.user_pending_key("u-1")] >= floor


@pytest.mark.asyncio
async def test_report_reschedules_while_a_command_report_is_owed():
    """A finished background command whose report-back job has not run its
    notification turn yet is as much pending work as an open subagent: the
    probe reads the thread's whole wake-job family, not only subagents."""
    cache, _, ptc = _seeded()
    h = _QuiescenceHarness(cache)
    h.open_jobs = create_autospec(
        outbox_db.list_open_notification_jobs,
        return_value=[
            {"hook_outbox_id": "cmd-job-1", "hook_type": "command_report_back"}
        ],
    )
    await h.run(_job(ptc, final_status="completed"))

    h.post.assert_not_awaited()
    h.reschedule.assert_awaited_once()
    # The probe asked for the wake-job family, not one type.
    asked = h.open_jobs.await_args.args[1]
    assert "command_report_back" in asked
    assert "task_report_back" in asked
    # fix2: a command_notice is NOT a wake job — by design it wakes nobody
    # and lives under its own prefixed key, so the thread-keyed read cannot
    # even see it. Running commands themselves hold the report (below).
    assert "command_notice" not in asked
    from src.server.database.runs.command_runs import notice_ordering_key

    assert notice_ordering_key("t-1") != "t-1"

    # fix2: a command still RUNNING is pending work in its own right, read
    # from the command ledger, not from any outbox row.
    h2 = _QuiescenceHarness(cache)
    h2.running_commands = create_autospec(
        cr_db.list_running, return_value=[{"command_id": "c-live"}]
    )
    h2.open_jobs = create_autospec(
        outbox_db.list_open_notification_jobs, return_value=[]
    )
    await h2.run(_job(ptc, final_status="completed"))
    h2.post.assert_not_awaited()
    h2.reschedule.assert_awaited_once()
    h2.running_commands.assert_awaited()


@pytest.mark.asyncio
async def test_report_reschedules_while_the_thread_runs_its_own_turn():
    """A live (or interrupted) turn on the analyst thread is work in motion.
    On a legacy job that names no run of its own, the takeover check cannot
    end the wait, so the probe's own latest-attempt read carries the hold."""
    cache, _, ptc = _seeded()
    h = _QuiescenceHarness(cache)
    h.latest_attempt = create_autospec(
        tl_db.get_latest_attempt,
        return_value={"conversation_response_id": "R-live", "status": "in_progress"},
    )
    job = {**_job(ptc, final_status="completed"), "run_id": None}
    await h.run(job)

    h.post.assert_not_awaited()
    h.reschedule.assert_awaited_once()


@pytest.mark.asyncio
async def test_many_waiting_reports_release_their_slots_at_once():
    """The starvation shape the lead named: more waiting report_backs than
    the drainer's MAX_CONCURRENT_JOBS. Every claim must return promptly —
    none of them may hold its slot waiting — so a burst of 12 cannot starve
    the task_report_back jobs whose turns end the wait. (Claims run one by
    one: the assertion is that EACH returns without waiting, which the old
    hold-loop shape could not do at all without a lease flip.)"""
    cache = _FakeCache()
    dispatchers = [f"cos-{i}" for i in range(12)]
    for d in dispatchers:
        _seed_dispatched(cache, d, [f"ptc-{d}"])

    started = time.monotonic()
    for dispatcher in dispatchers:
        h = _QuiescenceHarness(
            cache,
            open_run_rows=[{"task_run_id": "tr-1", "status": "in_progress"}],
        )
        job = {
            "hook_outbox_id": f"job-{dispatcher}",
            "hook_type": "report_back",
            "payload": {"ptc_thread_id": f"ptc-{dispatcher}",
                        "final_status": "completed"},
            "run_id": f"run-{dispatcher}",
            "attempts": 1,
            "ordering_key": dispatcher,
        }
        await h.run(job)
        h.post.assert_not_awaited()
        h.reschedule.assert_awaited_once()
    elapsed = time.monotonic() - started
    # 12 claims come straight back: no claim waited on the analyst's work.
    assert elapsed < 10.0


@pytest.mark.asyncio
async def test_a_lost_lease_mid_hold_stands_down_silently():
    """Fence lost between the payload merge and the reschedule: the claim
    ends with no reschedule of the new owner's row and no teardown."""
    cache, _, ptc = _seeded()
    h = _QuiescenceHarness(
        cache,
        lease_ok=False,
        open_run_rows=[{"task_run_id": "tr-1", "status": "in_progress"}],
    )
    await h.run(_job("ptc-1", final_status="completed"))

    h.reschedule.assert_not_awaited()
    h.post.assert_not_awaited()


# ---------------------------------------------------------------------------
# The release: one report once the thread is quiescent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_posts_once_when_quiescent():
    """Quiescent at the first probe: the report posts exactly once, as
    completed, with one wake — and no reschedule happens."""
    cache, _, ptc = _seeded()
    h = _QuiescenceHarness(cache)
    await h.run(_job(ptc, final_status="completed"))

    h.reschedule.assert_not_awaited()
    h.post.assert_awaited_once()
    assert h.post.await_args.args[1].final_status == "completed"
    assert h.post.await_args.args[1].unfinished is None
    wakes = [msg for _, msg in cache.client.published if '"run_id": "rb-run"' in msg]
    assert len(wakes) == 1


@pytest.mark.asyncio
async def test_error_report_does_not_wait_for_quiescence():
    """A failed thread reports as it stands: the hold is a completed-report
    guard, and the existing error semantics are unchanged."""
    cache, _, ptc = _seeded()
    h = _QuiescenceHarness(
        cache,
        run_statuses=("completed", "completed"),
        open_run_rows=[{"task_run_id": "tr-1", "status": "in_progress"}],
    )
    await h.run(_job(ptc, final_status="error"))

    h.post.assert_awaited_once()
    assert h.post.await_args.args[1].final_status == "error"
    assert h.open_runs.await_count == 0  # the probe is never consulted


@pytest.mark.asyncio
async def test_no_second_report_after_the_pair_is_cleared():
    """The dedup the hold must not disturb: once a delivered report's pair is
    terminal-cleared, a later job on it acks without posting again."""
    cache, dispatcher, ptc = _seeded()
    await pointer.clear_handoff_report_back(cache, ptc, dispatcher)

    h = _QuiescenceHarness(cache)
    await h.run(_job(ptc, final_status="completed"))

    h.post.assert_not_awaited()


# ---------------------------------------------------------------------------
# The wait cap: an honest report, never "has completed"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_cap_delivers_the_honest_unfinished_report():
    """Past the cap with work still open, the report goes out naming the
    pending subagents and commands — never 'has completed'."""
    cache, _, ptc = _seeded()
    h = _QuiescenceHarness(
        cache,
        open_run_rows=[{"task_id": "3", "task_run_id": "tr-3",
                        "status": "in_progress"}],
    )
    h.running_commands = create_autospec(
        cr_db.list_running,
        return_value=[{"command_id": "c-9"}],
    )
    cap = 60.0
    job = _job(
        ptc,
        final_status="completed",
        quiescence_wait_since=time.time() - cap - 1,
        quiescence_polls=99,
    )
    with patch.object(executor, "_quiescence_cap",
                      create_autospec(executor._quiescence_cap,
                                      return_value=cap)):
        await h.run(job)

    h.reschedule.assert_not_awaited()  # the cap ends the wait, not a retry
    h.post.assert_awaited_once()
    owed = h.post.await_args.args[1]
    assert owed.unfinished == ["Task-3", "command c-9"]

    # The message itself says what actually stands: unfinished work listed,
    # never "has completed".
    from src.server.services.report_back.handoff.executor import report_message

    message, source = await report_message(owed)
    assert "请勿当作已完成交付" in message
    assert "Task-3" in message and "command c-9" in message
    assert "has completed" not in message
    assert source.unfinished == ["Task-3", "command c-9"]


@pytest.mark.asyncio
async def test_the_cap_anchors_on_when_the_analyst_turn_ended():
    """fix2: a report that is first claimed hours after its turn ended must
    not restart the 24h clock at that claim — the anchor is the turn's own
    end (usage_settled_at), so this turn that ended past the cap reports
    unfinished at its FIRST claim."""
    cache, _, ptc = _seeded()
    h = _QuiescenceHarness(
        cache,
        open_run_rows=[{"task_id": "3", "task_run_id": "tr-3",
                        "status": "in_progress"}],
    )
    h.latest_attempt = create_autospec(
        tl_db.get_latest_attempt,
        return_value={
            "conversation_response_id": "R-end",
            "status": "completed",
            "usage_settled_at": datetime.now(tz=UTC) - timedelta(hours=2),
        },
    )
    cap = 3600.0
    job = _job(ptc, final_status="completed")  # no payload anchor: first sight
    with patch.object(executor, "_quiescence_cap",
                      create_autospec(executor._quiescence_cap,
                                      return_value=cap)):
        await h.run(job)

    # The cap had already passed when the wait began: no reschedule round.
    h.reschedule.assert_not_awaited()
    h.post.assert_awaited_once()
    assert h.post.await_args.args[1].unfinished is not None


@pytest.mark.asyncio
async def test_cap_report_with_unreadable_listing_still_says_unfinished():
    """fix2: unfinished is its own state — both listing reads failing still
    reports the work unfinished ('清单读取失败'), never "has completed"."""
    cache, _, ptc = _seeded()
    h = _QuiescenceHarness(cache)
    h.open_runs = create_autospec(sr_db.list_open_runs_for_thread)
    async def _blown(thread_id):
        raise ConnectionError("db blip")
    h.open_runs.side_effect = _blown
    h.running_commands = create_autospec(cr_db.list_running)
    h.running_commands.side_effect = _blown
    cap = 60.0
    job = _job(
        ptc,
        final_status="completed",
        quiescence_wait_since=time.time() - cap - 1,
    )
    with patch.object(executor, "_quiescence_cap",
                      create_autospec(executor._quiescence_cap,
                                      return_value=cap)):
        await h.run(job)

    h.post.assert_awaited_once()
    owed = h.post.await_args.args[1]
    assert owed.unfinished == [] and owed.listing_failed is True

    from src.server.services.report_back.handoff.executor import report_message

    message, _source = await report_message(owed)
    assert "清单读取失败" in message
    assert "请勿当作已完成交付" in message
    assert "has completed" not in message


@pytest.mark.asyncio
async def test_cap_report_with_an_empty_listing_names_no_items():
    """fix2: reads that answer with nothing still say the work is
    unfinished — '未能列出具体条目', not a completed deliverable."""
    cache, _, ptc = _seeded()
    h = _QuiescenceHarness(cache)
    # Not quiescent via an owed wake job; both listing reads answer empty.
    h.open_jobs = create_autospec(
        outbox_db.list_open_notification_jobs,
        return_value=[{"hook_outbox_id": "j", "hook_type": "task_report_back"}],
    )
    cap = 60.0
    job = _job(
        ptc,
        final_status="completed",
        quiescence_wait_since=time.time() - cap - 1,
    )
    with patch.object(executor, "_quiescence_cap",
                      create_autospec(executor._quiescence_cap,
                                      return_value=cap)):
        await h.run(job)

    h.post.assert_awaited_once()
    owed = h.post.await_args.args[1]
    assert owed.unfinished == [] and owed.listing_failed is False

    from src.server.services.report_back.handoff.executor import report_message

    message, _source = await report_message(owed)
    assert "未能列出具体条目" in message
    assert "has completed" not in message


@pytest.mark.asyncio
async def test_wait_below_cap_keeps_rescheduling_not_delivering():
    """An anchor well inside the cap reschedules with the ladder's ceiling,
    however many polls ran — the cap is wall-clock, not poll-count."""
    cache, _, ptc = _seeded()
    h = _QuiescenceHarness(
        cache, open_run_rows=[{"task_run_id": "tr-1", "status": "in_progress"}]
    )
    job = _job(
        ptc,
        final_status="completed",
        quiescence_wait_since=time.time() - 60.0,
        reschedules=50,
    )
    await h.run(job)

    h.reschedule.assert_awaited_once()
    assert h.reschedule.await_args.kwargs["delay_seconds"] == 300.0
    h.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_renewal_extends_shared_keys_never_shortens_them():
    """fix3: the watch and user-pending keys are SHARED by every pair the
    Home dispatched. A renewal that wrote a short TTL over a sibling's full
    24h (set at reserve) once starved that sibling's report into NotOwed;
    now an extension only ever lengthens a key, floored at the reserve-time
    constant — and 15 minutes later the sibling still reports."""
    from src.server.services.report_back.handoff import executor as ex
    from src.server.services.report_back.handoff import keys as hb_keys

    cache, dispatcher, ptc = _seeded()
    # Pair B reserved moments ago: the shared keys hold a full-strength TTL.
    full = int(hb_keys.PTC_ORIGIN_TTL)
    for key in (
        hb_keys.ptc_origin_key(ptc),
        hb_keys.watch_key(dispatcher),
        hb_keys.user_pending_key("u-1"),
    ):
        cache.client.ttls[key] = full

    await ex._renew_pair_ttls(
        cache, ptc, {"home_thread_id": dispatcher, "user_id": "u-1"}, 30.0
    )

    # Not shortened (the old write of max(30*3, 900) = 900 is the bug).
    assert cache.client.ttls[hb_keys.watch_key(dispatcher)] == full
    assert cache.client.ttls[hb_keys.user_pending_key("u-1")] == full
    assert cache.client.ttls[hb_keys.ptc_origin_key(ptc)] == full

    # An aging key (a pair reserved hours ago) is extended back to full.
    cache.client.ttls[hb_keys.watch_key(dispatcher)] = 600
    await ex._renew_pair_ttls(
        cache, ptc, {"home_thread_id": dispatcher, "user_id": "u-1"}, 30.0
    )
    assert cache.client.ttls[hb_keys.watch_key(dispatcher)] == full

    # And the sibling's report still finds its pair owed — the 15-min-later
    # delivery the shortened keys used to swallow.
    verdict = await ex.analyst_report_verdict(
        cache,
        {
            "hook_outbox_id": "job-b",
            "ordering_key": dispatcher,
            "run_id": "run-b",
            "payload": {"ptc_thread_id": ptc},
        },
    )
    assert isinstance(verdict, ex.OwedAnalystReport)


@pytest.mark.asyncio
async def test_renewal_falls_back_to_read_and_max_without_gt():
    """fix3: a client without EXPIRE's GT option (or a server that refuses
    it) falls back to reading the TTL and writing back only the larger
    value; a read that fails leaves the key exactly as it was."""
    from src.server.services.report_back.handoff import executor as ex
    from src.server.services.report_back.handoff import keys as hb_keys

    cache, dispatcher, ptc = _seeded()

    class _NoGtClient:
        """Wraps the fake client the way an older client/server pair
        behaves: expire takes no gt, ttl reads work."""

        def __init__(self, inner):
            self._inner = inner

        async def expire(self, key, ttl):
            await self._inner.expire(key, ttl)

        async def ttl(self, key):
            return await self._inner.ttl(key)

    class _CacheView:
        def __init__(self, client):
            self.client = client

    full = int(hb_keys.PTC_ORIGIN_TTL)
    watch = hb_keys.watch_key(dispatcher)
    cache.client.ttls[watch] = full
    no_gt = _CacheView(_NoGtClient(cache.client))

    await ex._renew_pair_ttls(
        no_gt, ptc, {"home_thread_id": dispatcher, "user_id": "u-1"}, 30.0
    )
    # Full-strength key untouched (a write of the equal value is skipped);
    # an aging key is raised to the floor, not lowered.
    assert cache.client.ttls[watch] == full
    origin_key = hb_keys.ptc_origin_key(ptc)
    cache.client.ttls[origin_key] = 300
    await ex._renew_pair_ttls(
        no_gt, ptc, {"home_thread_id": dispatcher, "user_id": "u-1"}, 30.0
    )
    assert cache.client.ttls[origin_key] == full


@pytest.mark.asyncio
async def test_reschedules_do_not_burn_the_awaiting_resume_budget():
    """fix3: the executor's budget checks read the EFFECTIVE attempt count
    (raw minus reschedules), like the DB ceilings do. A job claimed five
    times but rescheduled four of them has a budget of one: the
    awaiting_resume revocation that fires at the ceiling must NOT fire."""
    from src.server.services.report_back.handoff import pointer as hb_pointer

    cache, dispatcher, ptc = _seeded()
    assert await hb_pointer.mark_awaiting_resume(
        cache, ptc, analyst_run_id="ptc-run-1", expected_gen=None
    )
    h = _QuiescenceHarness(cache, run_statuses=("completed", "completed"))
    job = {
        "hook_outbox_id": "job-budget",
        "hook_type": "report_back",
        # Raw attempts at the ceiling, but four were waits.
        "attempts": 5,
        "ordering_key": "stale-key",  # drives the requeue branch's guard
        "run_id": "ptc-run-1",
        "payload": {
            "ptc_thread_id": ptc,
            "dispatch_gen": None,
            "final_status": "error",
            "reschedules": 4,
        },
    }
    await h.run(job)

    # The revocation never fired: the marker the resumed run depends on
    # survives, and the job simply requeued onto the right chain.
    origin = cache.kv[keys.ptc_origin_key(ptc)]
    assert hb_pointer.AWAITING_RESUME_FIELD in origin
    h.requeue.assert_awaited_once()


@pytest.mark.asyncio
async def test_quiescence_probe_fails_closed_on_read_error():
    """A ledger read that fails reads as not quiescent — a blind 'completed'
    is the false report the probe exists to stop."""

    async def _broken(thread_id):
        raise ConnectionError("db blip")

    with patch.object(tl_db, "get_latest_attempt", _broken):
        assert await executor.analyst_thread_quiescent("ptc-x") is False


# ---------------------------------------------------------------------------
# The steer side: a live turn must not take a premature report in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyst_offer_leaves_a_report_while_not_quiescent():
    job = {
        "hook_outbox_id": "job-s",
        "hook_type": "report_back",
        "payload": {"ptc_thread_id": "ptc-1", "final_status": "completed"},
        "run_id": None,
    }
    cache = _FakeCache()
    _seed_dispatched(cache, "t-1", ["ptc-1"])
    probe = create_autospec(executor.analyst_thread_quiescent, return_value=False)
    with (
        patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache),
        patch.object(tl_db, "find_run_by_request_key",
                     create_autospec(tl_db.find_run_by_request_key, return_value=None)),
        patch.object(tl_db, "get_latest_attempt",
                     create_autospec(tl_db.get_latest_attempt, return_value=None)),
        patch.object(outbox_db, "has_live_hand_off",
                     create_autospec(outbox_db.has_live_hand_off, return_value=False)),
        patch.object(executor, "analyst_thread_quiescent", probe),
    ):
        offer = await steer._analyst_offer(job, "t-1")

    assert offer is None
    probe.assert_awaited_once_with("ptc-1")
