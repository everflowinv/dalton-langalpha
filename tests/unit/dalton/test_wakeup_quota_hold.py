"""Unit tests for Prod Audit 2 §2: wakeup shared quota deferral & checkpoint resume.

Covers all scenarios in §2.5:
1. Alternating pre-dispatch hold + mid-turn tool exhaustion accumulating on shared ``root_job_id``:
   - #1 pre-dispatch hold -> defer_count=1 -> notify_turn._plan_limited -> hold_alert_for_plan
   - #2 mid-turn exhaustion after step-1 tool -> defer_count=2, persists resume_checkpoint_id &
     enqueues ``wakeup_quota_resume`` in ``hook_outbox``
   - #3 pre-dispatch hold -> defer_count=3
   - #4 exceeds cap (>3) -> ``exhausted_failed`` + Item 3 wakeup notice to ``owner_user_id``
2. Step idempotency (replaying same ``pre:`` or ``mid:`` step key does not increment ``defer_count``).
3. Unknown reset time (``reset_at=None``) schedules ``now + 30m`` with ``held_known=False``.
4. Single cooldown exceeding ``first_due_at + 24h`` immediately rejected -> ``exhausted_failed``.
5. Pure ``auth_unavailable`` fails immediately (zero wait) and notifies ``owner_user_id``.
6. Cross-worker ``execute_wakeup_quota_resume`` resumes from checkpoint or yields ``settled``
   if the user already moved the thread forward.
7. ``notify_turn._plan_limited`` + ``wakeups/delivery.py`` integration & executor registration.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, create_autospec
from uuid import uuid4

import pytest

from src.dalton.llm import unattended_fallback as uf
from src.server.services.report_back import notify_turn
from src.server.services.wakeups import delivery as wakeup_delivery


class _InMemoryDeferralStore:
    """In-memory transactional emulator for ``dalton_wakeup_quota_deferrals`` + ``hook_outbox``."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.outbox: list[dict[str, Any]] = []
        self._outbox_idem: set[str] = set()

    def cursor(self, **kwargs: Any) -> "_InMemoryCursor":
        return _InMemoryCursor(self)


class _InMemoryCursor:
    def __init__(self, store: _InMemoryDeferralStore) -> None:
        self._store = store
        self._result: Any = None
        self.rowcount: int = 0

    async def __aenter__(self) -> "_InMemoryCursor":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        norm = " ".join(sql.split()).upper()
        self.rowcount = 0
        self._result = None
        if norm.startswith("SELECT") and "FROM DALTON_WAKEUP_QUOTA_DEFERRALS" in norm:
            key = str(params[0])
            row = self._store.rows.get(key)
            self._result = dict(row) if row is not None else None
        elif norm.startswith("INSERT INTO DALTON_WAKEUP_QUOTA_DEFERRALS"):
            # Two shapes: pure_auth (11 params, NULL reset) vs cooldown/exhausted (12 params)
            key = str(params[0])
            existing = self._store.rows.get(key)
            if len(params) == 11:
                new_row = {
                    "root_job_id": key,
                    "wakeup_id": params[1],
                    "thread_id": str(params[2]),
                    "owner_user_id": params[3],
                    "first_due_at": params[4],
                    "deadline_at": params[5],
                    "defer_count": params[6],
                    "last_step_key": params[7],
                    "resume_from_run_id": params[8],
                    "resume_checkpoint_id": params[9],
                    "last_reset_at": None,
                    "last_reason": params[10],
                    "status": "exhausted_failed",
                }
            else:
                status = "exhausted_failed" if "'EXHAUSTED_FAILED'" in norm else "deferred"
                new_row = {
                    "root_job_id": key,
                    "wakeup_id": params[1],
                    "thread_id": str(params[2]),
                    "owner_user_id": params[3],
                    "first_due_at": params[4],
                    "deadline_at": params[5],
                    "defer_count": params[6],
                    "last_step_key": params[7],
                    "resume_from_run_id": params[8],
                    "resume_checkpoint_id": params[9],
                    "last_reset_at": params[10],
                    "last_reason": params[11],
                    "status": status,
                }
            if existing is not None:
                merged = dict(existing)
                merged["defer_count"] = new_row["defer_count"]
                merged["last_step_key"] = new_row["last_step_key"]
                merged["resume_from_run_id"] = new_row["resume_from_run_id"] or existing.get("resume_from_run_id")
                merged["resume_checkpoint_id"] = new_row["resume_checkpoint_id"] or existing.get("resume_checkpoint_id")
                merged["last_reset_at"] = new_row["last_reset_at"]
                merged["last_reason"] = new_row["last_reason"]
                merged["status"] = new_row["status"]
                self._store.rows[key] = merged
            else:
                self._store.rows[key] = new_row
            self.rowcount = 1
        elif norm.startswith("INSERT INTO HOOK_OUTBOX"):
            idem = str(params[5])
            if idem not in self._store._outbox_idem:
                self._store._outbox_idem.add(idem)
                raw_payload = params[3]
                payload_obj = getattr(raw_payload, "obj", raw_payload)
                self._store.outbox.append(
                    {
                        "run_id": str(params[0]),
                        "conversation_thread_id": str(params[1]),
                        "hook_type": params[2],
                        "payload": payload_obj,
                        "ordering_key": params[4],
                        "idempotency_key": idem,
                        "next_retry_at": params[6],
                    }
                )
                self.rowcount = 1
        elif norm.startswith("UPDATE DALTON_WAKEUP_QUOTA_DEFERRALS") and "'SETTLED'" in norm:
            key = str(params[0])
            row = self._store.rows.get(key)
            if row and row["status"] == "deferred":
                row["status"] = "settled"
                self.rowcount = 1

    async def fetchone(self) -> Any:
        return self._result


@pytest.mark.asyncio
async def test_wakeup_alternating_pre_and_mid_defers_share_cap_and_notify_on_4th(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _InMemoryDeferralStore()
    notices: list[dict[str, Any]] = []

    async def _capture_notice(conn: Any, **kw: Any) -> bool:
        notices.append(kw)
        return True

    monkeypatch.setattr(uf, "_maybe_enqueue_failure_notice_in_tx", _capture_notice)

    root_job_id = str(uuid4())
    thread_id = str(uuid4())
    t0 = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)

    # 1) Pre-dispatch deferral #1
    d1 = await uf.evaluate_and_record_wakeup_deferral_tx(
        store,
        root_job_id=root_job_id,
        wakeup_id="wake-1",
        thread_id=thread_id,
        owner_user_id="owner-1",
        step_key=f"pre:{root_job_id}:1",
        availabilities=[
            uf.SubscriptionAvailability(
                available=False,
                kind="quota_cooldown",
                refusal_code="plan_limit",
                reset_at=t0 + timedelta(hours=1),
                model_name="claude-opus-5-5-oauth",
            )
        ],
        first_due_at=t0,
        now=t0,
    )
    assert d1.action == "defer"
    assert d1.defer_count == 1
    assert d1.held_known is True
    assert d1.scheduled_wake_at == t0 + timedelta(hours=1, minutes=1)
    assert len(notices) == 0

    # Idempotent replay of step #1 does NOT increment defer_count
    d1_dup = await uf.evaluate_and_record_wakeup_deferral_tx(
        store,
        root_job_id=root_job_id,
        wakeup_id="wake-1",
        thread_id=thread_id,
        owner_user_id="owner-1",
        step_key=f"pre:{root_job_id}:1",
        availabilities=[
            uf.SubscriptionAvailability(
                available=False,
                kind="quota_cooldown",
                refusal_code="plan_limit",
                reset_at=t0 + timedelta(hours=1),
                model_name="claude-opus-5-5-oauth",
            )
        ],
        first_due_at=t0,
        now=t0,
    )
    assert d1_dup.idempotent_replay is True
    assert d1_dup.defer_count == 1

    # 2) Mid-turn exhaustion after step-1 tool -> defer_count=2 + wakeup_quota_resume job
    run1_id = str(uuid4())
    d2 = await uf.record_wakeup_mid_turn_quota_exhaustion_tx(
        store,
        thread_id=thread_id,
        run_id=run1_id,
        owner_user_id="owner-1",
        root_job_id=root_job_id,
        wakeup_id="wake-1",
        turn_index=2,
        availabilities=[
            uf.SubscriptionAvailability(
                available=False,
                kind="quota_cooldown",
                refusal_code="plan_limit",
                reset_at=t0 + timedelta(hours=3),
                model_name="claude-opus-5-5-oauth",
            )
        ],
        latest_checkpoint_id="ckpt-after-tool-1",
        first_due_at=t0,
        now=t0 + timedelta(hours=2),
    )
    assert d2.action == "defer"
    assert d2.defer_count == 2
    assert d2.resume_checkpoint_id == "ckpt-after-tool-1"
    assert len(store.outbox) == 1
    assert store.outbox[0]["hook_type"] == "wakeup_quota_resume"
    assert store.outbox[0]["idempotency_key"] == f"wakeup_resume:{root_job_id}:2"
    assert store.outbox[0]["payload"]["resume_checkpoint_id"] == "ckpt-after-tool-1"

    # 3) Pre-dispatch deferral #3 -> defer_count=3
    d3 = await uf.evaluate_and_record_wakeup_deferral_tx(
        store,
        root_job_id=root_job_id,
        wakeup_id="wake-1",
        thread_id=thread_id,
        owner_user_id="owner-1",
        step_key=f"pre:{root_job_id}:2",
        availabilities=[
            uf.SubscriptionAvailability(
                available=False,
                kind="quota_cooldown",
                refusal_code="plan_limit",
                reset_at=t0 + timedelta(hours=5),
                model_name="claude-opus-5-5-oauth",
            )
        ],
        first_due_at=t0,
        now=t0 + timedelta(hours=4),
    )
    assert d3.action == "defer"
    assert d3.defer_count == 3
    assert len(notices) == 0

    # 4) 4th attempt hits defer_count + 1 > 3 -> exhausted_failed + wakeup notice to owner
    d4 = await uf.evaluate_and_record_wakeup_deferral_tx(
        store,
        root_job_id=root_job_id,
        wakeup_id="wake-1",
        thread_id=thread_id,
        owner_user_id="owner-1",
        step_key=f"pre:{root_job_id}:3",
        availabilities=[
            uf.SubscriptionAvailability(
                available=False,
                kind="quota_cooldown",
                refusal_code="plan_limit",
                reset_at=t0 + timedelta(hours=7),
                model_name="claude-opus-5-5-oauth",
            )
        ],
        first_due_at=t0,
        now=t0 + timedelta(hours=6),
    )
    assert d4.action == "exhausted_failed"
    assert d4.defer_count == 3
    assert d4.reason == "wakeup_quota_defer_limit_exceeded"
    assert store.rows[root_job_id]["status"] == "exhausted_failed"
    assert len(notices) == 1
    assert notices[0]["subject_kind"] == "wakeup"
    assert notices[0]["recipient_user_id"] == "owner-1"


@pytest.mark.asyncio
async def test_wakeup_unknown_reset_uses_30m_and_24h_overflow_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _InMemoryDeferralStore()
    notices: list[dict[str, Any]] = []

    async def _capture_notice(conn: Any, **kw: Any) -> bool:
        notices.append(kw)
        return True

    monkeypatch.setattr(uf, "_maybe_enqueue_failure_notice_in_tx", _capture_notice)

    t0 = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)
    # Unknown reset_at=None -> effective_reset_at = now + 30m, held_known = False
    r_unknown = str(uuid4())
    dec_unknown = await uf.evaluate_and_record_wakeup_deferral_tx(
        store,
        root_job_id=r_unknown,
        wakeup_id="wake-unknown",
        thread_id=str(uuid4()),
        owner_user_id="owner-2",
        step_key="pre:u:1",
        availabilities=[
            uf.SubscriptionAvailability(
                available=False,
                kind="quota_cooldown",
                refusal_code="plan_limit",
                reset_at=None,
                model_name="claude-opus-5-5-oauth",
            )
        ],
        first_due_at=t0,
        now=t0,
    )
    assert dec_unknown.action == "defer"
    assert dec_unknown.held_known is False
    assert dec_unknown.effective_reset_at == t0 + timedelta(minutes=30)
    assert dec_unknown.scheduled_wake_at == t0 + timedelta(minutes=31)
    exc = uf.build_wakeup_cooldown_http_exception(dec_unknown)
    assert exc.status_code == 400
    assert exc.detail["type"] == "dalton_subscription_cooldown"
    assert exc.detail["held_known"] is False

    # Single 7-day cooldown exceeding first_due_at + 24h -> immediate exhausted_failed + notice
    r_long = str(uuid4())
    dec_long = await uf.evaluate_and_record_wakeup_deferral_tx(
        store,
        root_job_id=r_long,
        wakeup_id="wake-long",
        thread_id=str(uuid4()),
        owner_user_id="owner-3",
        step_key="pre:l:1",
        availabilities=[
            uf.SubscriptionAvailability(
                available=False,
                kind="quota_cooldown",
                refusal_code="plan_limit",
                reset_at=t0 + timedelta(days=7),
                model_name="claude-opus-5-5-oauth",
            )
        ],
        first_due_at=t0,
        now=t0,
    )
    assert dec_long.action == "exhausted_failed"
    assert dec_long.reason == "wakeup_quota_deadline_exceeded"
    assert len(notices) == 1
    assert notices[0]["recipient_user_id"] == "owner-3"
    assert notices[0]["subject_kind"] == "wakeup"


@pytest.mark.asyncio
async def test_wakeup_pure_auth_unavailable_fails_immediately_with_zero_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _InMemoryDeferralStore()
    notices: list[dict[str, Any]] = []

    async def _capture_notice(conn: Any, **kw: Any) -> bool:
        notices.append(kw)
        return True

    monkeypatch.setattr(uf, "_maybe_enqueue_failure_notice_in_tx", _capture_notice)

    t0 = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)
    root_id = str(uuid4())
    dec = await uf.evaluate_and_record_wakeup_deferral_tx(
        store,
        root_job_id=root_id,
        wakeup_id="wake-auth",
        thread_id=str(uuid4()),
        owner_user_id="owner-auth",
        step_key="pre:a:1",
        availabilities=[
            uf.SubscriptionAvailability(
                available=False,
                kind="auth_unavailable",
                refusal_code="oauth_reauth_required",
                model_name="claude-opus-5-5-oauth",
            ),
            uf.SubscriptionAvailability(
                available=False,
                kind="auth_unavailable",
                refusal_code="oauth_reauth_required",
                model_name="gpt-6.2-sol-oauth",
            ),
        ],
        first_due_at=t0,
        now=t0,
    )
    assert dec.action == "exhausted_failed"
    assert dec.reason == "oauth_auth_unavailable"
    assert dec.defer_count == 0
    assert dec.scheduled_wake_at is None
    assert len(notices) == 1
    assert notices[0]["failure_reason"] == "oauth_auth_unavailable"
    assert notices[0]["recipient_user_id"] == "owner-auth"


@pytest.mark.asyncio
async def test_notify_turn_plan_limited_and_wakeup_resume_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. _plan_limited recognizes dalton_subscription_cooldown (with held_known) & dalton_wakeup_quota_exhausted
    class _FakeResp:
        def __init__(self, status: int, body: dict[str, Any]) -> None:
            self.status = status
            self._body = body

        async def json(self) -> dict[str, Any]:
            return self._body

    detail_cool = {
        "type": "dalton_subscription_cooldown",
        "reason": "plan_limited",
        "held_until": "2026-11-27T14:00:00+00:00",
        "held_known": False,
    }
    parsed = await notify_turn._plan_limited(_FakeResp(400, {"detail": detail_cool}))
    assert parsed == detail_cool

    detail_exh = {"type": "dalton_wakeup_quota_exhausted", "reason": "wakeup_quota_defer_limit_exceeded"}
    parsed_exh = await notify_turn._plan_limited(_FakeResp(400, {"detail": detail_exh}))
    assert parsed_exh == detail_exh

    # 2. execute_wakeup_quota_resume dispatches /retry when thread still at failed_run_id
    from src.server.database.runs import lifecycle as tl_db
    from src.server.services.runs import pending_input

    root_id = str(uuid4())
    thread_id = str(uuid4())
    failed_run_id = str(uuid4())

    monkeypatch.setattr(
        tl_db,
        "get_latest_attempt",
        AsyncMock(return_value={"conversation_response_id": failed_run_id, "status": "error"}),
    )
    monkeypatch.setattr(
        pending_input,
        "retried_wake_fields",
        AsyncMock(return_value={"query_type": "wakeup", "wakeup": {"v": 1, "notices": []}}),
    )
    post_mock = create_autospec(notify_turn.post_notification_turn, instance=False)
    post_mock.return_value = ("dispatched", "new-run-1")
    monkeypatch.setattr(notify_turn, "post_notification_turn", post_mock)

    await uf.execute_wakeup_quota_resume(
        {
            "hook_outbox_id": str(uuid4()),
            "conversation_thread_id": thread_id,
            "run_id": failed_run_id,
            "payload": {
                "root_job_id": root_id,
                "wakeup_id": "wake-1",
                "owner_user_id": "owner-1",
                "failed_run_id": failed_run_id,
                "turn_index": 3,
                "resume_checkpoint_id": "ckpt-step-1",
                "defer_count": 2,
                "request_key": f"wakeup_resume:{root_id}:2",
            },
        }
    )
    assert post_mock.await_count == 1
    call_kw = post_mock.await_args.kwargs
    assert call_kw["route"] == "retry"
    assert call_kw["body"]["checkpoint_id"] == "ckpt-step-1"
    assert call_kw["body"]["run_id"] == failed_run_id
    assert call_kw["body"]["request_key"] == f"wakeup_resume:{root_id}:2"

    # 3. If user already sent a newer turn on the thread, executor marks deferral settled and yields
    settled_ids: list[str] = []

    async def _fake_settled(rid: str, **kw: Any) -> bool:
        settled_ids.append(rid)
        return True

    monkeypatch.setattr(uf, "mark_wakeup_deferral_settled", _fake_settled)
    monkeypatch.setattr(
        tl_db,
        "get_latest_attempt",
        AsyncMock(return_value={"conversation_response_id": str(uuid4()), "status": "completed"}),
    )
    post_mock.reset_mock()
    await uf.execute_wakeup_quota_resume(
        {
            "hook_outbox_id": str(uuid4()),
            "conversation_thread_id": thread_id,
            "run_id": failed_run_id,
            "payload": {
                "root_job_id": root_id,
                "wakeup_id": "wake-1",
                "owner_user_id": "owner-1",
                "failed_run_id": failed_run_id,
                "resume_checkpoint_id": "ckpt-step-1",
                "defer_count": 2,
            },
        }
    )
    assert post_mock.await_count == 0
    assert settled_ids == [root_id]


def test_register_wakeup_executors_registers_resume_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.server.services import hook_outbox

    registered: dict[str, Any] = {}
    monkeypatch.setattr(
        hook_outbox,
        "register_hook_executor",
        lambda hook_type, fn: registered.__setitem__(hook_type, fn),
    )
    wakeup_delivery.register_wakeup_executors()
    assert "wakeup" in registered
    assert uf.WAKEUP_QUOTA_RESUME_HOOK in registered
    assert registered[uf.WAKEUP_QUOTA_RESUME_HOOK] is uf.execute_wakeup_quota_resume
