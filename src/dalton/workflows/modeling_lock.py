"""Per-workspace exclusive modeling lock for Dalton modeling workflows.

Only one modeling workflow (``company-model``, ``earnings-deep-update`` with a
``model_path``, or ``full-coverage``) may hold a workspace's modeling lock at a
time. All state lives in a workspace JSON file (``.agents/modeling-lock.json``)
consistent with the workflow run journal and ``.progress.json``, serialized by
a short PostgreSQL transaction advisory lock when a run ledger is active (plus
a per-workspace ``asyncio.Lock`` for same-process concurrency).

Locks carry ``run_id`` and ``expires_at`` so an active holder releases on normal
exit / error / cancellation, and a crashed worker's lock clears automatically
after expiry.
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
DEFAULT_MODELING_LOCK_TTL_SECONDS = 7200.0

# Per-(event-loop, scope-key) in-process mutex so concurrent launches on the
# same worker (and CLI / unit tests without PostgreSQL) serialize cleanly.
_LOCAL_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


class ModelingLockBusy(Exception):
    """Another active modeling run holds this workspace's modeling lock."""


def needs_modeling_lock(workflow: str | None, params: Any = None) -> bool:
    """Return True when ``workflow`` with ``params`` will enter the modeling stage."""
    if workflow not in MODELING_WORKFLOWS:
        return False
    if workflow == "earnings-deep-update" and isinstance(params, dict):
        raw_model_path = params.get("model_path")
        if not isinstance(raw_model_path, str) or not raw_model_path.strip():
            return False
    return True


def modeling_lock_ttl(caps: Any = None) -> float:
    """Derive a lock TTL that outlasts a valid run while bounding crash staleness."""
    run_timeout = getattr(caps, "run_timeout", None)
    if isinstance(run_timeout, (int, float)) and run_timeout > 0:
        return max(float(run_timeout) + 60.0, 300.0)
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
    tool_call_id: str | None,
    now: float,
) -> bool:
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
    holder_call = str(doc.get("tool_call_id") or "").strip()
    if tool_call_id and holder_call and holder_call == tool_call_id:
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
    multi-worker run ledger is active) a PostgreSQL transaction advisory lock."""
    use_db = registry is not None and getattr(registry, "run_ledger", None) is not None
    async with _local_lock(scope_key):
        if not use_db:
            return await fn()
        try:
            from src.server.database.pool import get_db_connection
        except Exception:
            get_db_connection = None  # type: ignore[assignment]
        if get_db_connection is None:
            return await fn()
        lock_key = f"wf-modeling-lock:{scope_key}"
        try:
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
            logger.debug(
                "modeling lock advisory release fell back to direct backend write",
                exc_info=True,
            )
            return await fn()


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
        if _is_active_other_holder(existing, run_id=run_id, tool_call_id=tool_call_id, now=now):
            assert existing is not None
            raise ModelingLockBusy(_busy_message(existing))

        payload = {
            "status": "locked",
            "run_id": run_id,
            "workflow": workflow,
            "thread_id": thread_id,
            "tool_call_id": tool_call_id,
            "acquired_at": now,
            "expires_at": now + max(float(ttl_seconds), 1.0),
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
    """Release the workspace modeling lock if (and only if) it is still held by ``run_id``."""
    scope_key = _workspace_scope_key(backend, workspace_id)

    async def _critical() -> bool:
        try:
            existing = await _read_lock_doc(backend, lock_path)
        except Exception:
            logger.debug("modeling lock read failed during release", exc_info=True)
            return False
        if not isinstance(existing, dict) or existing.get("status") != "locked":
            return False
        if str(existing.get("run_id") or "").strip() != run_id:
            return False
        payload = {
            "status": "released",
            "run_id": run_id,
            "workflow": workflow or existing.get("workflow"),
            "released_at": float(now_fn()),
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
