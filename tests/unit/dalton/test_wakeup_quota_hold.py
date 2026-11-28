"""Unit tests for Prod Audit 2 §2: wakeup shared quota deferral & checkpoint resume.

Exercises real call chains without faking side effects or bypassing Pydantic/executor/delivery seams:
1. Alternating pre-dispatch hold + mid-turn tool exhaustion sharing ``root_job_id`` cap (3 defers / 24h)
   and persisted ``seen_step_keys`` idempotency (even for non-last earlier step keys).
2. Real end-to-end 4th pre-dispatch cooldown on the same wakeup:
   ``delivery._deliver`` -> ``post_notification_turn`` -> ``automation_thread_model`` ->
   ``evaluate_and_record_wakeup_deferral`` -> ``build_wakeup_cooldown_http_exception`` ->
   ``notify_turn._plan_limited`` (NO ``KeyError`` on missing ``held_until``!) -> ``delivery._deliver``
   returns ``"release"`` and settles cleanly on the 4th cooldown.
3. Real mid-turn exhaustion -> ``LocalRunExecutor._finalize_run`` -> ``finalization.assemble_finalize_artifacts``
   -> ``RunCoordinator.finalize_run`` -> ``lifecycle.finalize_run`` atomic transaction registers
   ``wakeup_quota_resume`` in ``hook_outbox`` with committed checkpoint ID -> ``execute_wakeup_quota_resume``
   builds a real ``RetryRequest``-validated body (with ``workspace_id``, UUID ``request_key``,
   ``wakeup_resume`` carrying original ``root_job_id``, incrementing ``attempts``, constant ``first_due_at``)
   and resumes a real LangGraph ``StateGraph`` + ``MemorySaver`` checkpoint so step-1 tool executes STRICTLY ONCE.
4. Resume pre-dispatch cooldown re-checks 24h deadline, uses distinct step keys per attempt, reschedules
   with ``AND attempts = %s`` fence, and settles cleanly on ``dalton_wakeup_quota_exhausted``.
5. Unknown reset (30m, ``held_known=False``), 24h overflow, pure ``auth_unavailable`` zero-wait settlement,
   and user-advanced thread yielding ``settled``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, TypedDict
from unittest.mock import create_autospec
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from src.dalton.llm import unattended_fallback as uf
from src.dalton.services import automation_thread_model as auto_tm_mod
from src.llms.provider_errors import ProviderRefusal
from src.ptc_agent.agent.middleware.model_resilience import _RemoteQuotaExhaustedError
from src.server.database import conversation as conv_db
from src.server.database.runs import lifecycle as tl_db
from src.server.database.runs import outbox as outbox_db
from src.server.database.wakeups import fire as fire_db
from src.server.database.wakeups import riders as riders_db
from src.server.models.workflow import RetryRequest
from src.server.services.llm import user_models
from src.server.services.report_back import notify_turn
from src.server.services.runs import finalization as finalization_mod
from src.server.services.runs.executor import LocalRunExecution, LocalRunExecutor, LocalRunStatus
from src.server.services.wakeups import delivery as wakeup_delivery


class _InMemoryDeferralStore:
    """In-memory transactional emulator for ``dalton_wakeup_quota_deferrals`` + ``hook_outbox``."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.outbox: list[dict[str, Any]] = []
        self._outbox_idem: set[str] = set()
        self.advisory_locks: list[int] = []
        self.reschedule_queries: list[tuple[str, Any]] = []
        self.responses: list[dict[str, Any]] = []

    @asynccontextmanager
    async def transaction(self):
        yield self

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
        if "PG_ADVISORY_XACT_LOCK" in norm:
            self._store.advisory_locks.append(int(params[0]))
            return
        if norm.startswith("SELECT") and "FROM DALTON_WAKEUP_QUOTA_DEFERRALS" in norm:
            key = str(params[0])
            row = self._store.rows.get(key)
            self._result = dict(row) if row is not None else None
        elif norm.startswith("SELECT 1 FROM CONVERSATION_RESPONSES"):
            tid, req_key, failed_id = str(params[0]), str(params[1]), str(params[2])
            matched = any(
                str(r.get("conversation_thread_id") or "") == tid
                and str(r.get("request_key") or "") == req_key
                and str(r.get("retry_of_run_id") or "") == failed_id
                for r in self._store.responses
            )
            self._result = (1,) if matched else None
        elif norm.startswith("INSERT INTO DALTON_WAKEUP_QUOTA_DEFERRALS"):
            key = str(params[0])
            existing = self._store.rows.get(key)
            raw_seen = getattr(params[8], "obj", params[8])
            if len(params) == 12:
                # pure_auth (NULL last_reset_at)
                new_row = {
                    "root_job_id": key,
                    "wakeup_id": params[1],
                    "thread_id": str(params[2]),
                    "owner_user_id": params[3],
                    "first_due_at": params[4],
                    "deadline_at": params[5],
                    "defer_count": params[6],
                    "last_step_key": params[7],
                    "seen_step_keys": list(raw_seen),
                    "resume_from_run_id": params[9],
                    "resume_checkpoint_id": params[10],
                    "last_reset_at": None,
                    "last_reason": params[11],
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
                    "seen_step_keys": list(raw_seen),
                    "resume_from_run_id": params[9],
                    "resume_checkpoint_id": params[10],
                    "last_reset_at": params[11],
                    "last_reason": params[12],
                    "status": status,
                }
            if existing is not None:
                merged = dict(existing)
                merged["defer_count"] = new_row["defer_count"]
                merged["last_step_key"] = new_row["last_step_key"]
                merged["seen_step_keys"] = list(new_row["seen_step_keys"])
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
                        "hook_outbox_id": str(uuid4()),
                        "run_id": str(params[0]),
                        "conversation_thread_id": str(params[1]),
                        "hook_type": params[2],
                        "payload": payload_obj,
                        "ordering_key": params[4],
                        "idempotency_key": idem,
                        "next_retry_at": params[6],
                        "attempts": 1,
                        "status": "pending",
                    }
                )
                self.rowcount = 1
        elif norm.startswith("UPDATE DALTON_WAKEUP_QUOTA_DEFERRALS"):
            key = str(params[0])
            row = self._store.rows.get(key)
            if row and row["status"] == "deferred":
                if "'SETTLED'" in norm:
                    row["status"] = "settled"
                elif "'EXHAUSTED_FAILED'" in norm:
                    row["status"] = "exhausted_failed"
                    if "'UNATTENDED_QUOTA_FALLBACK_DISABLED'" in norm:
                        row["last_reason"] = "unattended_quota_fallback_disabled"
                    else:
                        row["last_reason"] = "wakeup_quota_deadline_exceeded"
                self.rowcount = 1
                if "RETURNING" in norm:
                    self._result = {
                        "wakeup_id": row.get("wakeup_id"),
                        "owner_user_id": row.get("owner_user_id"),
                        "defer_count": row.get("defer_count", 0),
                    }
        elif norm.startswith("UPDATE HOOK_OUTBOX"):
            self._store.reschedule_queries.append((sql, params))
            self.rowcount = 1

    async def fetchone(self) -> Any:
        return self._result


# ---------------------------------------------------------------------------
# 1. Shared cap across pre/mid defers + full seen_step_keys idempotency + advisory lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wakeup_alternating_pre_and_mid_defers_share_cap_and_seen_step_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _InMemoryDeferralStore()
    notices: list[dict[str, Any]] = []
    notice_mock = create_autospec(uf._maybe_enqueue_failure_notice_in_tx, instance=False)

    async def _capture_notice(
        conn: Any,
        *,
        subject_kind: Any,
        subject_id: str,
        source_key: str,
        recipient_user_id: str,
        failure_reason: str,
        summary: Any,
        dedup_window_key: str | None = None,
    ) -> bool:
        notices.append(
            {
                "subject_kind": subject_kind,
                "subject_id": subject_id,
                "recipient_user_id": recipient_user_id,
                "failure_reason": failure_reason,
                "summary": summary,
            }
        )
        return True

    notice_mock.side_effect = _capture_notice
    monkeypatch.setattr(uf, "_maybe_enqueue_failure_notice_in_tx", notice_mock)

    root_job_id = str(uuid4())
    thread_id = str(uuid4())
    t0 = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)

    # 1) Pre-dispatch deferral #1 -> advisory lock acquired before SELECT FOR UPDATE
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
    assert d1.action == "defer" and d1.defer_count == 1
    assert len(store.advisory_locks) == 1

    # 2) Mid-turn exhaustion -> defer_count=2 + wakeup_quota_resume with valid UUID request_key
    run1_id = str(uuid4())
    d2 = await uf.record_wakeup_mid_turn_quota_exhaustion_tx(
        store,
        thread_id=thread_id,
        run_id=run1_id,
        owner_user_id="owner-1",
        root_job_id=root_job_id,
        wakeup_id="wake-1",
        turn_index=2,
        workspace_id="ws-1",
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
    assert d2.action == "defer" and d2.defer_count == 2
    assert len(store.outbox) == 1
    outbox_payload = store.outbox[0]["payload"]
    assert outbox_payload["workspace_id"] == "ws-1"
    assert outbox_payload["first_due_at"] == t0.isoformat()
    UUID(outbox_payload["request_key"])  # must be valid UUID

    # Delayed out-of-order replay of EARLIER step #1 (NOT last_step_key!) is STILL idempotent!
    d1_late_replay = await uf.evaluate_and_record_wakeup_deferral_tx(
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
        now=t0 + timedelta(hours=2, minutes=1),
    )
    assert d1_late_replay.idempotent_replay is True
    assert d1_late_replay.defer_count == 2

    # 3) Pre-dispatch deferral #3 -> defer_count=3
    d3 = await uf.evaluate_and_record_wakeup_deferral_tx(
        store,
        root_job_id=root_job_id,
        wakeup_id="wake-1",
        thread_id=thread_id,
        owner_user_id="owner-1",
        step_key="pre:resume-job-1:1",
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
    assert d3.action == "defer" and d3.defer_count == 3

    # 4) 4th cooldown exceeds shared cap -> exhausted_failed + notice
    d4 = await uf.evaluate_and_record_wakeup_deferral_tx(
        store,
        root_job_id=root_job_id,
        wakeup_id="wake-1",
        thread_id=thread_id,
        owner_user_id="owner-1",
        step_key="pre:resume-job-1:2",
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
    assert d4.reason == "wakeup_quota_defer_limit_exceeded"
    assert len(notices) == 1
    assert notices[0]["recipient_user_id"] == "owner-1"


# ---------------------------------------------------------------------------
# 2. REAL CHAIN: Same wakeup 4th pre-dispatch cooldown -> HTTP 400 ->
#    notify_turn.post_notification_turn (NO KeyError!) -> delivery._deliver -> "release"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_wakeup_4th_pre_dispatch_cooldown_terminates_without_keyerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real chain across delivery._deliver -> post_notification_turn -> automation_thread_model -> ledger."""
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")

    store = _InMemoryDeferralStore()
    root_job_id = str(uuid4())
    thread_id = str(uuid4())
    ws_id = str(uuid4())
    t0 = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)

    @asynccontextmanager
    async def _conn_ctx(conn: Any = None):
        yield store

    monkeypatch.setattr(uf, "get_db_connection", _conn_ctx)
    monkeypatch.setattr(
        uf,
        "_maybe_enqueue_failure_notice_in_tx",
        create_autospec(uf._maybe_enqueue_failure_notice_in_tx, instance=False, return_value=True),
    )
    monkeypatch.setattr(uf, "get_profile_default_fallback_chain", lambda: [])
    monkeypatch.setattr(
        user_models,
        "get_model_preference",
        create_autospec(
            user_models.get_model_preference,
            instance=False,
            return_value={"default_model": "claude-opus-5-5-oauth", "fallback_models": []},
        ),
    )
    monkeypatch.setattr(
        auto_tm_mod,
        "_resolve_automation_notification_context",
        create_autospec(
            auto_tm_mod._resolve_automation_notification_context,
            instance=False,
            return_value=(None, None, None, False, False, None),
        ),
    )

    async def _always_cooled(
        user_id: str,
        model_name: str,
        *,
        client: Any = None,
        prefs: Any = None,
        ignore_custom_models: bool = False,
        now: Any = None,
    ) -> uf.SubscriptionAvailability:
        return uf.SubscriptionAvailability(
            available=False,
            kind="quota_cooldown",
            refusal_code="plan_limit",
            provider="anthropic-oauth",
            reset_at=t0 + timedelta(hours=1),
            cooldown_key="ck:anthropic",
            model_name=model_name,
        )

    monkeypatch.setattr(uf, "inspect_subscription_availability", _always_cooled)

    # Wire aiohttp POST inside post_notification_turn directly into real automation_thread_model!
    class _FakeHttpResponse:
        def __init__(self, status: int, body: dict[str, Any]) -> None:
            self.status = status
            self._body = body

        async def __aenter__(self) -> "_FakeHttpResponse":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def json(self) -> dict[str, Any]:
            return self._body

        async def text(self) -> str:
            return str(self._body)

    class _FakeHttpSession:
        async def __aenter__(self) -> "_FakeHttpSession":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, Any], timeout: Any) -> _FakeHttpResponse:
            self._pending_json = json
            return _DeferredDispatchResponse(json)

    class _DeferredDispatchResponse(_FakeHttpResponse):
        def __init__(self, req_json: dict[str, Any]) -> None:
            super().__init__(400, {})
            self._req_json = req_json

        async def __aenter__(self) -> "_DeferredDispatchResponse":
            req_obj = SimpleNamespace(
                query_type=self._req_json.get("query_type"),
                platform=None,
                wakeup=self._req_json.get("wakeup"),
                workspace_id=self._req_json.get("workspace_id"),
                request_key=self._req_json.get("request_key"),
                hitl_response=None,
            )
            try:
                await auto_tm_mod.resolve_turn_config_with_automation_inheritance(
                    user_id="owner-1",
                    thread_id=thread_id,
                    request=req_obj,
                    requested_model="claude-opus-5-5-oauth",
                    held_model=None,
                    from_service=True,
                    resolve=create_autospec(lambda m: None, instance=False),
                )
            except HTTPException as exc:
                self.status = exc.status_code
                self._body = {"detail": exc.detail}
            return self

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: _FakeHttpSession())

    # Stub only DB/workspace lookup seams around delivery._deliver
    monkeypatch.setattr(tl_db, "get_latest_attempt", create_autospec(tl_db.get_latest_attempt, instance=False, return_value=None))
    monkeypatch.setattr(fire_db, "job_owed", create_autospec(fire_db.job_owed, instance=False, return_value=True))
    monkeypatch.setattr(wakeup_delivery, "showing_run", create_autospec(wakeup_delivery.showing_run, instance=False, return_value=None))
    monkeypatch.setattr(
        conv_db,
        "get_thread_by_id",
        create_autospec(conv_db.get_thread_by_id, instance=False, return_value={"workspace_id": ws_id, "archived_at": None}),
    )
    from src.server.database import workspace as ws_db

    monkeypatch.setattr(ws_db, "get_workspace", create_autospec(ws_db.get_workspace, instance=False, return_value={"workspace_id": ws_id}))
    from src.server.services import features as features_mod

    monkeypatch.setattr(features_mod, "user_feature_enabled", create_autospec(features_mod.user_feature_enabled, instance=False, return_value=True))
    monkeypatch.setattr(outbox_db, "extend_job_lease", create_autospec(outbox_db.extend_job_lease, instance=False, return_value=True))
    monkeypatch.setattr(notify_turn, "park_unless_ready", create_autospec(notify_turn.park_unless_ready, instance=False, return_value=False))
    monkeypatch.setattr(riders_db, "gather_wakeup_riders", create_autospec(riders_db.gather_wakeup_riders, instance=False, return_value=[]))
    monkeypatch.setattr(
        wakeup_delivery,
        "render_wake",
        create_autospec(
            wakeup_delivery.render_wake,
            instance=False,
            return_value=("wake text", [{"wakeup_id": "wake-1", "notice": "time", "seq": 1}]),
        ),
    )
    hold_mock = create_autospec(outbox_db.hold_alert_for_plan, instance=False, return_value=True)
    monkeypatch.setattr(outbox_db, "hold_alert_for_plan", hold_mock)
    monkeypatch.setattr(outbox_db, "end_plan_hold", create_autospec(outbox_db.end_plan_hold, instance=False, return_value=True))
    monkeypatch.setattr(wakeup_delivery, "_adopt_own_wake", create_autospec(wakeup_delivery._adopt_own_wake, instance=False, return_value=False))

    # Attempts 1, 2, 3 -> held; Attempt 4 -> exhausted_failed -> NO KeyError, delivery returns "release"!
    exits: list[str] = []
    for attempt in (1, 2, 3, 4):
        job_row = {
            "hook_outbox_id": root_job_id,
            "conversation_thread_id": thread_id,
            "attempts": attempt,
            "created_at": t0,
            "payload": {
                "wakeup_id": "wake-1",
                "notice": "time",
                "seq": 1,
                "user_id": "owner-1",
                **({"plan_hold": {"reset_at": (t0 + timedelta(hours=1)).isoformat()}} if attempt > 1 else {}),
            },
        }
        wake = wakeup_delivery._Wake(
            job=job_row,
            thread_id=thread_id,
            wakeup_id="wake-1",
            subject="time 1 of wakeup wake-1",
        )
        exits.append(await wakeup_delivery._deliver(wake))

    assert exits == ["held", "held", "held", "release"]
    assert hold_mock.await_count == 3
    assert store.rows[root_job_id]["status"] == "exhausted_failed"
    assert store.rows[root_job_id]["defer_count"] == 3


# ---------------------------------------------------------------------------
# 3. REAL CHAIN: Mid-turn exhaustion -> LocalRunExecutor._finalize_run ->
#    wakeup_quota_resume -> RetryRequest validation -> LangGraph checkpoint resume
#    with step-1 tool executing STRICTLY ONCE!
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mid_turn_exhaustion_executor_finalize_enqueues_resume_and_replays_checkpoint_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real LocalRunExecutor._finalize_run -> atomic tx -> real RetryRequest -> real LangGraph MemorySaver."""
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    store = _InMemoryDeferralStore()
    root_job_id = str(uuid4())
    thread_id = str(uuid4())
    failed_run_id = str(uuid4())
    ws_id = str(uuid4())
    t0 = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)

    @asynccontextmanager
    async def _conn_ctx(conn: Any = None):
        yield store

    monkeypatch.setattr(uf, "get_db_connection", _conn_ctx)

    # Build a real 2-step LangGraph StateGraph backed by real MemorySaver checkpointer
    class _GraphState(TypedDict):
        step1_done: bool
        answer: str

    tool_side_effect_calls: list[str] = []
    quota_exhausted_flag = {"exhausted": True}

    def _tool_step(state: _GraphState) -> dict[str, Any]:
        tool_side_effect_calls.append("tool-1-ran")
        return {"step1_done": True}

    def _llm_step(state: _GraphState) -> dict[str, Any]:
        if quota_exhausted_flag["exhausted"]:
            raise _RemoteQuotaExhaustedError(
                "Plan limit reached",
                429,
                expires_at=(t0 + timedelta(minutes=20)).timestamp(),
                provider_refusal=ProviderRefusal("plan_limit", until=t0 + timedelta(minutes=20)),
            )
        return {"answer": "completed-after-resume"}

    builder = StateGraph(_GraphState)
    builder.add_node("tool_step", _tool_step)
    builder.add_node("llm_step", _llm_step)
    builder.add_edge(START, "tool_step")
    builder.add_edge("tool_step", "llm_step")
    builder.add_edge("llm_step", END)
    checkpointer = MemorySaver()
    graph = builder.compile(checkpointer=checkpointer)

    # Worker 1 runs graph: tool_step executes ONCE & commits checkpoint, llm_step raises quota exhaustion
    run_cfg = {"configurable": {"thread_id": thread_id}}
    caught_exc: BaseException | None = None
    try:
        await graph.ainvoke({"step1_done": False, "answer": ""}, config=run_cfg)
    except _RemoteQuotaExhaustedError as exc:
        caught_exc = exc

    assert caught_exc is not None
    assert len(tool_side_effect_calls) == 1
    saved_state = await graph.aget_state(run_cfg)
    committed_ckpt_id = saved_state.config["configurable"]["checkpoint_id"]
    assert committed_ckpt_id is not None

    # Drive REAL LocalRunExecutor._finalize_run(kind="failed", exc=caught_exc)
    # and verify atomic finalization calls maybe_record_wakeup_mid_turn_resume_in_tx!
    start_meta = uf.build_wakeup_quota_start_metadata(
        SimpleNamespace(
            query_type="wakeup",
            wakeup={
                "v": 1,
                "root_job_id": root_job_id,
                "wakeup_id": "wake-real-1",
                "first_due_at": t0.isoformat(),
                "notices": [{"wakeup_id": "wake-real-1"}],
            },
            llm_model="claude-opus-5-5-oauth",
        ),
        user_id="owner-1",
        workspace_id=ws_id,
        effective_model="claude-opus-5-5-oauth",
    )
    persisted_run_row = {
        "conversation_response_id": failed_run_id,
        "conversation_thread_id": thread_id,
        "turn_index": 1,
        "status": "error",
        "user_id": "owner-1",
        "metadata": {
            "workspace_id": ws_id,
            "user_id": "owner-1",
            "llm_model": "claude-opus-5-5-oauth",
            **start_meta,
        },
    }

    async def _drive_finalize_in_same_tx(
        key: tuple[str, str], handle: Any, outcome: tl_db.RunOutcome, *, tail_drain: Any = None
    ) -> finalization_mod.FinalizeVerdict:
        assert outcome.status == "error"
        assert outcome.wakeup_quota_failure is not None
        assert outcome.wakeup_quota_failure["refusal_code"] == "plan_limit"
        await uf.maybe_record_wakeup_mid_turn_resume_in_tx(
            store,
            run_row=persisted_run_row,
            thread_id=key[0],
            run_id=key[1],
            newest_checkpoint_id=committed_ckpt_id,
            quota_failure=outcome.wakeup_quota_failure,
        )
        return finalization_mod.FinalizeVerdict(applied=True, status="error")

    monkeypatch.setattr(finalization_mod, "drive_finalize_cas", _drive_finalize_in_same_tx)
    monkeypatch.setattr(
        finalization_mod,
        "_delivered_steering_inputs",
        create_autospec(finalization_mod._delivered_steering_inputs, instance=False, return_value=[]),
    )

    executor = LocalRunExecutor.__new__(LocalRunExecutor)
    import asyncio

    executor.task_lock = asyncio.Lock()
    executor.executions = {}
    executor.event_storage_backend = "memory"
    executor._orphan_collectors = {}
    executor._shielded_tasks = set()
    monkeypatch.setattr(executor, "append_run_end_event", create_autospec(executor.append_run_end_event, instance=False))
    monkeypatch.setattr(executor, "_note_credential_refusal", create_autospec(executor._note_credential_refusal, instance=False))

    fake_handler = SimpleNamespace(
        agent_config=SimpleNamespace(
            llm=SimpleNamespace(name="claude-opus-5-5-oauth"),
            fallback_llm_clients=[],
            feature_enabled=lambda name: False,
        ),
        saw_interrupt=False,
        captured_images={},
        captured_images_by_message={},
        saved_image_paths=[],
        record=lambda stop_events=None: tl_db.ProducerRecord(),
    )
    run_handle = SimpleNamespace(run_id=failed_run_id, thread_id=thread_id, guard=None)
    exec_info = LocalRunExecution(
        thread_id=thread_id,
        run_id=failed_run_id,
        status=LocalRunStatus.RUNNING,
        created_at=t0,
        graph=graph,
        metadata={
            "run_handle": run_handle,
            "handler": fake_handler,
            "workspace_id": ws_id,
            "user_id": "owner-1",
            "recorded_as": "owner-1",
            "start_time": t0.timestamp(),
        },
    )
    executor.executions[(thread_id, failed_run_id)] = exec_info

    await executor._finalize_run(thread_id, failed_run_id, kind="failed", error=str(caught_exc), exc=caught_exc)

    # Assert exactly 1 wakeup_quota_resume outbox job was enqueued with committed_ckpt_id
    assert len(store.outbox) == 1
    outbox_job = store.outbox[0]
    assert outbox_job["hook_type"] == "wakeup_quota_resume"
    assert outbox_job["payload"]["resume_checkpoint_id"] == committed_ckpt_id
    assert outbox_job["payload"]["workspace_id"] == ws_id

    # Worker 2 claims the outbox job and runs execute_wakeup_quota_resume:
    # validate body with REAL RetryRequest Pydantic model, then resume the REAL LangGraph checkpoint!
    monkeypatch.setattr(
        tl_db,
        "get_latest_attempt",
        create_autospec(tl_db.get_latest_attempt, instance=False, return_value=persisted_run_row),
    )

    validated_bodies: list[RetryRequest] = []
    resumed_final_state: dict[str, Any] = {}

    async def _dispatch_retry_to_real_graph(
        *,
        thread_id: str,
        body: dict[str, Any],
        user_id: str,
        wait_cap: float,
        log_prefix: str,
        subject: str = "",
        route: str = "messages",
        busy_fails_fast: bool = False,
        refusal_fails_fast: bool = False,
        on_plan_limited: Any = None,
        heartbeat: Any = None,
        still_due: Any = None,
        on_unanswered: Any = None,
        room_fails_fast: bool = False,
        suspension_waits: bool = False,
    ) -> tuple[str, str | None]:
        assert route == "retry"
        parsed = RetryRequest.model_validate(body)  # REAL Pydantic /retry validation!
        validated_bodies.append(parsed)
        # Quota has now reset: resume the REAL LangGraph graph from parsed.checkpoint_id
        quota_exhausted_flag["exhausted"] = False
        result = await graph.ainvoke(
            None,
            config={"configurable": {"thread_id": thread_id, "checkpoint_id": parsed.checkpoint_id}},
        )
        resumed_final_state.update(result)
        return "dispatched", str(uuid4())

    post_mock = create_autospec(notify_turn.post_notification_turn, instance=False, side_effect=_dispatch_retry_to_real_graph)
    monkeypatch.setattr(notify_turn, "post_notification_turn", post_mock)

    await uf.execute_wakeup_quota_resume(dict(outbox_job))

    assert len(validated_bodies) == 1
    assert validated_bodies[0].workspace_id == ws_id
    assert validated_bodies[0].checkpoint_id == committed_ckpt_id
    UUID(str(validated_bodies[0].request_key))  # valid UUID
    assert validated_bodies[0].wakeup_resume is not None
    assert validated_bodies[0].wakeup_resume["root_job_id"] == root_job_id
    assert validated_bodies[0].wakeup_resume["attempts"] == 1
    assert validated_bodies[0].wakeup_resume["first_due_at"] == t0.isoformat()

    # The graph completed llm_step without re-running tool_step!
    assert resumed_final_state["answer"] == "completed-after-resume"
    assert tool_side_effect_calls == ["tool-1-ran"]


# ---------------------------------------------------------------------------
# 4. Resume deadline re-check, attempts-fenced reschedule, and exhausted settlement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_wakeup_quota_resume_deadline_recheck_and_fenced_reschedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    store = _InMemoryDeferralStore()
    root_job_id = str(uuid4())
    thread_id = str(uuid4())
    failed_run_id = str(uuid4())
    ws_id = str(uuid4())
    past_due = datetime.now(timezone.utc) - timedelta(hours=25)

    @asynccontextmanager
    async def _conn_ctx(conn: Any = None):
        yield store

    monkeypatch.setattr(uf, "get_db_connection", _conn_ctx)
    monkeypatch.setattr(
        uf,
        "_maybe_enqueue_failure_notice_in_tx",
        create_autospec(uf._maybe_enqueue_failure_notice_in_tx, instance=False, return_value=True),
    )

    # Case A: Past 24h deadline on claim -> marks exhausted_failed, NEVER calls post_notification_turn
    store.rows[root_job_id] = {
        "root_job_id": root_job_id,
        "wakeup_id": "wake-expired",
        "thread_id": thread_id,
        "owner_user_id": "owner-1",
        "first_due_at": past_due,
        "deadline_at": past_due + timedelta(hours=24),
        "defer_count": 1,
        "last_step_key": f"mid:{failed_run_id}",
        "seen_step_keys": [f"mid:{failed_run_id}"],
        "status": "deferred",
    }
    orig_post_turn = notify_turn.post_notification_turn
    post_mock = create_autospec(orig_post_turn, instance=False)
    monkeypatch.setattr(notify_turn, "post_notification_turn", post_mock)

    await uf.execute_wakeup_quota_resume(
        {
            "hook_outbox_id": str(uuid4()),
            "conversation_thread_id": thread_id,
            "run_id": failed_run_id,
            "attempts": 1,
            "payload": {
                "root_job_id": root_job_id,
                "wakeup_id": "wake-expired",
                "owner_user_id": "owner-1",
                "workspace_id": ws_id,
                "failed_run_id": failed_run_id,
                "defer_count": 1,
            },
        }
    )
    assert post_mock.await_count == 0
    assert store.rows[root_job_id]["status"] == "exhausted_failed"

    # Case B: Within deadline, hits pre-dispatch cooldown on resume -> reschedules WITH attempts fence
    root_job_2 = str(uuid4())
    outbox_id_2 = str(uuid4())
    now = datetime.now(timezone.utc)
    store.rows[root_job_2] = {
        "root_job_id": root_job_2,
        "wakeup_id": "wake-resched",
        "thread_id": thread_id,
        "owner_user_id": "owner-1",
        "first_due_at": now - timedelta(hours=1),
        "deadline_at": now + timedelta(hours=23),
        "defer_count": 1,
        "last_step_key": f"mid:{failed_run_id}",
        "seen_step_keys": [f"mid:{failed_run_id}"],
        "status": "deferred",
    }
    monkeypatch.setattr(
        tl_db,
        "get_latest_attempt",
        create_autospec(
            tl_db.get_latest_attempt,
            instance=False,
            return_value={"conversation_response_id": failed_run_id, "status": "error", "metadata": {"workspace_id": ws_id}},
        ),
    )

    async def _plan_limited_cooldown(*args: Any, **kwargs: Any) -> tuple[str, None]:
        cb = kwargs.get("on_plan_limited")
        if cb is not None:
            await cb(
                {
                    "type": uf.DALTON_SUBSCRIPTION_COOLDOWN_TYPE,
                    "reason": "plan_limited",
                    "held_until": (now + timedelta(minutes=30)).isoformat(),
                }
            )
        return "plan_limited", None

    post_mock_2 = create_autospec(orig_post_turn, instance=False, side_effect=_plan_limited_cooldown)
    monkeypatch.setattr(notify_turn, "post_notification_turn", post_mock_2)

    await uf.execute_wakeup_quota_resume(
        {
            "hook_outbox_id": outbox_id_2,
            "conversation_thread_id": thread_id,
            "run_id": failed_run_id,
            "attempts": 2,
            "payload": {
                "root_job_id": root_job_2,
                "wakeup_id": "wake-resched",
                "owner_user_id": "owner-1",
                "workspace_id": ws_id,
                "failed_run_id": failed_run_id,
                "defer_count": 1,
            },
        }
    )
    assert len(store.reschedule_queries) == 1
    sql_text, sql_params = store.reschedule_queries[0]
    assert "AND attempts = %s" in sql_text
    assert sql_params[1] == outbox_id_2
    assert sql_params[2] == 2

    # Case C: Resume hits dalton_wakeup_quota_exhausted (no held_until) -> settles cleanly without raising
    async def _plan_limited_exhausted(*args: Any, **kwargs: Any) -> tuple[str, None]:
        cb = kwargs.get("on_plan_limited")
        if cb is not None:
            await cb(
                {
                    "type": uf.DALTON_WAKEUP_QUOTA_EXHAUSTED_TYPE,
                    "reason": "wakeup_quota_defer_limit_exceeded",
                    "held_until": None,
                }
            )
        return "plan_limited", None

    post_mock_3 = create_autospec(orig_post_turn, instance=False, side_effect=_plan_limited_exhausted)
    monkeypatch.setattr(notify_turn, "post_notification_turn", post_mock_3)
    await uf.execute_wakeup_quota_resume(
        {
            "hook_outbox_id": outbox_id_2,
            "conversation_thread_id": thread_id,
            "run_id": failed_run_id,
            "attempts": 3,
            "payload": {
                "root_job_id": root_job_2,
                "wakeup_id": "wake-resched",
                "owner_user_id": "owner-1",
                "workspace_id": ws_id,
                "failed_run_id": failed_run_id,
                "defer_count": 3,
            },
        }
    )
    assert post_mock_3.await_count == 1


# ---------------------------------------------------------------------------
# 5. Unknown reset, 24h overflow, pure auth failure, and executor registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wakeup_unknown_reset_24h_overflow_and_pure_auth_zero_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _InMemoryDeferralStore()
    notice_mock = create_autospec(uf._maybe_enqueue_failure_notice_in_tx, instance=False, return_value=True)
    monkeypatch.setattr(uf, "_maybe_enqueue_failure_notice_in_tx", notice_mock)

    t0 = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc)

    # Unknown reset_at=None -> now + 30m, held_known=False
    dec_unknown = await uf.evaluate_and_record_wakeup_deferral_tx(
        store,
        root_job_id=str(uuid4()),
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

    # 7-day cooldown exceeds 24h deadline -> immediate exhausted_failed
    dec_long = await uf.evaluate_and_record_wakeup_deferral_tx(
        store,
        root_job_id=str(uuid4()),
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
    exc_long = uf.build_wakeup_cooldown_http_exception(dec_long)
    assert exc_long.detail["type"] == uf.DALTON_WAKEUP_QUOTA_EXHAUSTED_TYPE
    assert "held_until" in exc_long.detail

    # Pure auth_unavailable -> zero wait, exhausted_failed
    dec_auth = await uf.evaluate_and_record_wakeup_deferral_tx(
        store,
        root_job_id=str(uuid4()),
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
        ],
        first_due_at=t0,
        now=t0,
    )
    assert dec_auth.action == "exhausted_failed"
    assert dec_auth.reason == "oauth_auth_unavailable"
    assert dec_auth.defer_count == 0
    assert notice_mock.await_count == 2


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


# ---------------------------------------------------------------------------
# 6. Codex #7, #8, #9: own-run recognition, 24h deadline before HTTP retries &
#    DB fail-closed & pre-held normal wakeup recovery, and switch-off settlement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_wakeup_quota_resume_recognizes_own_dispatched_run_without_settling_deferral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #7: Re-claiming unacked job after /retry created its own run must NOT mark deferral settled."""
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    store = _InMemoryDeferralStore()
    root_job_id = str(uuid4())
    thread_id = str(uuid4())
    failed_run_id = str(uuid4())
    own_retry_run_id = str(uuid4())
    ws_id = str(uuid4())
    now = datetime.now(timezone.utc)
    req_key = uf.deterministic_wakeup_resume_request_key(root_job_id, 1)

    @asynccontextmanager
    async def _conn_ctx(conn: Any = None):
        yield store

    monkeypatch.setattr(uf, "get_db_connection", _conn_ctx)
    store.rows[root_job_id] = {
        "root_job_id": root_job_id,
        "wakeup_id": "wake-own",
        "thread_id": thread_id,
        "owner_user_id": "owner-1",
        "first_due_at": now - timedelta(hours=1),
        "deadline_at": now + timedelta(hours=23),
        "defer_count": 1,
        "last_step_key": f"mid:{failed_run_id}",
        "seen_step_keys": [f"mid:{failed_run_id}"],
        "status": "deferred",
    }

    job = {
        "hook_outbox_id": str(uuid4()),
        "conversation_thread_id": thread_id,
        "run_id": failed_run_id,
        "attempts": 2,
        "payload": {
            "root_job_id": root_job_id,
            "wakeup_id": "wake-own",
            "owner_user_id": "owner-1",
            "workspace_id": ws_id,
            "failed_run_id": failed_run_id,
            "defer_count": 1,
            "request_key": req_key,
        },
    }

    orig_post = notify_turn.post_notification_turn
    orig_get_latest = tl_db.get_latest_attempt
    post_mock = create_autospec(orig_post, instance=False)
    monkeypatch.setattr(notify_turn, "post_notification_turn", post_mock)

    # Case A: latest attempt IS our own dispatched retry run -> ack old job ONLY, keep deferral 'deferred'!
    monkeypatch.setattr(
        tl_db,
        "get_latest_attempt",
        create_autospec(
            orig_get_latest,
            instance=False,
            return_value={
                "conversation_response_id": own_retry_run_id,
                "status": "in_progress",
                "request_key": req_key,
                "retry_of_run_id": failed_run_id,
            },
        ),
    )
    await uf.execute_wakeup_quota_resume(job)
    assert post_mock.await_count == 0
    assert store.rows[root_job_id]["status"] == "deferred"

    # Case B: our own retry run already finished & another retry R3 is now latest, while R2 is in history ->
    # STILL recognizes own dispatched run via (request_key, retry_of_run_id) and keeps deferral 'deferred'!
    store.responses.append(
        {
            "conversation_response_id": own_retry_run_id,
            "conversation_thread_id": thread_id,
            "request_key": req_key,
            "retry_of_run_id": failed_run_id,
        }
    )
    monkeypatch.setattr(
        tl_db,
        "get_latest_attempt",
        create_autospec(
            orig_get_latest,
            instance=False,
            return_value={
                "conversation_response_id": str(uuid4()),
                "status": "in_progress",
                "request_key": str(uuid4()),
                "retry_of_run_id": own_retry_run_id,
            },
        ),
    )
    await uf.execute_wakeup_quota_resume(job)
    assert post_mock.await_count == 0
    assert store.rows[root_job_id]["status"] == "deferred"

    # Case C: genuinely foreign user turn (no matching (request_key, retry_of_run_id)) -> marks 'settled'
    store.responses.clear()
    await uf.execute_wakeup_quota_resume(job)
    assert store.rows[root_job_id]["status"] == "settled"


@pytest.mark.asyncio
async def test_wakeup_24h_deadline_checked_before_http_retries_db_error_fails_closed_and_pre_held_recovery_checks_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #8:
    (a) DB error during deadline check fails closed (raises, never dispatches).
    (b) Crossing 24h deadline between internal HTTP retries is caught by still_due before next POST.
    (c) Pre-dispatch held normal wakeup recovering past 24h when model IS now available STILL checks
        the ledger in automation_thread_model and refuses with dalton_wakeup_quota_exhausted (400)
        (or 503 on DB error).
    """
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    store = _InMemoryDeferralStore()
    root_job_id = str(uuid4())
    thread_id = str(uuid4())
    failed_run_id = str(uuid4())
    ws_id = str(uuid4())
    now = datetime.now(timezone.utc)

    # (a) DB failure during _check_and_enforce_resume_deadline raises and NEVER dispatches
    @asynccontextmanager
    async def _broken_conn(conn: Any = None):
        raise RuntimeError("pg connection lost")
        yield  # pragma: no cover

    orig_post = notify_turn.post_notification_turn
    monkeypatch.setattr(uf, "get_db_connection", _broken_conn)
    post_mock = create_autospec(orig_post, instance=False)
    monkeypatch.setattr(notify_turn, "post_notification_turn", post_mock)
    with pytest.raises(RuntimeError, match="pg connection lost"):
        await uf.execute_wakeup_quota_resume(
            {
                "hook_outbox_id": str(uuid4()),
                "conversation_thread_id": thread_id,
                "run_id": failed_run_id,
                "attempts": 1,
                "payload": {
                    "root_job_id": root_job_id,
                    "wakeup_id": "wake-db-err",
                    "owner_user_id": "owner-1",
                    "workspace_id": ws_id,
                    "failed_run_id": failed_run_id,
                    "defer_count": 1,
                },
            }
        )
    assert post_mock.await_count == 0

    # (b) Crossing 24h deadline before an internal HTTP retry stops via still_due
    @asynccontextmanager
    async def _conn_ctx(conn: Any = None):
        yield store

    monkeypatch.setattr(uf, "get_db_connection", _conn_ctx)
    monkeypatch.setattr(
        uf,
        "_maybe_enqueue_failure_notice_in_tx",
        create_autospec(uf._maybe_enqueue_failure_notice_in_tx, instance=False, return_value=True),
    )
    store.rows[root_job_id] = {
        "root_job_id": root_job_id,
        "wakeup_id": "wake-mid-retry",
        "thread_id": thread_id,
        "owner_user_id": "owner-1",
        "first_due_at": now - timedelta(hours=23),
        "deadline_at": now + timedelta(hours=1),
        "defer_count": 1,
        "last_step_key": f"mid:{failed_run_id}",
        "seen_step_keys": [f"mid:{failed_run_id}"],
        "status": "deferred",
    }
    monkeypatch.setattr(
        tl_db,
        "get_latest_attempt",
        create_autospec(
            tl_db.get_latest_attempt,
            instance=False,
            return_value={"conversation_response_id": failed_run_id, "status": "error", "metadata": {"workspace_id": ws_id}},
        ),
    )

    still_due_results: list[bool] = []

    async def _sim_retry_crosses_deadline(*args: Any, **kwargs: Any) -> tuple[str, None]:
        still_due_cb = kwargs["still_due"]
        assert still_due_cb is not None
        # First attempt before deadline -> due
        still_due_results.append(await still_due_cb())
        # Deadline expires before the next HTTP retry!
        store.rows[root_job_id]["deadline_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        still_due_results.append(await still_due_cb())
        return "superseded", None

    monkeypatch.setattr(
        notify_turn,
        "post_notification_turn",
        create_autospec(orig_post, instance=False, side_effect=_sim_retry_crosses_deadline),
    )
    await uf.execute_wakeup_quota_resume(
        {
            "hook_outbox_id": str(uuid4()),
            "conversation_thread_id": thread_id,
            "run_id": failed_run_id,
            "attempts": 1,
            "payload": {
                "root_job_id": root_job_id,
                "wakeup_id": "wake-mid-retry",
                "owner_user_id": "owner-1",
                "workspace_id": ws_id,
                "failed_run_id": failed_run_id,
                "defer_count": 1,
            },
        }
    )
    assert still_due_results == [True, False]
    assert store.rows[root_job_id]["status"] == "exhausted_failed"

    # (c) Pre-dispatch held normal wakeup recovering past 24h when model IS now available
    #     STILL checks ledger in automation_thread_model and refuses (never dispatches!)
    root_pre = str(uuid4())
    store.rows[root_pre] = {
        "root_job_id": root_pre,
        "wakeup_id": "wake-pre-expired",
        "thread_id": thread_id,
        "owner_user_id": "owner-1",
        "first_due_at": now - timedelta(hours=25),
        "deadline_at": now - timedelta(hours=1),
        "defer_count": 1,
        "last_step_key": f"pre:{root_pre}:1",
        "seen_step_keys": [f"pre:{root_pre}:1"],
        "status": "deferred",
    }
    monkeypatch.setattr(
        auto_tm_mod,
        "_resolve_automation_notification_context",
        create_autospec(
            auto_tm_mod._resolve_automation_notification_context,
            instance=False,
            return_value=(None, None, None, False, False, None),
        ),
    )
    # Primary model is AVAILABLE now!
    monkeypatch.setattr(
        uf,
        "inspect_subscription_availability",
        create_autospec(
            uf.inspect_subscription_availability,
            instance=False,
            return_value=uf.SubscriptionAvailability(available=True, model_name="claude-opus-5-5-oauth"),
        ),
    )
    resolve_never = create_autospec(lambda m: None, instance=False)
    req_wake = SimpleNamespace(
        query_type="wakeup",
        platform=None,
        hitl_response=None,
        wakeup={"v": 1, "root_job_id": root_pre, "wakeup_id": "wake-pre-expired", "notices": []},
    )
    with pytest.raises(HTTPException) as exp_exc:
        await auto_tm_mod.resolve_turn_config_with_automation_inheritance(
            user_id="owner-1",
            thread_id=thread_id,
            request=req_wake,
            requested_model="claude-opus-5-5-oauth",
            held_model=None,
            from_service=True,
            resolve=resolve_never,
        )
    assert exp_exc.value.status_code == 400
    assert exp_exc.value.detail["type"] == uf.DALTON_WAKEUP_QUOTA_EXHAUSTED_TYPE
    assert store.rows[root_pre]["status"] == "exhausted_failed"
    assert resolve_never.call_count == 0

    # And DB failure on admission returns 503 (never dispatches!)
    monkeypatch.setattr(uf, "get_db_connection", _broken_conn)
    with pytest.raises(HTTPException) as db_exc:
        await auto_tm_mod.resolve_turn_config_with_automation_inheritance(
            user_id="owner-1",
            thread_id=thread_id,
            request=req_wake,
            requested_model="claude-opus-5-5-oauth",
            held_model=None,
            from_service=True,
            resolve=resolve_never,
        )
    assert db_exc.value.status_code == 503
    assert resolve_never.call_count == 0


@pytest.mark.asyncio
async def test_execute_wakeup_quota_resume_switch_off_settles_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #9: Main switch OFF settles already-queued wakeup_quota_resume as failed without dispatching."""
    monkeypatch.delenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", raising=False)
    store = _InMemoryDeferralStore()
    root_job_id = str(uuid4())
    thread_id = str(uuid4())
    failed_run_id = str(uuid4())
    now = datetime.now(timezone.utc)

    @asynccontextmanager
    async def _conn_ctx(conn: Any = None):
        yield store

    monkeypatch.setattr(uf, "get_db_connection", _conn_ctx)
    notice_mock = create_autospec(uf._maybe_enqueue_failure_notice_in_tx, instance=False, return_value=True)
    monkeypatch.setattr(uf, "_maybe_enqueue_failure_notice_in_tx", notice_mock)
    store.rows[root_job_id] = {
        "root_job_id": root_job_id,
        "wakeup_id": "wake-switch-off",
        "thread_id": thread_id,
        "owner_user_id": "owner-1",
        "first_due_at": now - timedelta(minutes=10),
        "deadline_at": now + timedelta(hours=23),
        "defer_count": 1,
        "last_step_key": f"mid:{failed_run_id}",
        "seen_step_keys": [f"mid:{failed_run_id}"],
        "status": "deferred",
    }

    post_mock = create_autospec(notify_turn.post_notification_turn, instance=False)
    monkeypatch.setattr(notify_turn, "post_notification_turn", post_mock)

    await uf.execute_wakeup_quota_resume(
        {
            "hook_outbox_id": str(uuid4()),
            "conversation_thread_id": thread_id,
            "run_id": failed_run_id,
            "attempts": 1,
            "payload": {
                "root_job_id": root_job_id,
                "wakeup_id": "wake-switch-off",
                "owner_user_id": "owner-1",
                "workspace_id": str(uuid4()),
                "failed_run_id": failed_run_id,
                "defer_count": 1,
            },
        }
    )
    assert post_mock.await_count == 0
    assert store.rows[root_job_id]["status"] == "exhausted_failed"
    assert store.rows[root_job_id]["last_reason"] == "unattended_quota_fallback_disabled"
    assert notice_mock.await_count == 1
