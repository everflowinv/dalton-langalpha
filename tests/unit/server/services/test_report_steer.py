"""SteeredReportInbox: which owed reports a live turn takes in, the hold that
makes the taking exclusive, and the confirm that closes them once the message
carrying them is checkpointed."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ptc_agent.agent.middleware.steering import SteeredReport
from src.server.services.report_back import steer
from src.server.services.report_back.notify_turn import NotOwed

_OUTBOX = "src.server.database.runs.outbox"
_BY_KEY = "src.server.database.runs.lifecycle.find_run_by_request_key"
_WAKE = "src.server.services.report_back.subagent.publish_cleared_wake"


@pytest.fixture(autouse=True)
def _no_admitted_turn():
    with patch(_BY_KEY, new=AsyncMock(return_value=None)) as by_key:
        yield by_key


@pytest.fixture(autouse=True)
def _no_live_hand_off():
    with patch(
        "src.server.database.runs.outbox.has_live_hand_off",
        new=AsyncMock(return_value=False),
    ) as live:
        yield live


def _job(n: int, hook_type: str = "task_report_back") -> dict:
    return {"hook_outbox_id": f"job-{n}", "hook_type": hook_type, "payload": {}}


def _report(n: int) -> SteeredReport:
    return SteeredReport(
        job_id=f"job-{n}",
        content=f"report {n}",
        shown={"report_source": {"kind": "task", "task_id": str(n)}},
    )


async def _take(jobs, decisions, held, *, closed=(), hold=None, close=None):
    """Run a take with per-job decisions (keyed by call order), the ids the
    hold reports as won, and the ids the unowed close reports as won."""
    hold = hold or AsyncMock(return_value=held)
    close = close or AsyncMock(return_value=list(closed))
    wake = AsyncMock()
    with (
        patch(f"{_OUTBOX}.list_steerable_report_jobs", new=AsyncMock(return_value=jobs)),
        patch(f"{_OUTBOX}.hold_steered_report_jobs", new=hold),
        patch(f"{_OUTBOX}.close_unowed_report_jobs", new=close),
        patch.object(steer, "_task_offer", new=AsyncMock(side_effect=decisions)),
        patch.object(steer, "_analyst_offer", new=AsyncMock(side_effect=decisions)),
        patch(_WAKE, new=wake),
    ):
        reports = await steer.SteeredReportInbox().take(
            "t-1", "run-A", kinds=("report",)
        )
    return reports, hold, close, wake


@pytest.mark.asyncio
async def test_holds_reports_it_hands_over_and_closes_unowed_jobs():
    jobs = [_job(1), _job(2), _job(3), _job(4)]
    decisions = [_report(1), NotOwed("delivered"), None, _report(4)]

    reports, hold, close, wake = await _take(
        jobs, decisions, held=["job-1"], closed=["job-2"]
    )

    # job-3 (left to its executor) is neither held nor closed; job-4 was lost
    # to its executor between the read and the hold.
    hold.assert_awaited_once_with(["job-1", "job-4"], run_id="run-A")
    close.assert_awaited_once_with(["job-2"])
    assert reports == [_report(1)]
    wake.assert_awaited_once_with("t-1")


@pytest.mark.asyncio
async def test_held_reports_keep_oldest_first_order():
    reports, *_ = await _take(
        [_job(1), _job(2)], [_report(1), _report(2)], held=["job-2", "job-1"]
    )

    assert [r.job_id for r in reports] == ["job-1", "job-2"]


@pytest.mark.asyncio
async def test_jobs_are_decided_concurrently():
    """Each decision reads the ledger and Redis; a turn waits on all of them
    before its model call, so they must not queue behind each other."""
    second_started = asyncio.Event()

    async def first(job):
        await asyncio.wait_for(second_started.wait(), timeout=1)
        return _report(1)

    async def second(job):
        second_started.set()
        return _report(2)

    offers = iter([first, second])

    async def _offer(job):
        return await next(offers)(job)

    with (
        patch(
            f"{_OUTBOX}.list_steerable_report_jobs",
            new=AsyncMock(return_value=[_job(1), _job(2)]),
        ),
        patch(
            f"{_OUTBOX}.hold_steered_report_jobs",
            new=AsyncMock(return_value=["job-1", "job-2"]),
        ),
        patch(f"{_OUTBOX}.close_unowed_report_jobs", new=AsyncMock(return_value=[])),
        patch.object(steer, "_task_offer", new=_offer),
        patch(_WAKE, new=AsyncMock()),
    ):
        reports = await steer.SteeredReportInbox().take("t-1", "run-A", kinds=("report",))

    assert [r.job_id for r in reports] == ["job-1", "job-2"]


@pytest.mark.asyncio
async def test_a_job_whose_read_fails_is_left_to_its_executor():
    reports, hold, close, _ = await _take(
        [_job(1), _job(2)], [RuntimeError("boom"), _report(2)], held=["job-2"]
    )

    hold.assert_awaited_once_with(["job-2"], run_id="run-A")
    close.assert_awaited_once_with([])
    assert reports == [_report(2)]


@pytest.mark.asyncio
async def test_listing_failure_takes_nothing():
    with patch(
        f"{_OUTBOX}.list_steerable_report_jobs",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        assert await steer.SteeredReportInbox().take("t-1", "run-A", kinds=("report",)) == []


@pytest.mark.asyncio
async def test_hold_failure_takes_nothing_but_still_closes_unowed_jobs():
    hold = AsyncMock(side_effect=RuntimeError("db down"))

    reports, _, close, _ = await _take(
        [_job(1), _job(2)], [_report(1), NotOwed("delivered")], held=[], hold=hold,
        closed=["job-2"],
    )

    assert reports == []
    close.assert_awaited_once_with(["job-2"])


@pytest.mark.asyncio
async def test_close_failure_keeps_the_reports_already_held():
    """The hold has committed; dropping its reports here would park them
    until the turn ends, unseen by the turn that holds them."""
    close = AsyncMock(side_effect=RuntimeError("db down"))

    reports, *_ = await _take(
        [_job(1), _job(2)], [_report(1), NotOwed("delivered")], held=["job-1"],
        close=close,
    )

    assert reports == [_report(1)]


_OVERTAKING = "src.server.services.report_back.handoff.executor.overtaking_run"


def _held_analyst(n: int, run_id: str) -> dict:
    return {
        "hook_outbox_id": f"job-{n}",
        "hook_type": "report_back",
        "run_id": run_id,
        "payload": {"ptc_thread_id": f"ptc-{n}"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("release_fails", [False, True])
async def test_a_report_a_newer_run_took_over_after_the_read_is_given_back(
    release_fails,
):
    # job-1's analyst thread started a newer run between the offer's read and
    # the hold; job-2's did not.
    jobs = [_held_analyst(1, "run-old"), _held_analyst(2, "run-2")]
    newer = {"ptc-1": "run-newer", "ptc-2": None}
    overtaking = AsyncMock(side_effect=lambda _run, ptc: newer[ptc])
    release = AsyncMock(
        side_effect=RuntimeError("db") if release_fails else None,
        return_value=["job-1"],
    )
    with (
        patch(_OVERTAKING, new=overtaking),
        patch(f"{_OUTBOX}.release_steered_report_jobs", new=release),
    ):
        reports, *_ = await _take(
            jobs, [_report(1), _report(2)], held=["job-1", "job-2"]
        )

    release.assert_awaited_once_with(["job-1"], run_id="run-A")
    # A failed release still keeps it out: the run's finalize releases it.
    assert reports == [_report(2)]


@pytest.mark.asyncio
async def test_a_held_report_whose_thread_cannot_be_read_again_is_given_back():
    release = AsyncMock(return_value=["job-1"])
    with (
        patch(_OVERTAKING, new=AsyncMock(side_effect=RuntimeError("db"))),
        patch(f"{_OUTBOX}.release_steered_report_jobs", new=release),
    ):
        reports, *_ = await _take(
            [_held_analyst(1, "run-old")], [_report(1)], held=["job-1"]
        )

    release.assert_awaited_once_with(["job-1"], run_id="run-A")
    assert reports == []


@pytest.mark.asyncio
async def test_confirm_closes_the_runs_held_reports_and_never_raises():
    # No alert among them: nothing to stamp ahead of the close.
    no_alerts = patch(f"{_OUTBOX}.held_alert_jobs", new=AsyncMock(return_value=[]))
    confirm = AsyncMock(return_value=["job-1"])
    with no_alerts, patch(f"{_OUTBOX}.confirm_jobs", new=confirm):
        await steer.SteeredReportInbox().confirm("run-A", ["job-1"])
    confirm.assert_awaited_once_with(["job-1"], owner=("run", "run-A"))

    with no_alerts, patch(
        f"{_OUTBOX}.confirm_jobs",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        await steer.SteeredReportInbox().confirm("run-A", ["job-1"])


# ---------------------------------------------------------------------------
# _task_offer: the executor's verdict, behind the steer's own pre-checks
# ---------------------------------------------------------------------------

_SR = "src.server.database.runs.subagent_runs.get_task_run"
_TL = "src.server.database.runs.lifecycle.get_run"
_STOPPED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _task_job(**payload) -> dict:
    base = {
        "task_run_id": "tr-1",
        "task_id": "abc123",
        "display_id": "Task-abc123",
        "subagent_type": "research",
        "description": "look things up",
    }
    base.update(payload)
    return {"hook_outbox_id": "job-1", "hook_type": "task_report_back", "payload": base}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run_row",
    [None, {"result_delivered_at": "2026-01-01T00:00:00Z"}],
    ids=["run-row-gone", "result-delivered"],
)
async def test_task_offer_closes_a_report_owed_nothing(run_row):
    with patch(_SR, new=AsyncMock(return_value=run_row)):
        assert isinstance(await steer._task_offer(_task_job()), NotOwed)


@pytest.mark.asyncio
async def test_task_offer_closes_a_report_its_stopped_turn_covered():
    run_row = {
        "result_delivered_at": None,
        "parent_run_id": "run-P",
        "finalized_at": _STOPPED_AT - timedelta(seconds=30),
    }
    parent = {"status": "cancelled", "cancel_requested_at": _STOPPED_AT}
    with (
        patch(_SR, new=AsyncMock(return_value=run_row)),
        patch(_TL, new=AsyncMock(return_value=parent)) as get_run,
    ):
        assert isinstance(await steer._task_offer(_task_job()), NotOwed)
    get_run.assert_awaited_once_with("run-P")


@pytest.mark.asyncio
async def test_task_offer_leaves_an_unlabelled_job_to_the_executor():
    assert await steer._task_offer(_task_job(task_run_id=None)) is None
    assert await steer._task_offer(_task_job(task_id=None)) is None


@pytest.mark.asyncio
async def test_task_offer_leaves_a_report_whose_turn_was_admitted(_no_admitted_turn):
    _no_admitted_turn.return_value = {"conversation_response_id": "run-R"}
    task_run = AsyncMock()
    with patch(_SR, new=task_run):
        assert await steer._task_offer(_task_job()) is None
    task_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_offer_steers_an_owed_report():
    run_row = {"result_delivered_at": None, "parent_run_id": None}
    with patch(_SR, new=AsyncMock(return_value=run_row)):
        offer = await steer._task_offer(_task_job())

    assert offer.job_id == "job-1"
    # DALTON-HOOK(i18n): task_report_message includes system-language notice
    assert offer.content == (
        "<system>\n"
        "Background subagent Task-abc123 (research) finished while you were "
        "working.\n"
        "Task description: look things up\n"
        "This notice is from the system, not the user. Keep writing to the user in the language of their request.\n"
        "</system>"
    )
    assert offer.shown["report_source"]["kind"] == "task"


def test_task_report_message_includes_language_notice_and_preserves_fields():
    from src.server.models.chat import TaskReport
    from src.server.services.report_back.subagent import task_report_message

    report = TaskReport(
        kind="task",
        task_id="abc123",
        display_id="Task-abc123",
        description="collect Cloud segment revenue",
        failed=False,
    )
    msg_posted = task_report_message(report, "dalton-evidence", steered=False)
    assert msg_posted == (
        "<system>\n"
        "Background subagent Task-abc123 (dalton-evidence) finished.\n"
        "Task description: collect Cloud segment revenue\n"
        "This notice is from the system, not the user. Keep writing to the user in the language of their request.\n"
        "</system>"
    )

    msg_steered = task_report_message(report, "dalton-evidence", steered=True)
    assert (
        "Background subagent Task-abc123 (dalton-evidence) finished while you were working.\n"
        in msg_steered
    )
    assert "Task description: collect Cloud segment revenue\n" in msg_steered
    assert (
        "This notice is from the system, not the user. Keep writing to the user in the language of their request.\n"
        in msg_steered
    )
    assert msg_steered.startswith("<system>\n") and msg_steered.endswith("\n</system>")


# ---------------------------------------------------------------------------
# _analyst_offer
# ---------------------------------------------------------------------------

_CACHE = "src.utils.cache.redis_cache.get_cache_client"
_DELIVERIES = "src.server.services.report_back.handoff.requested_from.deliveries"


def _analyst_cache(origin: dict, *, member: bool = True) -> MagicMock:
    cache = MagicMock()
    cache.get_strict = AsyncMock(
        side_effect=lambda key: None if key.startswith("handoff_rb_run:") else origin
    )
    cache.client.sismember = AsyncMock(return_value=member)
    return cache


def _analyst_job(**over) -> dict:
    return {
        "hook_outbox_id": "job-1",
        "hook_type": "report_back",
        "payload": {"ptc_thread_id": "ptc-1", "final_status": "completed"},
        **over,
    }


def _origin(**over) -> dict:
    base = {
        "origin": "home",
        "report_back": True,
        "home_thread_id": "t-1",
        "user_id": "u-1",
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_analyst_offer_steers_an_owed_report():
    with patch(_CACHE, return_value=_analyst_cache(_origin())):
        offer = await steer._analyst_offer(_analyst_job(), "t-1")

    assert offer.job_id == "job-1"
    # DALTON-HOOK(i18n): Analyst report_message carries language-preservation notice
    assert offer.content == (
        "<system>\n"
        "The analysis you dispatched (thread ptc-1 in a workspace) has completed.\n"
        "This notice is from the system, not the user. Keep writing to the user in the language of their request.\n"
        "</system>"
    )
    assert offer.shown["report_source"] == {"kind": "analyst", "thread_id": "ptc-1"}


@pytest.mark.asyncio
async def test_analyst_offer_leaves_automation_reports_to_the_executor():
    with (
        patch(_CACHE, return_value=_analyst_cache(_origin())),
        patch(_DELIVERIES, return_value=[object()]),
    ):
        assert await steer._analyst_offer(_analyst_job(), "t-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin", "member"),
    [(_origin(home_thread_id="t-other"), True), (_origin(), False), (None, True)],
    ids=["owed-to-another-thread", "pair-already-cleared", "origin-gone"],
)
async def test_analyst_offer_leaves_what_is_not_owed_here(origin, member):
    with patch(_CACHE, return_value=_analyst_cache(origin, member=member)):
        assert await steer._analyst_offer(_analyst_job(), "t-1") is None


@pytest.mark.asyncio
async def test_analyst_offer_leaves_an_outcome_it_has_no_words_for():
    job = _analyst_job(payload={"ptc_thread_id": "ptc-1", "final_status": "weird"})
    with patch(_CACHE, return_value=_analyst_cache(_origin())):
        assert await steer._analyst_offer(job, "t-1") is None


@pytest.mark.asyncio
async def test_analyst_offer_leaves_a_report_whose_summary_run_started(_no_admitted_turn):
    _no_admitted_turn.return_value = {"conversation_response_id": "run-R"}
    with patch(_CACHE, return_value=_analyst_cache(_origin())):
        assert await steer._analyst_offer(_analyst_job(), "t-1") is None


@pytest.mark.asyncio
async def test_analyst_offer_leaves_a_report_its_executor_is_posting():
    from src.server.services.report_back.handoff.executor import analyst_request_key

    cache = _analyst_cache(_origin())
    cache.get_strict = AsyncMock(
        side_effect=lambda key: (
            {"request_key": analyst_request_key("job-1")}
            if key.startswith("handoff_rb_run:")
            else _origin()
        )
    )
    with patch(_CACHE, return_value=cache):
        assert await steer._analyst_offer(_analyst_job(), "t-1") is None


@pytest.mark.asyncio
async def test_analyst_offer_leaves_a_report_a_newer_run_took_over():
    latest = AsyncMock(return_value={"conversation_response_id": "run-newer"})
    with (
        patch(_CACHE, return_value=_analyst_cache(_origin())),
        patch("src.server.database.runs.lifecycle.get_latest_attempt", new=latest),
    ):
        assert await steer._analyst_offer(_analyst_job(run_id="run-old"), "t-1") is None
    latest.assert_awaited_once_with("ptc-1")


@pytest.mark.asyncio
async def test_analyst_offer_carries_where_the_work_was_asked_for_inline():
    """Its own turn sends the reminder as additional context; a steer has no
    such field, so the message carries it as the request path renders it."""
    from src.server.models.additional_context import DirectiveContext
    from src.server.utils.directive_context import build_directive_reminder

    with (
        patch(_CACHE, return_value=_analyst_cache(_origin())),
        patch(
            "src.server.services.report_back.handoff.requested_from.reminder",
            return_value="Asked in a Slack conversation.",
        ),
    ):
        offer = await steer._analyst_offer(_analyst_job(), "t-1")

    assert offer.content == (
        "<system>\n"
        "The analysis you dispatched (thread ptc-1 in a workspace) has completed.\n"
        "This notice is from the system, not the user. Keep writing to the user in the language of their request.\n"
        "</system>"
    ) + build_directive_reminder(
        [DirectiveContext(content="Asked in a Slack conversation.")]
    )


# DALTON-HOOK(i18n): system notices (subagent and analyst handoff) carry the keep-the-user-language line.
@pytest.mark.asyncio
async def test_analyst_report_message_includes_language_notice():
    from src.server.services.report_back.handoff import executor

    with patch.object(
        executor, "_workspace_name", autospec=True, return_value="GOOGL"
    ):
        msg, source = await executor.report_message(
            "ptc-99",
            {"ptc_workspace_id": "ws-googl"},
            "completed",
            steered=False,
        )
    assert msg == (
        "<system>\n"
        'The analysis you dispatched (thread ptc-99 in the "GOOGL" workspace) has completed.\n'
        "This notice is from the system, not the user. Keep writing to the user in the language of their request.\n"
        "</system>"
    )
    assert source.kind == "analyst"
    assert source.thread_id == "ptc-99"
    assert source.workspace_name == "GOOGL"


# DALTON-HOOK(handoff-keep-on-resumable): steered worker_lost failure stamps
# awaiting_resume on ptc_origin at offer time so even if the resumed run finishes
# before the old steered_clear executes, the handoff pair is kept and reports back.
@pytest.mark.asyncio
async def test_worker_lost_steered_delivery_resumed_completes_before_old_watch_clear_executes():
    from contextlib import asynccontextmanager
    from unittest.mock import create_autospec

    from src.server.database.runs import lifecycle as tl_db
    from src.server.database.runs import outbox as outbox_db
    from src.server.services.report_back import notify_turn
    from src.server.services.report_back.handoff import core, executor
    from src.server.services.report_back.handoff.keys import (
        ORIGIN_KIND,
        ORIGIN_THREAD_FIELD,
        ptc_origin_key,
        user_pending_key,
        watch_key,
    )
    from src.utils.cache import redis_cache
    from tests.unit.server.handlers.chat.redis_fakes import FakeCache

    cache = FakeCache()
    cache.kv[ptc_origin_key("ptc-dash")] = {
        "origin": ORIGIN_KIND,
        "report_back": True,
        ORIGIN_THREAD_FIELD: "t-1",
        "user_id": "u-1",
        "dispatch_gen": "gen-1",
        "ptc_workspace_id": "ws-dash",
    }
    cache.client.sets[watch_key("t-1")] = {"ptc-dash"}
    cache.client.sets[user_pending_key("u-1")] = {"ptc-dash"}

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

    # DALTON-HOOK(handoff-keep-on-resumable): all test doubles use create_autospec
    get_cache_mock = create_autospec(
        redis_cache.get_cache_client, return_value=cache
    )
    get_run_mock = create_autospec(tl_db.get_run, side_effect=_fake_get_run)
    get_latest_mock = create_autospec(
        tl_db.get_latest_attempt, side_effect=_fake_latest_attempt
    )
    ws_name_mock = create_autospec(executor._workspace_name, return_value="DASH")
    post_mock = create_autospec(
        notify_turn.post_notification_turn,
        return_value=("dispatched", "rb-run-1"),
    )
    await_terminal_mock = create_autospec(
        notify_turn.await_run_terminal, return_value="terminal"
    )
    extend_mock = create_autospec(outbox_db.extend_job_lease, return_value=True)
    merge_mock = create_autospec(outbox_db.merge_job_payload, return_value=True)

    @asynccontextmanager
    async def _owned_guard(job_id: str, attempts: int, *, lease_seconds: int = 60):
        yield True

    guard_mock = create_autospec(
        outbox_db.fenced_job_guard, side_effect=_owned_guard
    )

    # DALTON-HOOK(handoff-keep-on-resumable): steer delivery of round-0 error + round-1 completion
    with (
        patch.object(redis_cache, "get_cache_client", get_cache_mock),
        patch.object(tl_db, "get_run", get_run_mock),
        patch.object(tl_db, "get_latest_attempt", get_latest_mock),
        patch.object(executor, "_workspace_name", ws_name_mock),
        patch.object(notify_turn, "post_notification_turn", post_mock),
        patch.object(notify_turn, "await_run_terminal", await_terminal_mock),
        patch.object(outbox_db, "extend_job_lease", extend_mock),
        patch.object(outbox_db, "merge_job_payload", merge_mock),
        patch.object(outbox_db, "fenced_job_guard", guard_mock),
    ):
        err_job = {
            "hook_outbox_id": "job-err-0",
            "hook_type": "report_back",
            "run_id": "ptc-run-0",
            "attempts": 1,
            "ordering_key": "t-1",
            "payload": {
                "ptc_thread_id": "ptc-dash",
                "dispatch_gen": "gen-1",
                "final_status": "error",
            },
        }
        offer = await steer._analyst_offer(err_job, "t-1")
        assert offer is not None
        assert "failed before it finished" in offer.content
        assert "运行被中断，系统会续跑，完成后会再回报" in offer.content
        origin_after_offer = cache.kv[ptc_origin_key("ptc-dash")]
        assert origin_after_offer["awaiting_resume"]["analyst_run_id"] == "ptc-run-0"
        assert origin_after_offer["awaiting_resume"]["dispatch_gen"] == "gen-1"

        # DALTON-HOOK(handoff-keep-on-resumable): resumed run finishes BEFORE
        # the unmarked steered_clear watch_clear executes.
        analyst_latest.update(runs_by_id["ptc-run-1"])
        latest_calls_before_clear = get_latest_mock.await_count

        await core._exec_watch_clear(
            {
                "hook_outbox_id": "job-err-0:steered_clear",
                "hook_type": "watch_clear",
                "run_id": "ptc-run-0",
                "attempts": 1,
                "ordering_key": "t-1",
                "payload": {
                    "ptc_thread_id": "ptc-dash",
                    "error_wake": False,
                    "dispatch_gen": "gen-1",
                },
            }
        )
        assert get_latest_mock.await_count == latest_calls_before_clear
        assert "ptc-dash" in cache.client.sets.get(watch_key("t-1"), set())
        assert ptc_origin_key("ptc-dash") in cache.kv

        # Round 1: resumed analyst run's report_back revokes awaiting_resume and delivers completion.
        ok_job = {
            "hook_outbox_id": "job-ok-1",
            "hook_type": "report_back",
            "run_id": "ptc-run-1",
            "attempts": 1,
            "ordering_key": "t-1",
            "payload": {
                "ptc_thread_id": "ptc-dash",
                "dispatch_gen": "gen-1",
                "final_status": "completed",
            },
        }
        await core._exec_handoff_report_back(ok_job)
        assert "awaiting_resume" not in cache.kv[ptc_origin_key("ptc-dash")]
        post_mock.assert_awaited_once()
        posted_msg = post_mock.await_args.kwargs["body"]["messages"][0]["content"]
        assert "has completed." in posted_msg
        assert "运行被中断" not in posted_msg
        assert any(
            '"run_id": "rb-run-1"' in payload_str
            for _, payload_str in cache.client.published
        )

        # Final watch_clear after resumed run completion clears the handoff pair.
        await core._exec_watch_clear(
            {
                "hook_outbox_id": "rb-run-1:watch_clear",
                "hook_type": "watch_clear",
                "run_id": "rb-run-1",
                "attempts": 1,
                "ordering_key": "t-1",
                "payload": {
                    "ptc_thread_id": "ptc-dash",
                    "user_id": "u-1",
                    "error_wake": False,
                    "dispatch_gen": "gen-1",
                },
            }
        )
        assert "ptc-dash" not in cache.client.sets.get(watch_key("t-1"), set())
        assert ptc_origin_key("ptc-dash") not in cache.kv


