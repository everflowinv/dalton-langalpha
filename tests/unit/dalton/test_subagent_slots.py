"""Unit tests for Redis-backed background subagent concurrency caps and queueing.

Covers:
1. Per-thread cap (default 8): 9th task queues in FIFO order and starts after any
   of the first 8 finishes;
2. Global cap (default 24): 3 threads each dispatch 9 tasks -> max 24 running
   globally and max 8 per thread;
3. Lease expiration (90s TTL, 30s renewal): a holder that stops renewing loses
   its slot after 90s on a controllable clock (no real sleep), while an active
   holder that renews every 30s keeps its slot past 90s;
4. Cancellation while queued: terminates cleanly without occupying or leaking a
   slot, and advances subsequent queued tasks;
5. Exception while running: releases the slot in ``finally`` so queued tasks
   proceed;
6. Redis unavailable / error: fails open without throttling and logs a WARNING
   (verified via ``caplog``);
7. Workflow child subagents: counted toward thread and global limits alongside
   direct ``Task`` dispatches (while the ``workflow`` driver task itself does
   not consume a child slot), and ``time_limit`` / ``child_timeout`` start when
   the slot is acquired rather than while queued;
8. Queued wording in ``Task`` tool results (init & resume) and ``TaskOutput``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import create_autospec

import pytest
import redis.asyncio as redis_async
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from ptc_agent.agent.middleware.background_subagent.dispatch import (
    SubagentDispatcher,
    dispatch_background_subagent,
)
from ptc_agent.agent.middleware.background_subagent.middleware import (
    BackgroundSubagentMiddleware,
)
from ptc_agent.agent.middleware.background_subagent.registry import (
    BackgroundTask,
    BackgroundTaskRegistry,
)
from ptc_agent.agent.middleware.background_subagent.spawn import spawn_task_writer
from ptc_agent.agent.middleware.background_subagent.task import WORKFLOW_SUBAGENT_TYPE
from ptc_agent.agent.middleware.background_subagent.task_actions import (
    handle_task_action,
)
from ptc_agent.agent.middleware.background_subagent.tools import (
    create_task_output_tool,
)
from ptc_agent.agent.middleware.background_subagent.workflow.driver import (
    WorkflowDriver,
    WorkflowRunSpec,
    _ChildRun,
)
from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
    WorkflowMeta,
)
from ptc_agent.agent.middleware.runtime_context.turn import TURN_DEADLINE_KEY
from src.config.models import WorkflowOrchestrationConfig
from src.dalton import subagent_slots
from src.dalton.subagent_slots import (
    ACQUIRE_SLOT_LUA,
    DEFAULT_MAX_RUNNING_GLOBAL,
    DEFAULT_MAX_RUNNING_PER_THREAD,
    LEASE_TTL_S,
    QUERY_QUEUE_LUA,
    RELEASE_SLOT_LUA,
    RENEW_INTERVAL_S,
    SubagentLimits,
    format_queued_notice,
    get_subagent_limits,
    global_slots_key,
    is_subagent_queued,
    queue_lease_key,
    queue_order_key,
    release_subagent_slot,
    reserve_subagent_slot,
    thread_slots_key,
)
from src.utils.cache.redis_cache import RedisCacheClient


class ControllableClock:
    """Deterministic monotonic/wall clock for lease TTL and timeout tests."""

    def __init__(self, initial: float = 1_000_000.0) -> None:
        self.now = float(initial)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


class FakeSlotRedisEval:
    """In-memory ZSET + counter engine faithfully executing ACQUIRE_SLOT_LUA and RELEASE_SLOT_LUA."""

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.counters: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.eval_calls: int = 0
        self.fail_with: Exception | None = None
        self.after_execute_error: BaseException | None = None

    def _zremrangebyscore(self, key: str, max_score: float) -> int:
        zset = self.zsets.get(key)
        if not zset:
            return 0
        expired = [m for m, score in zset.items() if score <= max_score]
        for m in expired:
            del zset[m]
        return len(expired)

    def _zrangebyscore(self, key: str, max_score: float) -> list[str]:
        zset = self.zsets.get(key)
        if not zset:
            return []
        matching = [(score, m) for m, score in zset.items() if score <= max_score]
        matching.sort()
        return [m for _, m in matching]

    def _zscore(self, key: str, member: str) -> float | None:
        zset = self.zsets.get(key)
        if not zset:
            return None
        return zset.get(member)

    def _zadd(self, key: str, score: float, member: str) -> None:
        zset = self.zsets.setdefault(key, {})
        zset[member] = float(score)

    def _zrem(self, key: str, member: str) -> int:
        zset = self.zsets.get(key)
        if not zset or member not in zset:
            return 0
        del zset[member]
        return 1

    def _zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def _zrank(self, key: str, member: str) -> int | None:
        zset = self.zsets.get(key)
        if not zset or member not in zset:
            return None
        ordered = sorted((score, m) for m, score in zset.items())
        for idx, (_, m) in enumerate(ordered):
            if m == member:
                return idx
        return None

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        self.eval_calls += 1
        if self.fail_with is not None:
            raise self.fail_with

        keys = [str(k) for k in keys_and_args[:numkeys]]
        argv = [str(a) for a in keys_and_args[numkeys:]]

        if script == ACQUIRE_SLOT_LUA:
            assert numkeys == 5
            thread_key, global_key, q_order_key, q_lease_key, seq_key = keys
            member = argv[0]
            now = float(argv[1])
            ttl = float(argv[2])
            max_thread = int(argv[3])
            max_global = int(argv[4])
            mode = argv[5] if len(argv) >= 6 else "acquire"
            expires_at = now + ttl
            key_ttl = max(300, int(ttl * 4))

            self._zremrangebyscore(thread_key, now)
            self._zremrangebyscore(global_key, now)

            expired_queued = self._zrangebyscore(q_lease_key, now)
            if expired_queued:
                for exp_member in expired_queued:
                    self._zrem(q_order_key, exp_member)
                self._zremrangebyscore(q_lease_key, now)

            in_thread = self._zscore(thread_key, member)
            in_global = self._zscore(global_key, member)
            if in_thread is not None and in_global is not None:
                self._zadd(thread_key, expires_at, member)
                self._zadd(global_key, expires_at, member)
                self._zrem(q_order_key, member)
                self._zrem(q_lease_key, member)
                self.ttls[thread_key] = key_ttl
                self.ttls[global_key] = key_ttl
                return [
                    1,
                    self._zcard(thread_key),
                    self._zcard(global_key),
                    0,
                ]
            if in_thread is not None or in_global is not None:
                self._zrem(thread_key, member)
                self._zrem(global_key, member)

            thread_running = self._zcard(thread_key)
            global_running = self._zcard(global_key)

            if mode == "reregister":
                self._zrem(q_order_key, member)
                self._zrem(q_lease_key, member)
                self._zadd(thread_key, expires_at, member)
                self._zadd(global_key, expires_at, member)
                self.ttls[thread_key] = key_ttl
                self.ttls[global_key] = key_ttl
                if self.after_execute_error is not None:
                    err = self.after_execute_error
                    self.after_execute_error = None
                    raise err
                return [
                    1,
                    self._zcard(thread_key),
                    self._zcard(global_key),
                    0,
                ]

            if mode == "renew":
                return [0, thread_running, global_running, 0]

            if self._zscore(q_order_key, member) is None:
                seq = self.counters.get(seq_key, 0) + 1
                self.counters[seq_key] = seq
                self._zadd(q_order_key, float(seq), member)
            self._zadd(q_lease_key, expires_at, member)
            self.ttls[q_order_key] = key_ttl
            self.ttls[q_lease_key] = key_ttl
            self.ttls[seq_key] = key_ttl

            rank = self._zrank(q_order_key, member) or 0
            if (
                rank == 0
                and thread_running < max_thread
                and global_running < max_global
            ):
                self._zrem(q_order_key, member)
                self._zrem(q_lease_key, member)
                self._zadd(thread_key, expires_at, member)
                self._zadd(global_key, expires_at, member)
                self.ttls[thread_key] = key_ttl
                self.ttls[global_key] = key_ttl
                if self.after_execute_error is not None:
                    err = self.after_execute_error
                    self.after_execute_error = None
                    raise err
                return [1, thread_running + 1, global_running + 1, 0]

            if self.after_execute_error is not None:
                err = self.after_execute_error
                self.after_execute_error = None
                raise err
            return [0, thread_running, global_running, rank]

        if script == QUERY_QUEUE_LUA:
            assert numkeys == 4
            thread_key, global_key, q_order_key, q_lease_key = keys
            exact_member = argv[0]
            base_member = argv[1]
            now = float(argv[2])

            def _check_member(m: str) -> int | None:
                exp = self._zscore(q_lease_key, m)
                if exp is None or exp <= now:
                    return None
                ordered = sorted(
                    (score, mem)
                    for mem, score in self.zsets.get(q_order_key, {}).items()
                )
                rank = next((i for i, (_, mem) in enumerate(ordered) if mem == m), None)
                if rank is None:
                    return None
                ahead = 0
                for _, prior in ordered[:rank]:
                    p_exp = self._zscore(q_lease_key, prior)
                    if p_exp is not None and p_exp > now:
                        ahead += 1
                return ahead

            ahead = _check_member(exact_member)
            if ahead is None and base_member != exact_member:
                ahead = _check_member(base_member)
            if ahead is None:
                return [0, 0, 0, 0]
            thread_running = sum(
                1 for score in self.zsets.get(thread_key, {}).values() if score > now
            )
            global_running = sum(
                1 for score in self.zsets.get(global_key, {}).values() if score > now
            )
            return [1, thread_running, global_running, ahead]

        if script == RELEASE_SLOT_LUA:
            assert numkeys == 4
            r1 = self._zrem(keys[0], argv[0])
            r2 = self._zrem(keys[1], argv[0])
            r3 = self._zrem(keys[2], argv[0])
            r4 = self._zrem(keys[3], argv[0])
            return 1 if (r1 + r2 + r3 + r4) > 0 else 0

        raise AssertionError(f"Unexpected Lua script: {script[:40]}")


def make_fake_cache(
    *,
    enabled: bool = True,
    with_client: bool = True,
) -> tuple[RedisCacheClient, FakeSlotRedisEval]:
    """Build a spec'd ``RedisCacheClient`` and ``redis.Redis`` client using ``create_autospec``."""
    engine = FakeSlotRedisEval()
    cache = create_autospec(RedisCacheClient, instance=True)
    cache.enabled = enabled
    if with_client:
        redis_client = create_autospec(redis_async.Redis, instance=True)
        redis_client.eval.side_effect = engine.eval
        cache.client = redis_client
    else:
        cache.client = None
    return cache, engine


def _task_row(task: BackgroundTask, *, thread_id: str) -> dict:
    """A ledger row for ``task`` as ThreadStatus's task listing reads it."""
    now = datetime.now(tz=UTC)
    return {
        "task_id": task.task_id,
        "task_run_id": str(getattr(task, "task_run_id", "") or ""),
        "conversation_thread_id": thread_id,
        "status": "in_progress",
        "subagent_type": task.subagent_type,
        "description": task.description,
        "started_at": now,
        "finalized_at": None,
    }


def _patch_reporting_tasks(
    monkeypatch: pytest.MonkeyPatch, tasks: list[BackgroundTask], thread_id: str
) -> None:
    """Serve ``tasks`` as ThreadStatus's task listing rows (B7: the queued
    overview lives in ThreadStatus, since TaskOutput's aggregate went away)."""
    from src.server.database.runs import subagent_runs as sr_db

    async def _list(
        thread_id_arg: str, *, since: Any = None, task_id: str | None = None
    ) -> list[dict]:
        if task_id is not None:
            return []
        return [_task_row(t, thread_id=thread_id) for t in tasks]

    monkeypatch.setattr(
        sr_db,
        "list_reporting_tasks",
        create_autospec(sr_db.list_reporting_tasks, side_effect=_list),
    )


async def _thread_status_tasks(
    thread_id: str, registry: BackgroundTaskRegistry
) -> str:
    """ThreadStatus's task-only listing, as the agent reads it."""
    from src.tools.thread_status.tool import build_thread_status_tool

    tool = build_thread_status_tool(
        thread_id=thread_id, user_id="u-1", registry=registry
    )
    message = await tool.ainvoke(
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "ThreadStatus",
            "args": {"type": "task"},
        },
        {"configurable": {"timezone": "UTC"}},
    )
    return str(message.content)


class FakeMiddleware:
    """Minimal middleware harness over a real ``BackgroundTaskRegistry``."""

    def __init__(self, thread_id: str = "thread-1") -> None:
        self.registry = BackgroundTaskRegistry(thread_id=thread_id)
        self.namespace_owner = None
        self.subagent_types = {"general-purpose", "research", "data-prep"}
        self._resume_claims: set[str] = set()

    async def _acquire_task_ns(self, task_id: str) -> bool:
        return True

    async def _resolve_or_error(
        self,
        target_task_id: str | None,
        parent_thread_id: str,
        tool_call_id: str,
        *,
        action_name: str,
    ) -> BackgroundTask | ToolMessage:
        assert target_task_id is not None
        task = await self.registry.get_by_task_id(target_task_id)
        assert task is not None
        return task

    async def admit_task_run(self, task: BackgroundTask, **kwargs: Any) -> str:
        if kwargs.get("cause") == "resume":
            return ""
        return f"run-{task.task_id}"

    async def _append_run_opener(self, task: BackgroundTask, prompt: str) -> None:
        return None

    async def _abort_admitted_run(self, task: BackgroundTask, exc: Exception) -> None:
        return None

    async def _finalize_cancelled_before_spawn(self, task: BackgroundTask) -> None:
        from ptc_agent.agent.middleware.background_subagent.run_executor import (
            _settle_terminal_run,
        )

        await _settle_terminal_run(
            task,
            ledger_status="cancelled",
            ledger_failure=None,
            registry=self.registry,
            namespace_owner=None,
        )

    async def _reset_task_for_resume(
        self,
        task: BackgroundTask,
        *,
        task_run_id: str | None,
        spawned_run_id: str | None,
    ) -> None:
        task.terminal_status = None
        task.result = None
        task.error = None
        task.asyncio_task = None
        task.handler_task = None
        task.task_run_id = task_run_id
        task.spawned_run_id = spawned_run_id


def _make_tool_request(
    *,
    tool_call_id: str,
    thread_id: str = "thread-1",
    action: str = "init",
    subagent_type: str = "general-purpose",
    description: str = "Analyze earnings",
    prompt: str = "Run full analysis",
    task_id: str | None = None,
) -> ToolCallRequest:
    request = create_autospec(ToolCallRequest, instance=True)
    args: dict[str, Any] = {
        "action": action,
        "subagent_type": subagent_type,
        "description": description,
        "prompt": prompt,
    }
    if task_id is not None:
        args["task_id"] = task_id
    request.tool_call = {
        "name": "Task",
        "id": tool_call_id,
        "args": args,
    }
    request.state = {"messages": []}
    request.runtime = None
    return request


@pytest.fixture
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> ControllableClock:
    """Use short poll intervals so queue progression tests finish in milliseconds."""
    clock = ControllableClock()
    monkeypatch.setattr(subagent_slots, "POLL_INTERVAL_MIN_S", 0.01)
    monkeypatch.setattr(subagent_slots, "POLL_INTERVAL_MAX_S", 0.02)
    orig_reserve = subagent_slots.reserve_subagent_slot
    orig_query = subagent_slots.query_queued_slot_status

    async def _fast_reserve(task: Any, thread_id: str, **kwargs: Any) -> Any:
        kwargs.setdefault("clock", clock)
        kwargs.setdefault("poll_min_s", 0.01)
        kwargs.setdefault("poll_max_s", 0.02)
        return await orig_reserve(task, thread_id, **kwargs)

    async def _fast_query(
        thread_id: str,
        task_id: str,
        task_run_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        kwargs.setdefault("clock", clock)
        return await orig_query(thread_id, task_id, task_run_id, **kwargs)

    monkeypatch.setattr(subagent_slots, "reserve_subagent_slot", _fast_reserve)
    monkeypatch.setattr(subagent_slots, "query_queued_slot_status", _fast_query)
    monkeypatch.setattr(
        "ptc_agent.agent.middleware.background_subagent.spawn.reserve_subagent_slot",
        _fast_reserve,
    )
    return clock


class TestConfigAndDefaults:
    def test_defaults_and_config_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert DEFAULT_MAX_RUNNING_PER_THREAD == 8
        assert DEFAULT_MAX_RUNNING_GLOBAL == 24
        assert LEASE_TTL_S == 90.0
        assert RENEW_INTERVAL_S == 30.0

        limits = get_subagent_limits()
        assert limits == SubagentLimits(
            max_running_per_thread=8,
            max_running_global=24,
        )

        overrides = {
            "subagent_limits.max_running_per_thread": 4,
            "subagent_limits.max_running_global": 12,
        }
        monkeypatch.setattr(
            subagent_slots,
            "get_nested_config",
            lambda key, default=None: overrides.get(key, default),
        )
        assert get_subagent_limits() == SubagentLimits(
            max_running_per_thread=4,
            max_running_global=12,
        )


class TestPerThreadCapAndFIFOQueue:
    @pytest.mark.asyncio
    async def test_ninth_task_queues_and_starts_after_one_finishes_in_fifo_order(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fast_polling: ControllableClock,
    ) -> None:
        cache, engine = make_fake_cache()
        monkeypatch.setattr(subagent_slots, "get_cache_client", lambda: cache)

        mw = FakeMiddleware(thread_id="thread-fifo")
        gates: dict[int, asyncio.Event] = {i: asyncio.Event() for i in range(1, 11)}
        started_order: list[int] = []
        started_events: dict[int, asyncio.Event] = {
            i: asyncio.Event() for i in range(1, 11)
        }

        tasks: list[BackgroundTask] = []
        messages: list[ToolMessage] = []

        for idx in range(1, 11):
            req = _make_tool_request(
                tool_call_id=f"tc-{idx}",
                thread_id="thread-fifo",
                description=f"Task {idx}",
            )

            async def _handler(_req: ToolCallRequest, i: int = idx) -> ToolMessage:
                started_order.append(i)
                started_events[i].set()
                await gates[i].wait()
                return ToolMessage(content=f"done-{i}", tool_call_id=f"tc-{i}")

            msg = await handle_task_action(mw, req, _handler)
            assert isinstance(msg, ToolMessage)
            messages.append(msg)
            task = mw.registry.get_by_tool_call_id(f"tc-{idx}")
            assert task is not None
            tasks.append(task)

        # Wait for the first 8 tasks to enter their handlers
        await asyncio.gather(*(started_events[i].wait() for i in range(1, 9)))
        await asyncio.sleep(0.03)

        # First 8 are running; 9th and 10th are queued and have NOT started their handlers
        assert started_order == [1, 2, 3, 4, 5, 6, 7, 8]
        assert not started_events[9].is_set()
        assert not started_events[10].is_set()
        assert is_subagent_queued(tasks[8])
        assert is_subagent_queued(tasks[9])
        assert engine._zcard(thread_slots_key("thread-fifo")) == 8
        assert engine._zcard(global_slots_key()) == 8

        # Tool messages for 9th and 10th tasks report queued status and queue position
        assert "Running in background" in messages[0].content
        assert (
            "已排队：本对话正在运行 8 个子代理（上限 8），前面还有 0 个在排队，会自动开始，不要重复派发"
            in messages[8].content
        )
        assert (
            "已排队：本对话正在运行 8 个子代理（上限 8），前面还有 1 个在排队，会自动开始，不要重复派发"
            in messages[9].content
        )

        # Finish task 3 -> task 9 (first in FIFO queue) must start before task 10
        gates[3].set()
        await asyncio.wait_for(started_events[9].wait(), timeout=1.0)
        assert 9 in started_order
        assert not started_events[10].is_set()
        assert not is_subagent_queued(tasks[8])
        assert is_subagent_queued(tasks[9])
        assert engine._zcard(thread_slots_key("thread-fifo")) == 8

        # Finish task 5 -> task 10 starts next
        gates[5].set()
        await asyncio.wait_for(started_events[10].wait(), timeout=1.0)
        assert started_order == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

        # Release all remaining tasks and verify all Redis slots are cleaned up
        for i in range(1, 11):
            gates[i].set()
        await asyncio.gather(*(t.asyncio_task for t in tasks if t.asyncio_task))
        assert engine._zcard(thread_slots_key("thread-fifo")) == 0
        assert engine._zcard(global_slots_key()) == 0
        assert engine._zcard(queue_order_key("thread-fifo")) == 0


class TestGlobalCapAcrossThreads:
    @pytest.mark.asyncio
    async def test_three_threads_nine_tasks_each_never_exceed_24_global_or_8_per_thread(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fast_polling: ControllableClock,
    ) -> None:
        cache, engine = make_fake_cache()
        monkeypatch.setattr(subagent_slots, "get_cache_client", lambda: cache)

        middlewares = [FakeMiddleware(thread_id=f"thread-{t}") for t in range(1, 4)]
        active_global = 0
        max_seen_global = 0
        active_by_thread = {f"thread-{t}": 0 for t in range(1, 4)}
        max_seen_by_thread = {f"thread-{t}": 0 for t in range(1, 4)}

        release_first_wave = asyncio.Event()
        first_wave_ready = asyncio.Event()
        all_tasks: list[BackgroundTask] = []

        for t_idx, mw in enumerate(middlewares, start=1):
            t_id = f"thread-{t_idx}"
            for c_idx in range(1, 10):
                tc_id = f"{t_id}-tc-{c_idx}"
                req = _make_tool_request(tool_call_id=tc_id, thread_id=t_id)

                async def _handler(
                    _req: ToolCallRequest,
                    thread_id: str = t_id,
                ) -> ToolMessage:
                    nonlocal active_global, max_seen_global
                    active_global += 1
                    active_by_thread[thread_id] += 1
                    max_seen_global = max(max_seen_global, active_global)
                    max_seen_by_thread[thread_id] = max(
                        max_seen_by_thread[thread_id],
                        active_by_thread[thread_id],
                    )
                    if active_global == 24:
                        first_wave_ready.set()
                    try:
                        await release_first_wave.wait()
                        await asyncio.sleep(0.01)
                        return ToolMessage(content="ok", tool_call_id="x")
                    finally:
                        active_global -= 1
                        active_by_thread[thread_id] -= 1

                await handle_task_action(mw, req, _handler)
                task = mw.registry.get_by_tool_call_id(tc_id)
                assert task is not None
                all_tasks.append(task)

        await asyncio.wait_for(first_wave_ready.wait(), timeout=2.0)
        await asyncio.sleep(0.03)

        # Exactly 24 running globally (8 per thread), 3 queued (the 9th in each thread)
        assert active_global == 24
        assert engine._zcard(global_slots_key()) == 24
        for t_idx in range(1, 4):
            t_id = f"thread-{t_idx}"
            assert active_by_thread[t_id] == 8
            assert engine._zcard(thread_slots_key(t_id)) == 8
            assert engine._zcard(queue_order_key(t_id)) == 1

        queued_tasks = [t for t in all_tasks if is_subagent_queued(t)]
        assert len(queued_tasks) == 3

        # Let all tasks finish; the 3 queued tasks acquire slots and complete
        release_first_wave.set()
        await asyncio.gather(*(t.asyncio_task for t in all_tasks if t.asyncio_task))

        assert max_seen_global <= 24
        for t_id, peak in max_seen_by_thread.items():
            assert peak <= 8, f"{t_id} exceeded per-thread cap: {peak}"
        assert engine._zcard(global_slots_key()) == 0

    @pytest.mark.asyncio
    async def test_global_cap_queues_fourth_thread_even_when_thread_is_under_8(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fast_polling: ControllableClock,
    ) -> None:
        cache, engine = make_fake_cache()
        monkeypatch.setattr(subagent_slots, "get_cache_client", lambda: cache)

        # Fill 24 global slots across 3 threads (8 each)
        holders: list[BackgroundTask] = []
        for t_idx in range(1, 4):
            t_id = f"t-{t_idx}"
            reg = BackgroundTaskRegistry(thread_id=t_id)
            for c_idx in range(8):
                task = await reg.register(
                    tool_call_id=f"{t_id}-{c_idx}",
                    description="d",
                    prompt="p",
                    subagent_type="research",
                )
                lease = await reserve_subagent_slot(task, t_id, clock=fast_polling)
                assert lease is not None and not lease.is_queued
                holders.append(task)

        assert engine._zcard(global_slots_key()) == 24

        # Now thread 4 has 0 running in its thread, but global is 24/24 -> must queue!
        reg4 = BackgroundTaskRegistry(thread_id="t-4")
        t4_task = await reg4.register(
            tool_call_id="t4-0",
            description="d",
            prompt="p",
            subagent_type="research",
        )
        lease4 = await reserve_subagent_slot(t4_task, "t-4", clock=fast_polling)
        assert lease4 is not None
        assert lease4.is_queued
        notice = format_queued_notice(t4_task)
        assert "全后端正在运行 24/24" in notice

        # Release one global holder -> t4_task can now acquire
        await release_subagent_slot(holders[0])
        assert await lease4.try_acquire_once(mode="acquire")
        assert not lease4.is_queued

        for h in holders[1:]:
            await release_subagent_slot(h)
        await release_subagent_slot(t4_task)


class TestLeaseExpirationAndRenewal:
    @pytest.mark.asyncio
    async def test_crashed_holder_lease_expires_after_90s_without_real_sleep(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache, engine = make_fake_cache()
        monkeypatch.setattr(subagent_slots, "get_cache_client", lambda: cache)
        monkeypatch.setattr(
            subagent_slots,
            "get_subagent_limits",
            lambda: SubagentLimits(max_running_per_thread=1, max_running_global=24),
        )

        clock = ControllableClock(initial=10_000.0)
        reg = BackgroundTaskRegistry(thread_id="thread-lease")
        holder = await reg.register(
            tool_call_id="tc-holder",
            description="holder",
            prompt="p",
            subagent_type="research",
        )
        waiter = await reg.register(
            tool_call_id="tc-waiter",
            description="waiter",
            prompt="p",
            subagent_type="research",
        )

        # Holder acquires the single slot at t=10_000 (expires at 10_090)
        holder_lease = await reserve_subagent_slot(
            holder,
            "thread-lease",
            clock=clock,
            renew_interval_s=3600.0,  # simulate holder no longer renewing
        )
        assert holder_lease is not None and not holder_lease.is_queued

        # Waiter tries at t=10_000 -> queued
        waiter_lease = await reserve_subagent_slot(
            waiter,
            "thread-lease",
            clock=clock,
            renew_interval_s=3600.0,
        )
        assert waiter_lease is not None and waiter_lease.is_queued

        # At t=10_089 (89s elapsed < 90s TTL), slot is still held by crashed holder
        clock.advance(89.0)
        assert not await waiter_lease.try_acquire_once(mode="acquire")
        assert waiter_lease.is_queued

        # At t=10_090.1 (> 90s TTL), expired lease is purged and waiter acquires the slot
        clock.advance(1.1)
        assert await waiter_lease.try_acquire_once(mode="acquire")
        assert not waiter_lease.is_queued
        assert engine._zcard(thread_slots_key("thread-lease")) == 1
        assert engine._zscore(
            thread_slots_key("thread-lease"), waiter_lease.slot_member
        ) == pytest.approx(clock() + LEASE_TTL_S)

        await release_subagent_slot(waiter)
        await release_subagent_slot(holder)

    @pytest.mark.asyncio
    async def test_active_holder_renewal_every_30s_keeps_slot_alive_past_90s(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache, _ = make_fake_cache()
        monkeypatch.setattr(subagent_slots, "get_cache_client", lambda: cache)
        monkeypatch.setattr(
            subagent_slots,
            "get_subagent_limits",
            lambda: SubagentLimits(max_running_per_thread=1, max_running_global=24),
        )

        clock = ControllableClock(initial=20_000.0)
        reg = BackgroundTaskRegistry(thread_id="thread-renew")
        holder = await reg.register(
            tool_call_id="tc-holder",
            description="holder",
            prompt="p",
            subagent_type="research",
        )
        waiter = await reg.register(
            tool_call_id="tc-waiter",
            description="waiter",
            prompt="p",
            subagent_type="research",
        )

        holder_lease = await reserve_subagent_slot(
            holder,
            "thread-renew",
            clock=clock,
            renew_interval_s=0.02,
        )
        assert holder_lease is not None and not holder_lease.is_queued

        waiter_lease = await reserve_subagent_slot(
            waiter,
            "thread-renew",
            clock=clock,
            renew_interval_s=3600.0,
        )
        assert waiter_lease is not None and waiter_lease.is_queued

        # Simulate 3 renewal ticks at t+30s, t+60s, t+90s, then check at t+120s
        for _ in range(4):
            clock.advance(30.0)
            await asyncio.sleep(0.03)
            # Waiter refreshes its own queue lease too, but cannot take the active slot
            assert not await waiter_lease.try_acquire_once(mode="acquire")

        assert waiter_lease.is_queued
        await release_subagent_slot(holder)
        assert await waiter_lease.try_acquire_once(mode="acquire")
        await release_subagent_slot(waiter)


class TestCancelWhileQueuedAndExceptionRelease:
    @pytest.mark.asyncio
    async def test_cancel_while_queued_does_not_occupy_or_leak_slot(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fast_polling: ControllableClock,
    ) -> None:
        cache, engine = make_fake_cache()
        monkeypatch.setattr(subagent_slots, "get_cache_client", lambda: cache)
        monkeypatch.setattr(
            subagent_slots,
            "get_subagent_limits",
            lambda: SubagentLimits(max_running_per_thread=1, max_running_global=24),
        )

        mw = FakeMiddleware(thread_id="thread-cancel")
        gate1 = asyncio.Event()
        started1 = asyncio.Event()
        started2 = asyncio.Event()
        started3 = asyncio.Event()

        async def _h1(_req: ToolCallRequest) -> ToolMessage:
            started1.set()
            await gate1.wait()
            return ToolMessage(content="ok-1", tool_call_id="tc-1")

        async def _h2(_req: ToolCallRequest) -> ToolMessage:
            started2.set()
            return ToolMessage(content="ok-2", tool_call_id="tc-2")

        async def _h3(_req: ToolCallRequest) -> ToolMessage:
            started3.set()
            return ToolMessage(content="ok-3", tool_call_id="tc-3")

        await handle_task_action(mw, _make_tool_request(tool_call_id="tc-1"), _h1)
        await handle_task_action(mw, _make_tool_request(tool_call_id="tc-2"), _h2)
        await handle_task_action(mw, _make_tool_request(tool_call_id="tc-3"), _h3)

        t1 = mw.registry.get_by_tool_call_id("tc-1")
        t2 = mw.registry.get_by_tool_call_id("tc-2")
        t3 = mw.registry.get_by_tool_call_id("tc-3")
        assert t1 and t2 and t3

        await asyncio.wait_for(started1.wait(), timeout=1.0)
        assert is_subagent_queued(t2)
        assert is_subagent_queued(t3)
        assert engine._zcard(queue_order_key("thread-cancel")) == 2

        # Cancel t2 while it is queued (non-force cancel_task and force cancel both work)
        cancelled = await mw.registry.cancel_task(t2.task_id, force=False)
        assert cancelled is True
        assert t2.asyncio_task is not None
        await asyncio.wait([t2.asyncio_task], timeout=1.0)
        assert t2.asyncio_task.done()

        # t2 never ran its handler and removed its queue entry in Redis
        assert not started2.is_set()
        assert t2.cancelled
        assert engine._zcard(queue_order_key("thread-cancel")) == 1
        assert engine._zcard(queue_lease_key("thread-cancel")) == 1

        # Now finish t1 -> t3 immediately acquires the slot and finishes
        gate1.set()
        await asyncio.wait_for(started3.wait(), timeout=1.0)
        await asyncio.gather(
            *(t.asyncio_task for t in (t1, t3) if t.asyncio_task is not None)
        )

        assert engine._zcard(thread_slots_key("thread-cancel")) == 0
        assert engine._zcard(global_slots_key()) == 0
        assert engine._zcard(queue_order_key("thread-cancel")) == 0
        assert engine._zcard(queue_lease_key("thread-cancel")) == 0

    @pytest.mark.asyncio
    async def test_exception_in_running_subagent_releases_slot_in_finally(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fast_polling: ControllableClock,
    ) -> None:
        cache, engine = make_fake_cache()
        monkeypatch.setattr(subagent_slots, "get_cache_client", lambda: cache)
        monkeypatch.setattr(
            subagent_slots,
            "get_subagent_limits",
            lambda: SubagentLimits(max_running_per_thread=1, max_running_global=24),
        )

        mw = FakeMiddleware(thread_id="thread-exc")
        boom_trigger = asyncio.Event()
        started1 = asyncio.Event()
        started2 = asyncio.Event()

        async def _failing_handler(_req: ToolCallRequest) -> ToolMessage:
            started1.set()
            await boom_trigger.wait()
            raise RuntimeError("subagent exploded")

        async def _succeeding_handler(_req: ToolCallRequest) -> ToolMessage:
            started2.set()
            return ToolMessage(content="recovered", tool_call_id="tc-2")

        await handle_task_action(
            mw, _make_tool_request(tool_call_id="tc-1"), _failing_handler
        )
        await handle_task_action(
            mw, _make_tool_request(tool_call_id="tc-2"), _succeeding_handler
        )

        t1 = mw.registry.get_by_tool_call_id("tc-1")
        t2 = mw.registry.get_by_tool_call_id("tc-2")
        assert t1 and t2

        await asyncio.wait_for(started1.wait(), timeout=1.0)
        assert is_subagent_queued(t2)

        # Trigger exception in t1 -> slot is released in finally -> t2 acquires and runs
        boom_trigger.set()
        await asyncio.wait_for(started2.wait(), timeout=1.0)
        await asyncio.gather(
            *(t.asyncio_task for t in (t1, t2) if t.asyncio_task is not None)
        )

        assert engine._zcard(thread_slots_key("thread-exc")) == 0
        assert engine._zcard(global_slots_key()) == 0


class TestRedisUnavailableFailOpen:
    @pytest.mark.asyncio
    async def test_redis_disabled_or_disconnected_allows_execution_and_logs_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cache, _ = make_fake_cache(enabled=False, with_client=False)
        monkeypatch.setattr(subagent_slots, "get_cache_client", lambda: cache)
        monkeypatch.setattr(
            subagent_slots,
            "get_subagent_limits",
            lambda: SubagentLimits(max_running_per_thread=1, max_running_global=1),
        )

        mw = FakeMiddleware(thread_id="thread-redis-down")
        ran = 0

        async def _handler(_req: ToolCallRequest) -> ToolMessage:
            nonlocal ran
            ran += 1
            return ToolMessage(content="ok", tool_call_id="tc")

        with caplog.at_level(logging.WARNING, logger="src.dalton.subagent_slots"):
            for i in range(3):
                msg = await handle_task_action(
                    mw,
                    _make_tool_request(tool_call_id=f"tc-{i}"),
                    _handler,
                )
                assert "Running in background" in msg.content

        tasks = [mw.registry.get_by_tool_call_id(f"tc-{i}") for i in range(3)]
        await asyncio.gather(*(t.asyncio_task for t in tasks if t and t.asyncio_task))
        assert ran == 3
        assert any(
            "Redis unavailable for subagent slot control" in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        )

    @pytest.mark.asyncio
    async def test_redis_eval_exception_fails_open_and_logs_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cache, engine = make_fake_cache()
        engine.fail_with = redis_async.ConnectionError("connection refused")
        monkeypatch.setattr(subagent_slots, "get_cache_client", lambda: cache)

        reg = BackgroundTaskRegistry(thread_id="thread-err")
        task = await reg.register(
            tool_call_id="tc-err",
            description="d",
            prompt="p",
            subagent_type="research",
        )

        with caplog.at_level(logging.WARNING, logger="src.dalton.subagent_slots"):
            lease = await reserve_subagent_slot(task, "thread-err")

        assert lease is not None
        assert not lease.is_queued
        assert lease.fail_open is True
        assert any(
            "Redis eval failed during subagent slot acquire" in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        )


class TestWorkflowChildrenAndTimeouts:
    @pytest.mark.asyncio
    async def test_workflow_children_count_toward_caps_while_workflow_driver_does_not(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fast_polling: ControllableClock,
    ) -> None:
        cache, engine = make_fake_cache()
        monkeypatch.setattr(subagent_slots, "get_cache_client", lambda: cache)
        monkeypatch.setattr(
            subagent_slots,
            "get_subagent_limits",
            lambda: SubagentLimits(max_running_per_thread=2, max_running_global=24),
        )

        mw = FakeMiddleware(thread_id="thread-wf")

        # 1. A workflow driver task (subagent_type == "workflow") does NOT consume a child slot
        wf_task = await mw.registry.register(
            tool_call_id="wf-driver-1",
            description="workflow driver",
            prompt="run workflow",
            subagent_type=WORKFLOW_SUBAGENT_TYPE,
        )
        driver_done = asyncio.Event()

        async def _wf_runner(_req: Any) -> dict[str, Any]:
            await driver_done.wait()
            return {"success": True, "result": "wf complete"}

        await spawn_task_writer(
            mw,
            wf_task,
            _wf_runner,
            prompt="run workflow",
            label="Workflow",
            name="wf",
            action="workflow",
        )
        assert getattr(wf_task, "slot_lease", None) is None
        assert engine._zcard(thread_slots_key("thread-wf")) == 0

        # 2. Dispatch 1 direct Task + 2 workflow child subagents in the same thread (cap=2)
        gate_direct = asyncio.Event()
        started_direct = asyncio.Event()

        async def _direct_handler(_req: ToolCallRequest) -> ToolMessage:
            started_direct.set()
            await gate_direct.wait()
            return ToolMessage(content="direct done", tool_call_id="tc-direct")

        await handle_task_action(
            mw,
            _make_tool_request(tool_call_id="tc-direct", thread_id="thread-wf"),
            _direct_handler,
        )
        await asyncio.wait_for(started_direct.wait(), timeout=1.0)
        assert engine._zcard(thread_slots_key("thread-wf")) == 1

        child1_gate = asyncio.Event()
        child1_started = asyncio.Event()
        child2_started = asyncio.Event()

        class _GatedGraph:
            def __init__(
                self, started: asyncio.Event, gate: asyncio.Event | None = None
            ) -> None:
                self._started = started
                self._gate = gate

            async def astream(self, state, config, stream_mode=None, durability=None):
                self._started.set()
                if self._gate is not None:
                    await self._gate.wait()
                yield ("values", {"messages": [AIMessage(content="child done")]})

        graphs = {
            "research": _GatedGraph(child1_started, child1_gate),
            "data-prep": _GatedGraph(child2_started),
        }

        child1 = await dispatch_background_subagent(
            mw,
            graphs,
            subagent_type="research",
            description="wf child 1",
            prompt="p1",
            parent_thread_id="thread-wf",
            run_id="run-1",
            owner_task_id=wf_task.task_id,
        )
        child2 = await dispatch_background_subagent(
            mw,
            graphs,
            subagent_type="data-prep",
            description="wf child 2",
            prompt="p2",
            parent_thread_id="thread-wf",
            run_id="run-1",
            owner_task_id=wf_task.task_id,
        )

        await asyncio.wait_for(child1_started.wait(), timeout=1.0)
        # Thread cap is 2 (direct + child1), so child2 is queued!
        assert not is_subagent_queued(child1)
        assert is_subagent_queued(child2)
        assert not child2_started.is_set()
        assert engine._zcard(thread_slots_key("thread-wf")) == 2

        # Finish child1 -> child2 acquires slot and completes
        child1_gate.set()
        await asyncio.wait_for(child2_started.wait(), timeout=1.0)

        gate_direct.set()
        driver_done.set()
        direct_task = mw.registry.get_by_tool_call_id("tc-direct")
        await asyncio.gather(
            *(
                t.asyncio_task
                for t in (wf_task, direct_task, child1, child2)
                if t and t.asyncio_task
            )
        )
        assert engine._zcard(thread_slots_key("thread-wf")) == 0

    @pytest.mark.asyncio
    async def test_time_limit_and_child_timeout_start_when_slot_acquired_not_while_queued(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fast_polling: ControllableClock,
    ) -> None:
        cache, _ = make_fake_cache()
        monkeypatch.setattr(subagent_slots, "get_cache_client", lambda: cache)
        monkeypatch.setattr(
            subagent_slots,
            "get_subagent_limits",
            lambda: SubagentLimits(max_running_per_thread=1, max_running_global=24),
        )

        mw = FakeMiddleware(thread_id="thread-timeout")

        # Occupy the single slot for 0.15s while child_timeout / time_limit is 0.08s!
        # If the timeout started while queued, the queued child would time out before
        # ever getting to run. Because the clock starts on slot acquisition, it succeeds!
        holder = await mw.registry.register(
            tool_call_id="tc-holder",
            description="holder",
            prompt="p",
            subagent_type="research",
        )
        await reserve_subagent_slot(holder, "thread-timeout", clock=fast_polling)

        recorded_deadline: list[float] = []

        class _QuickGraph:
            async def astream(self, state, config, stream_mode=None, durability=None):
                configurable = config.get("configurable") or {}
                recorded_deadline.append(float(configurable[TURN_DEADLINE_KEY]))
                await asyncio.sleep(0.01)
                yield ("values", {"messages": [AIMessage(content="quick answer")]})

        child_task = await dispatch_background_subagent(
            mw,
            {"research": _QuickGraph()},
            subagent_type="research",
            description="timed child",
            prompt="p",
            parent_thread_id="thread-timeout",
            run_id="run-1",
            owner_task_id="wf-1",
            time_limit=0.08,
        )
        assert is_subagent_queued(child_task)

        # Wait 0.15s (longer than child_timeout=0.08s) while child_task is queued
        async def _release_holder_later() -> None:
            await asyncio.sleep(0.15)
            await release_subagent_slot(holder)

        releaser = asyncio.create_task(_release_holder_later())

        # Drive WorkflowDriver._await_child with child_timeout=0.08s
        caps = WorkflowOrchestrationConfig.model_construct(
            child_timeout=0.08, run_timeout=10.0, max_summary_bytes=4096
        )
        dispatcher = create_autospec(SubagentDispatcher, instance=True)
        spec = WorkflowRunSpec(
            run_task=holder,
            registry=mw.registry,
            dispatcher=dispatcher,
            backend=None,
            thread_id="thread-timeout",
            short_thread_id="thread-t",
            script="",
            meta=WorkflowMeta(name="wf", description="wf"),
            caps=caps,
        )
        driver = WorkflowDriver(spec)
        child_run = _ChildRun(
            seq=1,
            subagent_type="research",
            label="timed child",
            phase=None,
            started_at=0.0,
            task=child_task,
        )

        before_release = asyncio.get_running_loop().time()
        await driver._await_child(child_run)
        await releaser

        assert child_run.error is None
        assert child_run.content == "quick answer"
        # recorded_deadline was computed after the 0.15s queue wait, not at dispatch
        assert len(recorded_deadline) == 1
        assert child_run.started_at > 0.0
        assert asyncio.get_running_loop().time() - before_release >= 0.14


class TestTaskOutputQueuedDisplay:
    @pytest.mark.asyncio
    async def test_task_output_shows_queued_state_and_notice(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fast_polling: ControllableClock,
    ) -> None:
        cache, _ = make_fake_cache()
        monkeypatch.setattr(subagent_slots, "get_cache_client", lambda: cache)
        monkeypatch.setattr(
            subagent_slots,
            "get_subagent_limits",
            lambda: SubagentLimits(max_running_per_thread=1, max_running_global=24),
        )

        mw = FakeMiddleware(thread_id="thread-output")
        gate = asyncio.Event()
        started = asyncio.Event()

        async def _handler(_req: ToolCallRequest) -> ToolMessage:
            started.set()
            await gate.wait()
            return ToolMessage(content="finished", tool_call_id="tc")

        await handle_task_action(
            mw, _make_tool_request(tool_call_id="tc-1", thread_id="thread-output"), _handler
        )
        await handle_task_action(
            mw, _make_tool_request(tool_call_id="tc-2", thread_id="thread-output"), _handler
        )

        t1 = mw.registry.get_by_tool_call_id("tc-1")
        t2 = mw.registry.get_by_tool_call_id("tc-2")
        assert t1 and t2
        await asyncio.wait_for(started.wait(), timeout=1.0)

        mw_spec = create_autospec(BackgroundSubagentMiddleware, instance=True)
        mw_spec.registry = mw.registry
        tool = create_task_output_tool(mw_spec)

        # B7: the overview lives in ThreadStatus's task listing — 1 running,
        # 1 queued (排队中) with its rank, read from Redis across workers.
        _patch_reporting_tasks(monkeypatch, [t1, t2], "thread-output")
        overview_text = await _thread_status_tasks("thread-output", mw.registry)
        queued_lines = [ln for ln in overview_text.splitlines() if "排队中" in ln]
        assert len(queued_lines) == 1
        assert f"Task-{t2.task_id}" in queued_lines[0]
        assert "queued (排队中), 0 ahead of it, auto-starts" in queued_lines[0]
        assert f"Task-{t1.task_id}" in overview_text

        # Single-task TaskOutput on queued task shows [QUEUED] (排队中)
        single_text, single_mark = await tool.coroutine(task_id=t2.task_id)
        assert single_mark is not None
        assert "[QUEUED] (排队中)" in single_text
        assert (
            "已排队：本对话正在运行 1 个子代理（上限 1），前面还有 0 个在排队，会自动开始，不要重复派发"
            in single_text
        )

        gate.set()
        await asyncio.gather(
            *(t.asyncio_task for t in (t1, t2) if t.asyncio_task is not None)
        )

    @pytest.mark.asyncio
    async def test_resume_action_also_queues_and_returns_queued_notice(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fast_polling: ControllableClock,
    ) -> None:
        cache, _ = make_fake_cache()
        monkeypatch.setattr(subagent_slots, "get_cache_client", lambda: cache)
        monkeypatch.setattr(
            subagent_slots,
            "get_subagent_limits",
            lambda: SubagentLimits(max_running_per_thread=1, max_running_global=24),
        )

        mw = FakeMiddleware(thread_id="thread-resume")

        # Complete an initial task so we can resume it later
        async def _quick_handler(_req: ToolCallRequest) -> ToolMessage:
            return ToolMessage(content="initial done", tool_call_id="tc-init")

        await handle_task_action(
            mw,
            _make_tool_request(tool_call_id="tc-init", thread_id="thread-resume"),
            _quick_handler,
        )
        resumable = mw.registry.get_by_tool_call_id("tc-init")
        assert resumable is not None and resumable.asyncio_task is not None
        await resumable.asyncio_task
        resumable.adopt_writer_outcome(resumable.asyncio_task)

        # Now occupy the single slot with another running task
        holder_gate = asyncio.Event()
        holder_started = asyncio.Event()

        async def _holder_handler(_req: ToolCallRequest) -> ToolMessage:
            holder_started.set()
            await holder_gate.wait()
            return ToolMessage(content="holder done", tool_call_id="tc-holder")

        await handle_task_action(
            mw,
            _make_tool_request(tool_call_id="tc-holder", thread_id="thread-resume"),
            _holder_handler,
        )
        await asyncio.wait_for(holder_started.wait(), timeout=1.0)

        # Resume the first task while the slot is occupied -> must queue and report queued notice
        resumed_started = asyncio.Event()

        async def _resume_handler(_req: ToolCallRequest) -> ToolMessage:
            resumed_started.set()
            return ToolMessage(content="resumed done", tool_call_id="tc-resume")

        resume_msg = await handle_task_action(
            mw,
            _make_tool_request(
                tool_call_id="tc-resume",
                thread_id="thread-resume",
                action="resume",
                task_id=resumable.task_id,
            ),
            _resume_handler,
        )
        assert isinstance(resume_msg, ToolMessage)
        assert (
            "已排队：本对话正在运行 1 个子代理（上限 1），前面还有 0 个在排队，会自动开始，不要重复派发"
            in resume_msg.content
        )
        assert not resumed_started.is_set()

        holder_gate.set()
        await asyncio.wait_for(resumed_started.wait(), timeout=1.0)
        holder_task = mw.registry.get_by_tool_call_id("tc-holder")
        await asyncio.gather(
            *(
                t.asyncio_task
                for t in (holder_task, resumable)
                if t and t.asyncio_task
            )
        )


class _RunLedgerSpec:
    """Typed spec for ``registry.run_ledger`` used with ``create_autospec``."""

    async def get_latest_run(self, task_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    async def mark_result_delivered(self, task_run_id: str) -> None:
        raise NotImplementedError


class TestRound2Fixes:
    @pytest.mark.asyncio
    async def test_renew_returning_zero_reregisters_running_task_and_counts_accurately(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Fix 1a: When renew returns 0 (lease expired during stall/disconnect),
        renew_once re-registers the running task via 'reregister' (even above cap),
        logs WARNING, and subsequent tasks see the accurate occupancy count.
        """
        cache, engine = make_fake_cache()
        monkeypatch.setattr(subagent_slots, "get_cache_client", lambda: cache)
        monkeypatch.setattr(
            subagent_slots,
            "get_subagent_limits",
            lambda: SubagentLimits(max_running_per_thread=1, max_running_global=24),
        )

        clock = ControllableClock(initial=50_000.0)
        reg = BackgroundTaskRegistry(thread_id="thread-rereg")
        t1 = await reg.register(
            tool_call_id="tc-1",
            description="running-1",
            prompt="p",
            subagent_type="research",
        )
        lease1 = await reserve_subagent_slot(
            t1,
            "thread-rereg",
            clock=clock,
            renew_interval_s=3600.0,
        )
        assert lease1 is not None and not lease1.is_queued
        assert engine._zcard(thread_slots_key("thread-rereg")) == 1

        # Advance past 90s TTL and simulate Redis purging the expired entry
        clock.advance(95.0)
        engine._zremrangebyscore(thread_slots_key("thread-rereg"), clock())
        engine._zremrangebyscore(global_slots_key(), clock())
        assert engine._zcard(thread_slots_key("thread-rereg")) == 0

        # When t1's renewal fires, mode='renew' returns 0 -> falls back to mode='reregister'
        with caplog.at_level(logging.WARNING, logger="src.dalton.subagent_slots"):
            await lease1.renew_once()

        assert engine._zcard(thread_slots_key("thread-rereg")) == 1
        assert engine._zcard(global_slots_key()) == 1
        assert not lease1.is_queued
        assert any(
            "Subagent slot lease was lost in Redis while task still running"
            in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        )

        # Now a new task t2 sees thread_running == 1 and queues instead of exceeding cap
        t2 = await reg.register(
            tool_call_id="tc-2",
            description="new-2",
            prompt="p",
            subagent_type="research",
        )
        lease2 = await reserve_subagent_slot(
            t2,
            "thread-rereg",
            clock=clock,
            renew_interval_s=3600.0,
        )
        assert lease2 is not None and lease2.is_queued
        assert lease2.thread_running == 1

        # Also verify reregister writes even when another task took the slot during the outage
        clock.advance(95.0)
        # t2 acquires the slot at t+190s after t1's second expiry
        assert await lease2.try_acquire_once(mode="acquire")
        assert engine._zcard(thread_slots_key("thread-rereg")) == 1
        # Now t1 wakes up and re-registers -> both t1 and t2 are counted (2 > cap 1), t1 does NOT queue
        await lease1.renew_once()
        assert not lease1.is_queued
        assert engine._zcard(thread_slots_key("thread-rereg")) == 2
        assert engine._zcard(global_slots_key()) == 2

        await release_subagent_slot(t1)
        await release_subagent_slot(t2)

    @pytest.mark.asyncio
    async def test_fail_open_task_starts_renewal_and_reregisters_when_redis_recovers(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Fix 1b: A task admitted under fail-open starts its renewal loop and
        registers itself in Redis via 'reregister' as soon as Redis recovers.
        """
        cache, engine = make_fake_cache()
        engine.fail_with = redis_async.ConnectionError("redis temporarily down")
        monkeypatch.setattr(subagent_slots, "get_cache_client", lambda: cache)
        monkeypatch.setattr(
            subagent_slots,
            "get_subagent_limits",
            lambda: SubagentLimits(max_running_per_thread=1, max_running_global=24),
        )

        clock = ControllableClock(initial=60_000.0)
        reg = BackgroundTaskRegistry(thread_id="thread-failopen-rec")
        t1 = await reg.register(
            tool_call_id="tc-fo-1",
            description="failopen task",
            prompt="p",
            subagent_type="research",
        )
        lease1 = await reserve_subagent_slot(
            t1,
            "thread-failopen-rec",
            clock=clock,
            renew_interval_s=3600.0,
        )
        assert lease1 is not None
        assert lease1.fail_open is True
        assert lease1._renew_task is not None and not lease1._renew_task.done()
        assert engine._zcard(thread_slots_key("thread-failopen-rec")) == 0

        # Redis recovers -> first renewal tick runs 'reregister' and records t1 in both ZSETs
        engine.fail_with = None
        clock.advance(30.0)
        with caplog.at_level(logging.WARNING, logger="src.dalton.subagent_slots"):
            await lease1.renew_once()

        assert lease1.fail_open is False
        assert lease1.registered_in_redis is True
        assert engine._zcard(thread_slots_key("thread-failopen-rec")) == 1
        assert engine._zcard(global_slots_key()) == 1
        assert any(
            "Re-registered fail-open subagent slot in Redis after recovery"
            in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        )

        # Subsequent task t2 now sees the recovered occupancy and queues
        t2 = await reg.register(
            tool_call_id="tc-fo-2",
            description="post-recovery task",
            prompt="p",
            subagent_type="research",
        )
        lease2 = await reserve_subagent_slot(
            t2,
            "thread-failopen-rec",
            clock=clock,
            renew_interval_s=3600.0,
        )
        assert lease2 is not None and lease2.is_queued
        assert lease2.thread_running == 1

        await release_subagent_slot(t1)
        await release_subagent_slot(t2)

    @pytest.mark.asyncio
    async def test_workflow_child_queued_heartbeat_propagates_to_parent_workflow_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fast_polling: ControllableClock,
    ) -> None:
        """Fix 2: While a workflow child is queued, polling calls registry.note_alive(task)
        without pre-writing task.alive_at, so the parent workflow task's alive_at is updated.
        """
        import time as _time

        cache, _ = make_fake_cache()
        monkeypatch.setattr(subagent_slots, "get_cache_client", lambda: cache)
        monkeypatch.setattr(
            subagent_slots,
            "get_subagent_limits",
            lambda: SubagentLimits(max_running_per_thread=1, max_running_global=24),
        )

        mw = FakeMiddleware(thread_id="thread-wf-hb")
        holder = await mw.registry.register(
            tool_call_id="tc-holder",
            description="holder",
            prompt="p",
            subagent_type="research",
        )
        await reserve_subagent_slot(holder, "thread-wf-hb", clock=fast_polling)

        parent_wf = await mw.registry.register(
            tool_call_id="tc-wf-parent",
            description="workflow parent",
            prompt="p",
            subagent_type=WORKFLOW_SUBAGENT_TYPE,
        )
        child = await mw.registry.register(
            tool_call_id="tc-wf-child",
            description="workflow child",
            prompt="p",
            subagent_type="research",
            owner_task_id=parent_wf.task_id,
        )
        child_lease = await subagent_slots.reserve_subagent_slot(
            child, "thread-wf-hb", clock=fast_polling
        )
        assert child_lease is not None and child_lease.is_queued

        # Backdate alive_at by 10s (> 5s _ALIVE_STAMP_INTERVAL_S) on both child and parent
        stale_ts = _time.time() - 10.0
        parent_wf.alive_at = stale_ts
        child.alive_at = stale_ts

        wait_task = asyncio.create_task(
            subagent_slots.wait_for_subagent_slot(child, registry=mw.registry)
        )
        await asyncio.sleep(0.04)

        # Both child.alive_at and parent_wf.alive_at must have been refreshed while queued
        assert child.alive_at > stale_ts + 5.0
        assert parent_wf.alive_at > stale_ts + 5.0

        await release_subagent_slot(holder)
        await asyncio.wait_for(wait_task, timeout=1.0)
        await release_subagent_slot(child)

    @pytest.mark.asyncio
    async def test_cancel_or_lost_response_during_initial_acquire_leaves_no_residual_members(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fix 3: CancelledError or lost response after Redis executes ACQUIRE_SLOT_LUA
        still cleans up the slot/queue member via idempotent release.
        """
        cache, engine = make_fake_cache()
        monkeypatch.setattr(subagent_slots, "get_cache_client", lambda: cache)
        monkeypatch.setattr(
            subagent_slots,
            "get_subagent_limits",
            lambda: SubagentLimits(max_running_per_thread=1, max_running_global=24),
        )

        mw = FakeMiddleware(thread_id="thread-cancel-acq")

        # Case A: CancelledError right after Redis Lua wrote the slot into thread/global ZSETs
        t_cancel = await mw.registry.register(
            tool_call_id="tc-cancel-acq",
            description="cancelled mid-acquire",
            prompt="p",
            subagent_type="research",
        )
        engine.after_execute_error = asyncio.CancelledError("cancelled awaiting eval")

        async def _noop_runner(_req: Any) -> str:
            return "ok"

        with pytest.raises(asyncio.CancelledError):
            await spawn_task_writer(
                mw,
                t_cancel,
                _noop_runner,
                prompt="p",
                label="Task",
                name="t-cancel",
                action="init",
            )

        # No residual slot or queue member in Redis
        assert engine._zcard(thread_slots_key("thread-cancel-acq")) == 0
        assert engine._zcard(global_slots_key()) == 0
        assert engine._zcard(queue_order_key("thread-cancel-acq")) == 0
        assert engine._zcard(queue_lease_key("thread-cancel-acq")) == 0

        # Case B: Response lost (TimeoutError) after Redis Lua wrote the slot -> fails open,
        # and subsequent release() still removes the Redis entry because registered_in_redis=True
        t_lost = await mw.registry.register(
            tool_call_id="tc-lost-resp",
            description="lost response",
            prompt="p",
            subagent_type="research",
        )
        engine.after_execute_error = redis_async.TimeoutError(
            "socket timed out after server executed Lua"
        )
        lease_lost = await reserve_subagent_slot(
            t_lost, "thread-cancel-acq", renew_interval_s=3600.0
        )
        assert lease_lost is not None
        assert lease_lost.fail_open is True
        assert lease_lost.registered_in_redis is True
        # Server had written the member before the response timed out
        assert engine._zcard(thread_slots_key("thread-cancel-acq")) == 1

        # Releasing the task cleans up the server-side entry
        await release_subagent_slot(t_lost)
        assert engine._zcard(thread_slots_key("thread-cancel-acq")) == 0
        assert engine._zcard(global_slots_key()) == 0

    @pytest.mark.asyncio
    async def test_cross_worker_task_output_without_local_lease_reads_queued_state_from_redis(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fast_polling: ControllableClock,
    ) -> None:
        """Fix 4: When TaskOutput runs on another worker (no local task.slot_lease),
        it queries Redis by deterministic slot_member and displays [QUEUED] (排队中) and rank.
        """
        cache, _ = make_fake_cache()
        monkeypatch.setattr(subagent_slots, "get_cache_client", lambda: cache)
        monkeypatch.setattr(
            subagent_slots,
            "get_subagent_limits",
            lambda: SubagentLimits(max_running_per_thread=1, max_running_global=24),
        )

        # Worker A spawns 1 running task and 2 queued tasks
        mw_worker_a = FakeMiddleware(thread_id="thread-xworker")
        gate = asyncio.Event()
        started = asyncio.Event()

        async def _handler(_req: ToolCallRequest) -> ToolMessage:
            started.set()
            await gate.wait()
            return ToolMessage(content="done", tool_call_id="tc")

        for i in range(1, 4):
            await handle_task_action(
                mw_worker_a,
                _make_tool_request(tool_call_id=f"tc-{i}", thread_id="thread-xworker"),
                _handler,
            )

        t1_a = mw_worker_a.registry.get_by_tool_call_id("tc-1")
        t2_a = mw_worker_a.registry.get_by_tool_call_id("tc-2")
        t3_a = mw_worker_a.registry.get_by_tool_call_id("tc-3")
        assert t1_a and t2_a and t3_a
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert is_subagent_queued(t2_a)
        assert is_subagent_queued(t3_a)

        # Worker B has checkpoint-hydrated shells with slot_lease=None and asyncio_task=None
        mw_worker_b = FakeMiddleware(thread_id="thread-xworker")
        for src_task in (t1_a, t2_a, t3_a):
            shell = BackgroundTask(
                task_id=src_task.task_id,
                tool_call_id=src_task.tool_call_id,
                description=src_task.description,
                prompt=src_task.prompt,
                subagent_type=src_task.subagent_type,
                task_run_id=src_task.task_run_id,
            )
            assert getattr(shell, "slot_lease", None) is None
            mw_worker_b.registry._tasks[shell.tool_call_id] = shell
            mw_worker_b.registry._task_id_to_tool_call_id[shell.task_id] = (
                shell.tool_call_id
            )

        mw_b_spec = create_autospec(BackgroundSubagentMiddleware, instance=True)
        mw_b_spec.registry = mw_worker_b.registry
        tool_b = create_task_output_tool(mw_b_spec)

        # 1. Hydrated shell without ledger: overview & single task query Redis
        overview_b, _ = await tool_b.coroutine(task_id=None)
        assert "1 running, 2 queued (排队中)" in overview_b
        assert "[QUEUED] (排队中)" in overview_b
        assert "前面还有 1 个在排队" in overview_b

        single_b, _ = await tool_b.coroutine(task_id=t3_a.task_id)
        assert "[QUEUED] (排队中)" in single_b
        assert (
            "已排队：本对话正在运行 1 个子代理（上限 1），前面还有 1 个在排队，会自动开始，不要重复派发"
            in single_b
        )

        # 2. Missing from local registry / resolved via ledger 'in_progress' on Worker B
        mw_worker_c = FakeMiddleware(thread_id="thread-xworker")
        ledger_c = create_autospec(_RunLedgerSpec, instance=True)
        ledger_c.get_latest_run.return_value = {
            "status": "in_progress",
            "task_run_id": t3_a.task_run_id,
        }
        mw_worker_c.registry.run_ledger = ledger_c
        mw_c_spec = create_autospec(BackgroundSubagentMiddleware, instance=True)
        mw_c_spec.registry = mw_worker_c.registry
        tool_c = create_task_output_tool(mw_c_spec)

        ledger_reply, ledger_mark = await tool_c.coroutine(task_id=t3_a.task_id)
        assert ledger_mark is not None
        assert "[QUEUED] (排队中" in ledger_reply
        assert (
            "已排队：本对话正在运行 1 个子代理（上限 1），前面还有 1 个在排队，会自动开始，不要重复派发"
            in ledger_reply
        )

        gate.set()
        await asyncio.gather(
            *(t.asyncio_task for t in (t1_a, t2_a, t3_a) if t.asyncio_task is not None)
        )


