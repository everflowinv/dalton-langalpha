"""Redis-backed concurrency caps and FIFO queueing for background subagents.

Enforces two cross-worker limits simultaneously:
- Per-thread cap (default 8, ``subagent_limits.max_running_per_thread``)
- Global cap across all backend workers (default 24, ``subagent_limits.max_running_global``)

Each running slot is a lease in two Redis ZSETs (thread-level and global) with
score = expiration timestamp (``now + 90s``), renewed every 30s while held and
released in ``finally`` on completion, failure, or cancellation. Excess tasks
enter a per-thread FIFO queue in Redis rather than being rejected, and their
own timeout clocks only begin once a slot is acquired. When Redis is disabled
or unreachable, slot acquisition fails open and logs a ``WARNING``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from src.config.settings import get_nested_config
from src.utils.cache.redis_cache import get_cache_client

logger = logging.getLogger(__name__)

DEFAULT_MAX_RUNNING_PER_THREAD = 8
DEFAULT_MAX_RUNNING_GLOBAL = 24

LEASE_TTL_S = 90.0
RENEW_INTERVAL_S = 30.0
POLL_INTERVAL_MIN_S = 1.0
POLL_INTERVAL_MAX_S = 2.0

WORKFLOW_DRIVER_SUBAGENT_TYPE = "workflow"
SlotAcquireMode = Literal["acquire", "renew", "reregister"]

# KEYS:
#   [1] thread_slots_key      (ZSET: member -> expires_at)
#   [2] global_slots_key      (ZSET: member -> expires_at)
#   [3] queue_order_key       (ZSET: member -> monotonic dispatch seq)
#   [4] queue_lease_key       (ZSET: member -> expires_at)
#   [5] queue_seq_key         (STRING: monotonic integer counter per thread)
# ARGV:
#   [1] slot_member
#   [2] now (float seconds)
#   [3] lease_ttl (float seconds)
#   [4] max_per_thread (int)
#   [5] max_global (int)
#   [6] mode ("acquire" | "renew" | "reregister")
# Returns:
#   {acquired (1|0), thread_running, global_running, queue_ahead}
ACQUIRE_SLOT_LUA = """
local thread_key = KEYS[1]
local global_key = KEYS[2]
local queue_order_key = KEYS[3]
local queue_lease_key = KEYS[4]
local seq_key = KEYS[5]

local member = ARGV[1]
local now = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local max_thread = tonumber(ARGV[4])
local max_global = tonumber(ARGV[5])
local mode = ARGV[6] or "acquire"
local expires_at = now + ttl
local key_ttl = math.max(300, math.ceil(ttl * 4))

redis.call('ZREMRANGEBYSCORE', thread_key, '-inf', now)
redis.call('ZREMRANGEBYSCORE', global_key, '-inf', now)

local expired_queued = redis.call('ZRANGEBYSCORE', queue_lease_key, '-inf', now)
if #expired_queued > 0 then
    for i = 1, #expired_queued do
        redis.call('ZREM', queue_order_key, expired_queued[i])
    end
    redis.call('ZREMRANGEBYSCORE', queue_lease_key, '-inf', now)
end

local in_thread = tonumber(redis.call('ZSCORE', thread_key, member))
local in_global = tonumber(redis.call('ZSCORE', global_key, member))
if in_thread and in_global then
    redis.call('ZADD', thread_key, expires_at, member)
    redis.call('ZADD', global_key, expires_at, member)
    redis.call('ZREM', queue_order_key, member)
    redis.call('ZREM', queue_lease_key, member)
    redis.call('EXPIRE', thread_key, key_ttl)
    redis.call('EXPIRE', global_key, key_ttl)
    local tr = tonumber(redis.call('ZCARD', thread_key)) or 0
    local gr = tonumber(redis.call('ZCARD', global_key)) or 0
    return {1, tr, gr, 0}
elseif in_thread or in_global then
    redis.call('ZREM', thread_key, member)
    redis.call('ZREM', global_key, member)
end

if mode == 'reregister' then
    redis.call('ZREM', queue_order_key, member)
    redis.call('ZREM', queue_lease_key, member)
    redis.call('ZADD', thread_key, expires_at, member)
    redis.call('ZADD', global_key, expires_at, member)
    redis.call('EXPIRE', thread_key, key_ttl)
    redis.call('EXPIRE', global_key, key_ttl)
    local tr = tonumber(redis.call('ZCARD', thread_key)) or 0
    local gr = tonumber(redis.call('ZCARD', global_key)) or 0
    return {1, tr, gr, 0}
end

local thread_running = tonumber(redis.call('ZCARD', thread_key)) or 0
local global_running = tonumber(redis.call('ZCARD', global_key)) or 0

if mode == 'renew' then
    return {0, thread_running, global_running, 0}
end

local existing_seq = tonumber(redis.call('ZSCORE', queue_order_key, member))
if not existing_seq then
    local seq = redis.call('INCR', seq_key)
    redis.call('ZADD', queue_order_key, seq, member)
end
redis.call('ZADD', queue_lease_key, expires_at, member)
redis.call('EXPIRE', queue_order_key, key_ttl)
redis.call('EXPIRE', queue_lease_key, key_ttl)
redis.call('EXPIRE', seq_key, key_ttl)

local rank = tonumber(redis.call('ZRANK', queue_order_key, member)) or 0
if rank == 0 and thread_running < max_thread and global_running < max_global then
    redis.call('ZREM', queue_order_key, member)
    redis.call('ZREM', queue_lease_key, member)
    redis.call('ZADD', thread_key, expires_at, member)
    redis.call('ZADD', global_key, expires_at, member)
    redis.call('EXPIRE', thread_key, key_ttl)
    redis.call('EXPIRE', global_key, key_ttl)
    return {1, thread_running + 1, global_running + 1, 0}
end

return {0, thread_running, global_running, rank}
"""

# KEYS:
#   [1] thread_slots_key
#   [2] global_slots_key
#   [3] queue_order_key
#   [4] queue_lease_key
# ARGV:
#   [1] slot_member
RELEASE_SLOT_LUA = """
local r1 = redis.call('ZREM', KEYS[1], ARGV[1])
local r2 = redis.call('ZREM', KEYS[2], ARGV[1])
local r3 = redis.call('ZREM', KEYS[3], ARGV[1])
local r4 = redis.call('ZREM', KEYS[4], ARGV[1])
if (r1 + r2 + r3 + r4) > 0 then
    return 1
end
return 0
"""

# KEYS:
#   [1] thread_slots_key
#   [2] global_slots_key
#   [3] queue_order_key
#   [4] queue_lease_key
# ARGV:
#   [1] exact_member ("{thread_id}:{task_id}:{task_run_id}" or "{thread_id}:{task_id}")
#   [2] base_member ("{thread_id}:{task_id}")
#   [3] now (float seconds)
# Returns:
#   {is_queued (1|0), thread_running, global_running, queue_ahead}
QUERY_QUEUE_LUA = """
local thread_key = KEYS[1]
local global_key = KEYS[2]
local queue_order_key = KEYS[3]
local queue_lease_key = KEYS[4]

local exact_member = ARGV[1]
local base_member = ARGV[2]
local now = tonumber(ARGV[3])

local matched = nil
if tonumber(redis.call('ZSCORE', queue_order_key, exact_member)) then
    matched = exact_member
elseif tonumber(redis.call('ZSCORE', queue_order_key, base_member)) then
    matched = base_member
else
    local prefix = base_member .. ':'
    local prefix_len = string.len(prefix)
    local members = redis.call('ZRANGE', queue_order_key, 0, -1)
    for i = 1, #members do
        if string.sub(members[i], 1, prefix_len) == prefix then
            matched = members[i]
            break
        end
    end
end

if not matched then
    return {0, 0, 0, 0}
end

local lease_exp = tonumber(redis.call('ZSCORE', queue_lease_key, matched))
if not lease_exp or lease_exp <= now then
    return {0, 0, 0, 0}
end

local members = redis.call('ZRANGE', queue_order_key, 0, -1)
local rank = 0
for i = 1, #members do
    local m = members[i]
    if m == matched then
        break
    end
    local exp = tonumber(redis.call('ZSCORE', queue_lease_key, m))
    if exp and exp > now then
        rank = rank + 1
    end
end

local thread_running = tonumber(redis.call('ZCOUNT', thread_key, '(' .. tostring(now), '+inf')) or 0
local global_running = tonumber(redis.call('ZCOUNT', global_key, '(' .. tostring(now), '+inf')) or 0
return {1, thread_running, global_running, rank}
"""


@dataclass(frozen=True)
class SubagentLimits:
    max_running_per_thread: int = DEFAULT_MAX_RUNNING_PER_THREAD
    max_running_global: int = DEFAULT_MAX_RUNNING_GLOBAL


@dataclass(frozen=True)
class QueuedSlotStatus:
    thread_running: int
    global_running: int
    queue_ahead: int
    max_per_thread: int
    max_global: int


def get_subagent_limits() -> SubagentLimits:
    """Load per-thread and global background subagent caps from config."""
    raw_thread = get_nested_config(
        "subagent_limits.max_running_per_thread",
        DEFAULT_MAX_RUNNING_PER_THREAD,
    )
    raw_global = get_nested_config(
        "subagent_limits.max_running_global",
        DEFAULT_MAX_RUNNING_GLOBAL,
    )
    try:
        max_thread = max(1, int(raw_thread))
    except (TypeError, ValueError):
        max_thread = DEFAULT_MAX_RUNNING_PER_THREAD
    try:
        max_global = max(1, int(raw_global))
    except (TypeError, ValueError):
        max_global = DEFAULT_MAX_RUNNING_GLOBAL
    return SubagentLimits(
        max_running_per_thread=max_thread,
        max_running_global=max_global,
    )


def thread_slots_key(thread_id: str) -> str:
    return f"dalton:subagent_slots:thread:{thread_id or 'default'}"


def global_slots_key() -> str:
    return "dalton:subagent_slots:global"


def queue_order_key(thread_id: str) -> str:
    return f"dalton:subagent_slots:queue_order:{thread_id or 'default'}"


def queue_lease_key(thread_id: str) -> str:
    return f"dalton:subagent_slots:queue_lease:{thread_id or 'default'}"


def queue_seq_key(thread_id: str) -> str:
    return f"dalton:subagent_slots:seq:{thread_id or 'default'}"


def slot_member_for_task(
    thread_id: str,
    task_id: str,
    task_run_id: str | None = None,
) -> str:
    """Build the deterministic cross-worker Redis ZSET member for a task run."""
    norm_thread_id = thread_id or "default"
    if task_run_id:
        return f"{norm_thread_id}:{task_id}:{task_run_id}"
    return f"{norm_thread_id}:{task_id}"


def _resolve_redis_client(*, warn_on_unavailable: bool = True) -> Any | None:
    try:
        cache = get_cache_client()
    except Exception as exc:
        if warn_on_unavailable:
            logger.warning(
                "Redis cache client lookup failed for subagent slot control: %s; "
                "allowing subagent without concurrency cap.",
                exc,
            )
        return None
    if not getattr(cache, "enabled", False) or getattr(cache, "client", None) is None:
        if warn_on_unavailable:
            logger.warning(
                "Redis unavailable for subagent slot control (enabled=%s); "
                "allowing subagent without concurrency cap.",
                getattr(cache, "enabled", False),
            )
        return None
    return cache.client


@dataclass
class SubagentSlotLease:
    """Per-task execution-context handle managing one subagent's Redis slot lease."""

    thread_id: str
    task_id: str
    slot_member: str
    max_per_thread: int
    max_global: int
    lease_ttl_s: float = LEASE_TTL_S
    renew_interval_s: float = RENEW_INTERVAL_S
    poll_min_s: float = POLL_INTERVAL_MIN_S
    poll_max_s: float = POLL_INTERVAL_MAX_S
    clock: Callable[[], float] = time.time

    status: Literal["queued", "running", "released"] = "queued"
    was_queued: bool = False
    fail_open: bool = False
    registered_in_redis: bool = False
    thread_running: int = 0
    global_running: int = 0
    queue_ahead: int = 0
    acquired_at: float | None = None
    acquired_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _renew_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _released: bool = False

    @property
    def is_queued(self) -> bool:
        return self.status == "queued" and not self._released

    def _keys(self) -> tuple[str, str, str, str, str]:
        return (
            thread_slots_key(self.thread_id),
            global_slots_key(),
            queue_order_key(self.thread_id),
            queue_lease_key(self.thread_id),
            queue_seq_key(self.thread_id),
        )

    def _mark_acquired(self, *, fail_open: bool) -> None:
        self.status = "running"
        self.fail_open = fail_open
        self.queue_ahead = 0
        if self.acquired_at is None:
            self.acquired_at = self.clock()
        self.acquired_event.set()
        if not self._released:
            self._start_renewal()

    def _start_renewal(self) -> None:
        if self._renew_task is not None and not self._renew_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._renew_task = loop.create_task(
            self._renew_loop(),
            name=f"subagent_slot_renew_{self.task_id}",
        )

    async def renew_once(self) -> bool:
        """Execute one renewal tick, re-registering in Redis if the lease was lost
        or if the task initially started in fail-open mode and Redis recovered."""
        if self._released or self.status != "running":
            return False

        if self.fail_open:
            await self.try_acquire_once(mode="reregister")
            if not self.fail_open:
                logger.warning(
                    "Re-registered fail-open subagent slot in Redis after recovery "
                    "(thread=%s, task=%s, thread_running=%d, global_running=%d).",
                    self.thread_id,
                    self.task_id,
                    self.thread_running,
                    self.global_running,
                )
                return True
            return False

        renewed = await self.try_acquire_once(mode="renew")
        if not renewed and not self._released:
            logger.warning(
                "Subagent slot lease was lost in Redis while task still running "
                "(thread=%s, task=%s); re-registering running task in Redis.",
                self.thread_id,
                self.task_id,
            )
            return await self.try_acquire_once(mode="reregister")
        return renewed

    async def _renew_loop(self) -> None:
        try:
            while not self._released and self.status == "running":
                await asyncio.sleep(self.renew_interval_s)
                if self._released or self.status != "running":
                    break
                await self.renew_once()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning(
                "Subagent slot renewal loop error (thread=%s, task=%s): %s",
                self.thread_id,
                self.task_id,
                exc,
            )

    async def try_acquire_once(
        self,
        *,
        mode: SlotAcquireMode = "acquire",
    ) -> bool:
        if self._released:
            return False

        # Warn once at the first acquire; renewals of a fail-open task retry
        # every 30 s and would flood the log while Redis stays down.
        client = _resolve_redis_client(warn_on_unavailable=(mode == "acquire"))
        if client is None:
            self._mark_acquired(fail_open=True)
            return True

        t_key, g_key, qo_key, ql_key, seq_key = self._keys()
        now = self.clock()
        # Mark before sending the command so a mid-flight cancellation or lost
        # reply still triggers an idempotent ZREM on release().
        self.registered_in_redis = True
        try:
            raw = await client.eval(
                ACQUIRE_SLOT_LUA,
                5,
                t_key,
                g_key,
                qo_key,
                ql_key,
                seq_key,
                self.slot_member,
                str(now),
                str(self.lease_ttl_s),
                str(self.max_per_thread),
                str(self.max_global),
                mode,
            )
        except asyncio.CancelledError:
            await self.release()
            raise
        except Exception as exc:
            logger.warning(
                "Redis eval failed during subagent slot %s (thread=%s, task=%s): %s; "
                "allowing subagent without concurrency cap.",
                mode,
                self.thread_id,
                self.task_id,
                exc,
            )
            self._mark_acquired(fail_open=True)
            return True

        acquired = int(raw[0]) if raw and len(raw) >= 1 else 1
        self.thread_running = int(raw[1]) if raw and len(raw) >= 2 else 0
        self.global_running = int(raw[2]) if raw and len(raw) >= 3 else 0
        self.queue_ahead = int(raw[3]) if raw and len(raw) >= 4 else 0

        if acquired == 1:
            self._mark_acquired(fail_open=False)
            return True

        if mode == "acquire":
            self.status = "queued"
            self.was_queued = True
        return False

    async def wait_for_slot(self, task: Any, registry: Any = None) -> None:
        try:
            while self.status == "queued" and not self._released:
                if getattr(task, "cancelled", False) or getattr(task, "completed", False):
                    raise asyncio.CancelledError(
                        "Subagent cancelled while queued for slot"
                    )
                delay = (
                    self.poll_min_s
                    if self.poll_max_s <= self.poll_min_s
                    else random.uniform(self.poll_min_s, self.poll_max_s)
                )
                await asyncio.sleep(delay)
                if (
                    self._released
                    or getattr(task, "cancelled", False)
                    or getattr(task, "completed", False)
                ):
                    raise asyncio.CancelledError(
                        "Subagent cancelled while queued for slot"
                    )
                # Delegate liveness stamping to registry.note_alive(task) without
                # pre-writing task.alive_at, so workflow child heartbeats propagate
                # to the owning workflow task (registry.note_alive skips propagation
                # if task.alive_at was updated < 5s ago).
                if registry is not None and hasattr(registry, "note_alive"):
                    with contextlib.suppress(Exception):
                        await registry.note_alive(task)
                else:
                    task.alive_at = self.clock()
                if await self.try_acquire_once(mode="acquire"):
                    now = self.clock()
                    task.last_updated_at = now
                    if registry is not None and hasattr(registry, "note_alive"):
                        with contextlib.suppress(Exception):
                            await registry.note_alive(task)
                    else:
                        task.alive_at = now
                    break
        except asyncio.CancelledError:
            await self.release()
            raise

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        self.status = "released"
        self.acquired_event.set()

        renew_task = self._renew_task
        self._renew_task = None
        current = asyncio.current_task()
        if (
            renew_task is not None
            and renew_task is not current
            and not renew_task.done()
        ):
            renew_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await renew_task

        if not self.registered_in_redis:
            return

        client = _resolve_redis_client(warn_on_unavailable=False)
        if client is None:
            return

        t_key, g_key, qo_key, ql_key, _ = self._keys()
        try:
            await asyncio.shield(
                client.eval(
                    RELEASE_SLOT_LUA,
                    4,
                    t_key,
                    g_key,
                    qo_key,
                    ql_key,
                    self.slot_member,
                )
            )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(
                "Failed to release subagent slot in Redis (thread=%s, task=%s): %s",
                self.thread_id,
                self.task_id,
                exc,
            )


async def reserve_subagent_slot(
    task: Any,
    thread_id: str,
    *,
    clock: Callable[[], float] = time.time,
    lease_ttl_s: float = LEASE_TTL_S,
    renew_interval_s: float = RENEW_INTERVAL_S,
    poll_min_s: float = POLL_INTERVAL_MIN_S,
    poll_max_s: float = POLL_INTERVAL_MAX_S,
) -> SubagentSlotLease | None:
    """Attempt to acquire a subagent slot or enqueue in FIFO order at spawn time.

    Workflow run drivers (``subagent_type == 'workflow'``) orchestrate child
    dispatches rather than running a subagent graph themselves, so they do not
    consume a subagent slot (their children do).
    """
    if getattr(task, "subagent_type", None) == WORKFLOW_DRIVER_SUBAGENT_TYPE:
        return None

    existing = getattr(task, "slot_lease", None)
    if isinstance(existing, SubagentSlotLease) and not existing._released:
        await existing.release()

    limits = get_subagent_limits()
    norm_thread_id = thread_id or "default"
    slot_member = slot_member_for_task(
        norm_thread_id,
        task.task_id,
        getattr(task, "task_run_id", None),
    )

    lease = SubagentSlotLease(
        thread_id=norm_thread_id,
        task_id=task.task_id,
        slot_member=slot_member,
        max_per_thread=limits.max_running_per_thread,
        max_global=limits.max_running_global,
        lease_ttl_s=lease_ttl_s,
        renew_interval_s=renew_interval_s,
        poll_min_s=poll_min_s,
        poll_max_s=poll_max_s,
        clock=clock,
    )
    task.slot_lease = lease
    try:
        await lease.try_acquire_once(mode="acquire")
    except asyncio.CancelledError:
        await lease.release()
        raise
    return lease


async def query_queued_slot_status(
    thread_id: str,
    task_id: str,
    task_run_id: str | None = None,
    *,
    clock: Callable[[], float] = time.time,
) -> QueuedSlotStatus | None:
    """Read-only Redis lookup of a task's queue state for cross-worker callers.

    Used by ``TaskOutput`` when the current worker holds no local
    ``SubagentSlotLease`` for ``task_id``. Returns ``None`` if the task is not
    queued in Redis or if Redis is unavailable (falling back to running display).
    """
    client = _resolve_redis_client(warn_on_unavailable=False)
    if client is None:
        return None

    norm_thread_id = thread_id or "default"
    exact_member = slot_member_for_task(norm_thread_id, task_id, task_run_id)
    base_member = slot_member_for_task(norm_thread_id, task_id, None)
    now = clock()
    try:
        raw = await client.eval(
            QUERY_QUEUE_LUA,
            4,
            thread_slots_key(norm_thread_id),
            global_slots_key(),
            queue_order_key(norm_thread_id),
            queue_lease_key(norm_thread_id),
            exact_member,
            base_member,
            str(now),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return None

    if not raw or int(raw[0]) != 1:
        return None
    limits = get_subagent_limits()
    return QueuedSlotStatus(
        thread_running=int(raw[1]) if len(raw) >= 2 else 0,
        global_running=int(raw[2]) if len(raw) >= 3 else 0,
        queue_ahead=int(raw[3]) if len(raw) >= 4 else 0,
        max_per_thread=limits.max_running_per_thread,
        max_global=limits.max_running_global,
    )


async def resolve_task_queued_status(
    task: Any,
    thread_id: str = "",
    *,
    task_run_id: str | None = None,
) -> QueuedSlotStatus | None:
    """Resolve queued status from the local lease if present, else from Redis."""
    if getattr(task, "completed", False):
        return None
    lease = getattr(task, "slot_lease", None)
    if isinstance(lease, SubagentSlotLease):
        if not lease.is_queued:
            return None
        return QueuedSlotStatus(
            thread_running=lease.thread_running,
            global_running=lease.global_running,
            queue_ahead=lease.queue_ahead,
            max_per_thread=lease.max_per_thread,
            max_global=lease.max_global,
        )
    resolved_thread_id = thread_id or getattr(task, "thread_id", "") or "default"
    resolved_run_id = task_run_id or getattr(task, "task_run_id", None)
    return await query_queued_slot_status(
        resolved_thread_id,
        task.task_id,
        resolved_run_id,
    )


async def wait_for_subagent_slot(task: Any, registry: Any = None) -> None:
    """Wait until ``task`` holds an active subagent slot before running its graph."""
    lease = getattr(task, "slot_lease", None)
    if not isinstance(lease, SubagentSlotLease):
        return
    await lease.wait_for_slot(task, registry=registry)


async def wait_until_subagent_slot_acquired(task: Any) -> bool:
    """Wait for a queued task to acquire its slot before starting caller timeouts.

    Returns ``True`` when the task was queued and subsequently acquired a slot
    so the caller can reset its ``started_at`` / timeout clock to the moment
    execution actually began.
    """
    lease = getattr(task, "slot_lease", None)
    if not isinstance(lease, SubagentSlotLease) or not lease.was_queued:
        return False
    if not lease.acquired_event.is_set():
        writer = getattr(task, "asyncio_task", None)
        if writer is not None and not writer.done():
            acquire_waiter = asyncio.create_task(lease.acquired_event.wait())
            try:
                await asyncio.wait(
                    [acquire_waiter, writer],
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if not acquire_waiter.done():
                    acquire_waiter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await acquire_waiter
        else:
            await lease.acquired_event.wait()
    return bool(lease.was_queued and lease.acquired_at is not None)


def abort_queued_subagent_on_cancel(task: Any) -> None:
    """If ``task`` is still waiting in the slot queue, cancel its handler task."""
    if is_subagent_queued(task):
        handler_task = getattr(task, "handler_task", None)
        if handler_task is not None and not handler_task.done():
            handler_task.cancel()


async def release_subagent_slot(task: Any) -> None:
    """Idempotently release ``task``'s Redis slot or queue entry."""
    lease = getattr(task, "slot_lease", None)
    if isinstance(lease, SubagentSlotLease):
        await lease.release()


def is_subagent_queued(task: Any) -> bool:
    """Return True if ``task`` is currently waiting in the concurrency queue."""
    if getattr(task, "completed", False):
        return False
    lease = getattr(task, "slot_lease", None)
    return isinstance(lease, SubagentSlotLease) and lease.is_queued


def format_queued_notice_from_status(status: QueuedSlotStatus) -> str:
    """Format the model-facing Chinese queue notice from a ``QueuedSlotStatus``."""
    if (
        status.thread_running < status.max_per_thread
        and status.global_running >= status.max_global
    ):
        cap_detail = (
            f"上限 {status.max_per_thread}，"
            f"全后端正在运行 {status.global_running}/{status.max_global}"
        )
    else:
        cap_detail = f"上限 {status.max_per_thread}"
    return (
        f"已排队：本对话正在运行 {status.thread_running} 个子代理"
        f"（{cap_detail}），前面还有 {status.queue_ahead} 个在排队，"
        f"会自动开始，不要重复派发"
    )


def format_queued_notice(
    task: Any,
    queued_status: QueuedSlotStatus | None = None,
) -> str:
    """Format the model-facing Chinese queue notice for a queued subagent."""
    if queued_status is not None:
        return format_queued_notice_from_status(queued_status)
    lease = getattr(task, "slot_lease", None)
    if isinstance(lease, SubagentSlotLease):
        return format_queued_notice_from_status(
            QueuedSlotStatus(
                thread_running=lease.thread_running,
                global_running=lease.global_running,
                queue_ahead=lease.queue_ahead,
                max_per_thread=lease.max_per_thread,
                max_global=lease.max_global,
            )
        )
    limits = get_subagent_limits()
    return format_queued_notice_from_status(
        QueuedSlotStatus(
            thread_running=limits.max_running_per_thread,
            global_running=limits.max_running_per_thread,
            queue_ahead=0,
            max_per_thread=limits.max_running_per_thread,
            max_global=limits.max_running_global,
        )
    )


def format_task_launch_status(task: Any, *, default: str) -> str:
    """Return the status line for a newly launched or resumed Task tool result."""
    if is_subagent_queued(task):
        return f"Queued (排队中) — {format_queued_notice(task)}"
    return default
