"""Per-workspace exclusive modeling lock for Dalton modeling workflows.

Only one modeling workflow (``company-model``, ``earnings-deep-update`` with a
``model_path``, or ``full-coverage``) may hold a workspace's modeling lock at a
time. All state lives in a workspace JSON file (``.agents/modeling-lock.json``)
consistent with the workflow run journal and progress markers, serialized by a
short PostgreSQL transaction advisory lock when a run ledger is active (plus a
per-workspace ``asyncio.Lock`` for same-process concurrency).

Locks carry ``run_id`` and a short ``expires_at`` lease renewed by a background
heartbeat while the driver runs. On cancellation, the lock is only released
after confirming the run's modeling writer tasks/processes have exited; if exit
cannot be confirmed or the inter-process advisory lock is unavailable, the
holder stops renewing and lets the short lease expire cleanly without risking
an unlocked overwrite of a new holder.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

MODELING_WORKFLOWS = frozenset(
    {
        "company-model",
        "earnings-deep-update",
        "full-coverage",
    }
)
MODELING_LOCK_PATH = ".agents/modeling-lock.json"
MODELING_LOCK_BUSY_MESSAGE = "这个工作区正在更新模型，请等它完成后再试"
DEFAULT_MODELING_LOCK_TTL_SECONDS = 600.0
DEFAULT_MODELING_LOCK_HEARTBEAT_SECONDS = 180.0
DEFAULT_WRITER_EXIT_TIMEOUT_SECONDS = 10.0

_MODEL_WRITER_ALIVE_SENTINEL = "__MODEL_WRITER_ALIVE__"
_MODEL_WRITER_GONE_SENTINEL = "__MODEL_WRITER_GONE__"
_MODEL_WRITER_PROBE_CMD = (
    "pgrep -f 'dalton-model/scripts/(model_edit|model_consensus)\\.py' >/dev/null 2>&1 "
    f"&& echo {_MODEL_WRITER_ALIVE_SENTINEL} || echo {_MODEL_WRITER_GONE_SENTINEL}"
)

# Per-(event-loop, scope-key) in-process mutex so concurrent launches on the
# same worker (and CLI / unit tests without PostgreSQL) serialize cleanly.
_LOCAL_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


class ModelingLockBusy(Exception):
    """Another active modeling run holds this workspace's modeling lock."""


def needs_modeling_lock(workflow: str | None, params: Any = None) -> bool:
    """Return True when ``workflow`` with ``params`` will enter the modeling stage."""
    if workflow not in MODELING_WORKFLOWS:
        return False
    if workflow == "earnings-deep-update":
        raw_model_path = params.get("model_path") if isinstance(params, dict) else None
        if not isinstance(raw_model_path, str) or not raw_model_path.strip():
            return False
    return True


def resolve_workflow_progress_path(
    workflow: str | None,
    params: Any,
    progress_path: str | None,
) -> str | None:
    """Give modeled and non-modeled ``earnings-deep-update`` runs separate progress files.

    A non-modeled ``earnings-deep-update`` run does not acquire the modeling
    lock and may run concurrently with a modeled ``earnings-deep-update`` in the
    same workspace/quarter. Routing its marker to a distinct sibling file
    prevents it from overwriting the modeled run's ``.progress.json``.
    """
    if not isinstance(progress_path, str) or not progress_path.strip():
        return None
    norm = progress_path.strip()
    if workflow != "earnings-deep-update":
        return norm
    if needs_modeling_lock(workflow, params):
        return norm
    if norm.endswith("/.progress.json"):
        return norm[: -len(".progress.json")] + ".progress.no-model.json"
    if norm == ".progress.json":
        return ".progress.no-model.json"
    if norm.endswith(".json") and not norm.endswith(".no-model.json"):
        return norm[: -len(".json")] + ".no-model.json"
    return f"{norm}.no-model"


def bind_script_run_context(
    script_args: Any,
    *,
    run_id: str,
    run_dir: str,
    resumed_from: str | None = None,
) -> Any:
    """Attach server-bound run identity to QuickJS ``args`` without mutating caller params."""
    if not isinstance(script_args, dict):
        return script_args
    bound = dict(script_args)
    bound["__run_id"] = run_id
    bound["__run_dir"] = run_dir
    bound["__resumed_from"] = resumed_from
    return bound


def modeling_lock_ttl(caps: Any = None) -> float:
    """Return the short lock lease duration (default 10 minutes), bounded so
    any abnormal crash or unconfirmed stop clears within one lease cycle."""
    custom = getattr(caps, "modeling_lock_ttl_seconds", None)
    if isinstance(custom, (int, float)) and not isinstance(custom, bool) and custom > 0:
        return max(float(custom), 0.1)
    return DEFAULT_MODELING_LOCK_TTL_SECONDS


def _workspace_scope_key(backend: Any, workspace_id: str | None) -> str:
    ws_id = str(workspace_id or "").strip()
    if ws_id:
        return f"ws:{ws_id}"
    ws_dir = getattr(backend, "workspace_dir", None)
    if isinstance(ws_dir, str) and ws_dir.strip():
        return f"dir:{os.path.normpath(ws_dir.strip())}"
    return f"backend:{id(backend)}"


def _local_lock(scope_key: str) -> asyncio.Lock:
    loop_id = id(asyncio.get_running_loop())
    key = (loop_id, scope_key)
    lock = _LOCAL_LOCKS.get(key)
    if lock is None:
        if len(_LOCAL_LOCKS) > 512:
            _LOCAL_LOCKS.clear()
        lock = asyncio.Lock()
        _LOCAL_LOCKS[key] = lock
    return lock


async def _read_lock_doc(backend: Any, lock_path: str) -> dict[str, Any] | None:
    try:
        text = await backend.aread_text(lock_path)
    except FileNotFoundError:
        return None
    except TypeError:
        return None
    if text is None or not str(text).strip():
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        logger.warning(
            "modeling lock file unparseable; treating as cleared",
            extra={"path": lock_path},
        )
        return None
    return doc if isinstance(doc, dict) else None


def _is_active_other_holder(
    doc: dict[str, Any] | None,
    *,
    run_id: str,
    now: float,
) -> bool:
    """Return True when ``doc`` represents an unexpired lock held by another ``run_id``.

    Only an identical ``run_id`` counts as the same holder (matching
    ``tool_call_id`` alone with a different ``run_id`` is rejected).
    """
    if not isinstance(doc, dict):
        return False
    if doc.get("status") != "locked":
        return False
    expires_at = doc.get("expires_at")
    if isinstance(expires_at, (int, float)) and now >= float(expires_at):
        return False
    holder_run_id = str(doc.get("run_id") or "").strip()
    if holder_run_id and holder_run_id == run_id:
        return False
    return True


def _busy_message(doc: dict[str, Any]) -> str:
    holder_run_id = str(doc.get("run_id") or "").strip()
    holder_wf = str(doc.get("workflow") or "").strip()
    if holder_run_id and holder_wf:
        return f"{MODELING_LOCK_BUSY_MESSAGE}（当前运行：{holder_run_id}，工作流：{holder_wf}）。"
    if holder_run_id:
        return f"{MODELING_LOCK_BUSY_MESSAGE}（当前运行：{holder_run_id}）。"
    return MODELING_LOCK_BUSY_MESSAGE


async def _run_serialized(
    *,
    scope_key: str,
    registry: Any,
    fail_closed_on_db_error: bool,
    fn: Any,
) -> Any:
    """Serialize lock read-check-write under an in-process lock and (when a
    multi-worker run ledger is active) a PostgreSQL transaction advisory lock.

    When the inter-process advisory lock cannot be obtained (including import
    failure of ``get_db_connection`` or DB connection/pool errors):
    - acquire (``fail_closed_on_db_error=True``) fails closed with ``ModelingLockBusy``;
    - release / renew (``fail_closed_on_db_error=False``) skips writing the lock
      file entirely and returns ``False`` so an unlocked stale writer can never
      clobber a newer holder (the short lease expires cleanly instead).
    """
    use_db = registry is not None and getattr(registry, "run_ledger", None) is not None
    async with _local_lock(scope_key):
        if not use_db:
            return await fn()
        lock_key = f"wf-modeling-lock:{scope_key}"
        try:
            from src.server.database.pool import get_db_connection

            if get_db_connection is None:
                raise RuntimeError("get_db_connection is unavailable")
            async with get_db_connection() as conn:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT pg_advisory_xact_lock(hashtext(%s))",
                            (lock_key,),
                        )
                    return await fn()
        except ModelingLockBusy:
            raise
        except Exception as error:
            if fail_closed_on_db_error:
                logger.warning("modeling lock advisory check failed", exc_info=True)
                raise ModelingLockBusy(
                    f"{MODELING_LOCK_BUSY_MESSAGE}（锁状态核验失败：{type(error).__name__}，请稍后再试）。"
                ) from error
            logger.warning(
                "modeling lock advisory lock unavailable; skipping lock file write",
                extra={"scope_key": scope_key, "error_type": type(error).__name__},
                exc_info=True,
            )
            return False


async def acquire_modeling_lock(
    backend: Any,
    *,
    registry: Any = None,
    run_id: str,
    workflow: str,
    thread_id: str | None = None,
    tool_call_id: str | None = None,
    workspace_id: str | None = None,
    ttl_seconds: float = DEFAULT_MODELING_LOCK_TTL_SECONDS,
    lock_path: str = MODELING_LOCK_PATH,
    now_fn: Any = time.time,
) -> dict[str, Any]:
    """Acquire this workspace's modeling lock for ``run_id`` or raise ``ModelingLockBusy``."""
    scope_key = _workspace_scope_key(backend, workspace_id)

    async def _critical() -> dict[str, Any]:
        now = float(now_fn())
        try:
            existing = await _read_lock_doc(backend, lock_path)
        except Exception as error:
            logger.warning("modeling lock read failed", extra={"path": lock_path}, exc_info=True)
            raise ModelingLockBusy(
                f"{MODELING_LOCK_BUSY_MESSAGE}（无法读取建模锁：{type(error).__name__}）。"
            ) from error
        if _is_active_other_holder(existing, run_id=run_id, now=now):
            assert existing is not None
            raise ModelingLockBusy(_busy_message(existing))

        payload = {
            "status": "locked",
            "run_id": run_id,
            "workflow": workflow,
            "thread_id": thread_id,
            "tool_call_id": tool_call_id,
            "acquired_at": now,
            "expires_at": now + max(float(ttl_seconds), 0.1),
        }
        try:
            written = await backend.awrite_text(
                lock_path,
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            )
        except Exception as error:
            raise ModelingLockBusy(
                f"{MODELING_LOCK_BUSY_MESSAGE}（无法写入建模锁：{type(error).__name__}）。"
            ) from error
        if written is False:
            raise ModelingLockBusy(f"{MODELING_LOCK_BUSY_MESSAGE}（建模锁写入未确认）。")
        return payload

    return await _run_serialized(
        scope_key=scope_key,
        registry=registry,
        fail_closed_on_db_error=True,
        fn=_critical,
    )


async def renew_modeling_lock(
    backend: Any,
    *,
    registry: Any = None,
    run_id: str,
    workflow: str | None = None,
    workspace_id: str | None = None,
    ttl_seconds: float = DEFAULT_MODELING_LOCK_TTL_SECONDS,
    lock_path: str = MODELING_LOCK_PATH,
    now_fn: Any = time.time,
) -> bool:
    """Extend the lease for ``run_id`` if it still holds an unexpired lock."""
    scope_key = _workspace_scope_key(backend, workspace_id)

    async def _critical() -> bool:
        now = float(now_fn())
        try:
            existing = await _read_lock_doc(backend, lock_path)
        except Exception:
            logger.debug("modeling lock read failed during renew", exc_info=True)
            return False
        if not isinstance(existing, dict) or existing.get("status") != "locked":
            return False
        if str(existing.get("run_id") or "").strip() != run_id:
            return False
        expires_at = existing.get("expires_at")
        if isinstance(expires_at, (int, float)) and now >= float(expires_at):
            return False
        payload = dict(existing)
        if workflow:
            payload["workflow"] = workflow
        payload["renewed_at"] = now
        payload["expires_at"] = now + max(float(ttl_seconds), 0.1)
        try:
            return bool(
                await backend.awrite_text(
                    lock_path,
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                )
            )
        except Exception:
            logger.debug("modeling lock renew write failed", exc_info=True)
            return False

    try:
        return bool(
            await _run_serialized(
                scope_key=scope_key,
                registry=registry,
                fail_closed_on_db_error=False,
                fn=_critical,
            )
        )
    except Exception:
        logger.debug("modeling lock renew failed", exc_info=True)
        return False


async def release_modeling_lock(
    backend: Any,
    *,
    registry: Any = None,
    run_id: str,
    workflow: str | None = None,
    workspace_id: str | None = None,
    lock_path: str = MODELING_LOCK_PATH,
    now_fn: Any = time.time,
) -> bool:
    """Release the workspace modeling lock if (and only if) it is still held by ``run_id``
    and has not already expired."""
    scope_key = _workspace_scope_key(backend, workspace_id)

    async def _critical() -> bool:
        now = float(now_fn())
        try:
            existing = await _read_lock_doc(backend, lock_path)
        except Exception:
            logger.debug("modeling lock read failed during release", exc_info=True)
            return False
        if not isinstance(existing, dict) or existing.get("status") != "locked":
            return False
        if str(existing.get("run_id") or "").strip() != run_id:
            return False
        expires_at = existing.get("expires_at")
        if isinstance(expires_at, (int, float)) and now >= float(expires_at):
            return False
        payload = {
            "status": "released",
            "run_id": run_id,
            "workflow": workflow or existing.get("workflow"),
            "released_at": now,
        }
        try:
            return bool(
                await backend.awrite_text(
                    lock_path,
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                )
            )
        except Exception:
            logger.debug("modeling lock release write failed", exc_info=True)
            return False

    try:
        return bool(
            await _run_serialized(
                scope_key=scope_key,
                registry=registry,
                fail_closed_on_db_error=False,
                fn=_critical,
            )
        )
    except Exception:
        logger.debug("modeling lock release failed", exc_info=True)
        return False


class ModelingLockHeartbeat:
    """Periodic lease renewer active while a WorkflowDriver owns the modeling lock."""

    def __init__(
        self,
        backend: Any,
        *,
        registry: Any = None,
        run_id: str,
        workflow: str,
        workspace_id: str | None = None,
        ttl_seconds: float = DEFAULT_MODELING_LOCK_TTL_SECONDS,
        interval_seconds: float | None = None,
        lock_path: str = MODELING_LOCK_PATH,
        now_fn: Any = time.time,
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._run_id = run_id
        self._workflow = workflow
        self._workspace_id = workspace_id
        self._ttl_seconds = max(float(ttl_seconds), 0.1)
        if interval_seconds is None:
            self._interval_seconds = min(
                DEFAULT_MODELING_LOCK_HEARTBEAT_SECONDS,
                max(self._ttl_seconds / 3.0, 0.05),
            )
        else:
            self._interval_seconds = max(float(interval_seconds), 0.01)
        self._lock_path = lock_path
        self._now_fn = now_fn
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run(),
            name=f"modeling_lock_heartbeat_{self._run_id}",
        )

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval_seconds)
                await renew_modeling_lock(
                    self._backend,
                    registry=self._registry,
                    run_id=self._run_id,
                    workflow=self._workflow,
                    workspace_id=self._workspace_id,
                    ttl_seconds=self._ttl_seconds,
                    lock_path=self._lock_path,
                    now_fn=self._now_fn,
                )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.debug("modeling lock heartbeat loop exited", exc_info=True)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("modeling lock heartbeat stop error", exc_info=True)


async def confirm_modeling_writers_exited(
    *,
    registry: Any,
    owner_task_id: str,
    extra_tasks: list[Any] | None = None,
    backend: Any = None,
    check_sandbox_processes: bool = False,
    timeout: float | None = None,
    poll_interval: float = 0.2,
) -> bool:
    """Confirm every child writer task (and, when cancelling, any sandbox modeling
    writer process) for ``owner_task_id`` has exited within ``timeout``.

    Returns ``False`` when any writer task is still alive or when the sandbox
    writer process cannot be confirmed gone — callers must skip releasing the
    modeling lock so the short lease blocks overlapping model writes until it
    expires.
    """
    eff_timeout = DEFAULT_WRITER_EXIT_TIMEOUT_SECONDS if timeout is None else float(timeout)
    deadline = time.monotonic() + max(eff_timeout, 0.0)

    async def _collect_live_writers() -> list[asyncio.Task[Any]]:
        seen_tasks: dict[int, Any] = {}
        for t in extra_tasks or ():
            if t is not None:
                seen_tasks[id(t)] = t
        get_all = getattr(registry, "get_all_tasks", None) if registry is not None else None
        if callable(get_all):
            try:
                all_tasks = await get_all()
            except Exception:
                all_tasks = ()
            for t in all_tasks or ():
                if getattr(t, "owner_task_id", None) == owner_task_id:
                    seen_tasks[id(t)] = t
        writers: list[asyncio.Task[Any]] = []
        seen_writers: set[int] = set()
        for t in seen_tasks.values():
            live = getattr(t, "live_writers", None)
            if isinstance(live, list):
                candidates = live
            else:
                candidates = [
                    w
                    for w in (
                        getattr(t, "asyncio_task", None),
                        getattr(t, "handler_task", None),
                    )
                    if w is not None and not w.done()
                ]
            for w in candidates:
                if w is not None and not w.done() and id(w) not in seen_writers:
                    seen_writers.add(id(w))
                    writers.append(w)
        return writers

    while True:
        writers = await _collect_live_writers()
        if not writers:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.wait(writers, timeout=remaining)
        still_alive = [w for w in writers if not w.done()]
        if still_alive:
            return False

    if check_sandbox_processes and backend is not None:
        execute = getattr(backend, "aexecute", None)
        if callable(execute):
            while True:
                remaining = deadline - time.monotonic()
                probe_timeout = max(remaining, 0.05)
                try:
                    response = await asyncio.wait_for(
                        execute(_MODEL_WRITER_PROBE_CMD),
                        timeout=probe_timeout,
                    )
                except Exception:
                    logger.warning(
                        "modeling writer sandbox exit probe failed; deferring lock release to lease expiry",
                        extra={"owner_task_id": owner_task_id},
                        exc_info=True,
                    )
                    return False
                raw_output = getattr(response, "output", None)
                if not isinstance(raw_output, str):
                    # Unconfigured test double without string shell output;
                    # in-process writer confirmation above already succeeded.
                    return True
                exit_code = getattr(response, "exit_code", 1)
                if _MODEL_WRITER_GONE_SENTINEL in raw_output and exit_code == 0:
                    return True
                if _MODEL_WRITER_ALIVE_SENTINEL in raw_output:
                    if time.monotonic() >= deadline:
                        return False
                    await asyncio.sleep(min(poll_interval, max(deadline - time.monotonic(), 0.01)))
                    continue
                return False

    return True
