"""Persistent maintenance admission gate coordinated by PostgreSQL advisory locks (§6.4b (1) #2).

Coordinates:
- Host-side atomic pause/resume/allowlist updates to ``<repo>/var/dalton/maintenance-admission.json``
  serialized with ``SELECT pg_advisory_xact_lock(hashtext('dalton_maintenance_admission_gate'))``.
- Backend request admission checks, START / subagent_runs / dalton_data_task_slot transaction
  ``before_commit`` guards, and wiki / sales-notes ingest guards serialized with
  ``SELECT pg_advisory_xact_lock_shared(hashtext('dalton_maintenance_admission_gate'))``.
- Container gate verification with a bounded deadline matching ``wait_backend_health`` so
  rollback/recovery paths treat missing, stopped, or unresponsive backend containers as
  ``unavailable`` and proceed to force-recreate before re-verifying the gate stays closed.
"""

from __future__ import annotations

import argparse
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any, Literal
import urllib.error
import urllib.request

from fastapi import HTTPException

logger = logging.getLogger(__name__)

MAINTENANCE_GATE_LOCK_KEY: str = "dalton_maintenance_admission_gate"
MAINTENANCE_SHARED_LOCK_SQL: str = (
    "SELECT pg_advisory_xact_lock_shared(hashtext('dalton_maintenance_admission_gate'))"
)
MAINTENANCE_EXCLUSIVE_LOCK_SQL: str = (
    "SELECT pg_advisory_xact_lock(hashtext('dalton_maintenance_admission_gate'))"
)
# Matches ``wait_backend_health`` in ``scripts/dalton/ops/prod/deploy_main.sh`` (80 rounds × 3s = 240s).
DEFAULT_CONTAINER_VERIFY_DEADLINE_S: float = 240.0
DEFAULT_CONTAINER_NAME: str = "dalton-langalpha-backend-1"
DEFAULT_CONTAINER_STATE_FILE: str = "/app/var/dalton/maintenance-admission.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_maintenance_state_file(state_file: Path | str | None = None) -> Path:
    """Resolve the shared ``maintenance-admission.json`` path (host & backend container shared volume)."""
    if state_file is not None and str(state_file).strip():
        return Path(state_file).expanduser().resolve()
    env_override = str(os.environ.get("DALTON_MAINTENANCE_ADMISSION_FILE") or "").strip()
    if env_override:
        return Path(env_override).expanduser().resolve()
    return (_repo_root() / "var" / "dalton" / "maintenance-admission.json").resolve()


@dataclass(frozen=True)
class MaintenanceAdmissionState:
    """Parsed snapshot of ``var/dalton/maintenance-admission.json``."""

    version: int = 1
    revision: int = 0
    paused: bool = False
    pause_host_direct: bool = False
    allow_workspace_ids: tuple[str, ...] = ()
    reason: str = ""
    operation: str = ""
    rehearsal_keep_paused_until: str | None = None
    updated_at: str = ""
    exists: bool = False

    def allows_workspace(self, workspace_id: str | None) -> bool:
        if not self.paused:
            return True
        cleaned = str(workspace_id or "").strip()
        if not cleaned:
            return False
        return cleaned in self.allow_workspace_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": int(self.version),
            "revision": int(self.revision),
            "paused": bool(self.paused),
            "pause_host_direct": bool(self.pause_host_direct),
            "allow_workspace_ids": list(self.allow_workspace_ids),
            "reason": str(self.reason or ""),
            "operation": str(self.operation or ""),
            "rehearsal_keep_paused_until": self.rehearsal_keep_paused_until,
            "updated_at": str(self.updated_at or ""),
        }


class MaintenanceAdmissionPausedError(HTTPException):
    """HTTP 503 raised when maintenance mode blocks a request, START commit, or write."""

    def __init__(
        self,
        state: MaintenanceAdmissionState,
        *,
        workspace_id: str | None = None,
        context: str = "",
    ) -> None:
        self.state = state
        self.workspace_id = str(workspace_id or "").strip() or None
        detail: dict[str, Any] = {
            "ok": False,
            "status": "maintenance_in_progress",
            "error": "maintenance_in_progress",
            "error_code": "maintenance_in_progress",
            "message": "System maintenance is in progress; new turns and writes are temporarily paused.",
            "revision": state.revision,
            "reason": state.reason,
            "operation": state.operation,
            "retryable": True,
        }
        if self.workspace_id:
            detail["workspace_id"] = self.workspace_id
        if context:
            detail["context"] = context
        super().__init__(
            status_code=503,
            detail=detail,
            headers={"Retry-After": "15"},
        )


def read_maintenance_state(
    state_file: Path | str | None = None,
) -> MaintenanceAdmissionState:
    """Read ``maintenance-admission.json`` from disk; fail closed if file exists but is unreadable/corrupt."""
    path = resolve_maintenance_state_file(state_file)
    if not path.exists():
        return MaintenanceAdmissionState(exists=False)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("maintenance-admission.json must be a JSON object")
        allow_raw = raw.get("allow_workspace_ids") or []
        allow_ids = tuple(
            str(ws).strip()
            for ws in allow_raw
            if isinstance(ws, str) and str(ws).strip()
        )
        keep_until_raw = raw.get("rehearsal_keep_paused_until")
        keep_until = (
            str(keep_until_raw).strip()
            if isinstance(keep_until_raw, str) and keep_until_raw.strip()
            else None
        )
        return MaintenanceAdmissionState(
            version=int(raw.get("version") or 1),
            revision=int(raw.get("revision") or 0),
            paused=bool(raw.get("paused", False)),
            pause_host_direct=bool(raw.get("pause_host_direct", False)),
            allow_workspace_ids=allow_ids,
            reason=str(raw.get("reason") or ""),
            operation=str(raw.get("operation") or ""),
            rehearsal_keep_paused_until=keep_until,
            updated_at=str(raw.get("updated_at") or ""),
            exists=True,
        )
    except Exception as exc:
        logger.error(
            "[maintenance_gate] Failed reading %s (%s); failing closed to paused",
            path,
            type(exc).__name__,
        )
        return MaintenanceAdmissionState(
            revision=-1,
            paused=True,
            pause_host_direct=True,
            allow_workspace_ids=(),
            reason=f"corrupt_state_file:{type(exc).__name__}",
            exists=True,
        )


def is_admission_paused(
    workspace_id: str | None = None,
    *,
    runs_in: str | None = None,
    state: MaintenanceAdmissionState | None = None,
    state_file: Path | str | None = None,
) -> tuple[bool, MaintenanceAdmissionState]:
    """Return ``(blocked, state)`` for the target workspace (``runs_in`` takes precedence over ``workspace_id``)."""
    st = read_maintenance_state(state_file) if state is None else state
    target_ws = str(runs_in or "").strip() or str(workspace_id or "").strip() or None
    return (not st.allows_workspace(target_ws)), st


def enforce_maintenance_admission(
    workspace_id: str | None = None,
    *,
    runs_in: str | None = None,
    state_file: Path | str | None = None,
    context: str = "",
) -> MaintenanceAdmissionState:
    """Raise ``MaintenanceAdmissionPausedError`` (503) if maintenance blocks this workspace."""
    blocked, st = is_admission_paused(
        workspace_id=workspace_id,
        runs_in=runs_in,
        state_file=state_file,
    )
    if blocked:
        raise MaintenanceAdmissionPausedError(
            st,
            workspace_id=runs_in or workspace_id,
            context=context,
        )
    return st


async def enforce_maintenance_admission_for_thread(
    *,
    workspace_id: str | None = None,
    thread_id: str | None = None,
    runs_in: str | None = None,
    state_file: Path | str | None = None,
    context: str = "",
) -> MaintenanceAdmissionState:
    """Enforce maintenance admission, lazily resolving ``thread_id``'s workspace only when paused with an allowlist."""
    st = read_maintenance_state(state_file)
    if not st.paused:
        return st
    target_ws = str(runs_in or "").strip() or str(workspace_id or "").strip() or None
    if target_ws is None and thread_id and st.allow_workspace_ids:
        try:
            from src.server.database import conversation as conv_db

            thread = await conv_db.get_thread_by_id(str(thread_id))
            if isinstance(thread, Mapping) and thread.get("workspace_id"):
                target_ws = str(thread["workspace_id"]).strip() or None
        except Exception as exc:
            logger.warning(
                "[maintenance_gate] Failed resolving workspace for thread %s: %s",
                thread_id,
                type(exc).__name__,
            )
    if not st.allows_workspace(target_ws):
        raise MaintenanceAdmissionPausedError(st, workspace_id=target_ws, context=context)
    return st


async def enforce_maintenance_admission_for_automation(
    automation_id: str,
    *,
    user_id: str | None = None,
    workspace_id: str | None = None,
    state_file: Path | str | None = None,
    context: str = "automation_trigger",
) -> MaintenanceAdmissionState:
    """Enforce maintenance admission for an automation trigger (resolving workspace only when allowlist is active)."""
    st = read_maintenance_state(state_file)
    if not st.paused:
        return st
    target_ws = str(workspace_id or "").strip() or None
    if target_ws is None and automation_id and user_id and st.allow_workspace_ids:
        try:
            from src.server.database import automation as auto_db

            row = await auto_db.get_automation(str(automation_id), str(user_id))
            if isinstance(row, Mapping) and row.get("workspace_id"):
                target_ws = str(row["workspace_id"]).strip() or None
        except Exception as exc:
            logger.warning(
                "[maintenance_gate] Failed resolving workspace for automation %s: %s",
                automation_id,
                type(exc).__name__,
            )
    if not st.allows_workspace(target_ws):
        raise MaintenanceAdmissionPausedError(st, workspace_id=target_ws, context=context)
    return st


def is_maintenance_in_progress_response(status: int, payload: Any) -> bool:
    """Return True iff an HTTP response represents a 503 ``maintenance_in_progress`` gate rejection."""
    if int(status) != 503:
        return False
    if not isinstance(payload, Mapping):
        return False
    detail = payload.get("detail")
    candidates = [detail, payload] if isinstance(detail, Mapping) else [payload]
    for obj in candidates:
        for key in ("error_code", "status", "error"):
            if str(obj.get(key) or "").strip() == "maintenance_in_progress":
                return True
    return False


async def raise_maintenance_paused_from_response(
    workspace_id: str | None = None,
    *,
    release_riders: Callable[[], Awaitable[Any]] | None = None,
    state_file: Path | str | None = None,
    context: str = "notification_turn",
) -> None:
    """Release any gathered wakeup riders and raise ``MaintenanceAdmissionPausedError`` after a 503 gate response."""
    if release_riders is not None:
        await release_riders()
    st = read_maintenance_state(state_file)
    if not st.paused:
        st = MaintenanceAdmissionState(
            version=st.version,
            revision=st.revision,
            paused=True,
            pause_host_direct=st.pause_host_direct,
            allow_workspace_ids=st.allow_workspace_ids,
            reason=st.reason or "maintenance_in_progress",
            operation=st.operation,
            rehearsal_keep_paused_until=st.rehearsal_keep_paused_until,
            updated_at=st.updated_at,
            exists=st.exists,
        )
    raise MaintenanceAdmissionPausedError(
        st, workspace_id=workspace_id, context=context
    )


async def raise_if_maintenance_http_response(
    resp: Any,
    *,
    workspace_id: str | None = None,
    context: str = "notification_turn",
) -> None:
    """If ``resp`` is a 503 ``maintenance_in_progress`` response, raise ``MaintenanceAdmissionPausedError``."""
    if getattr(resp, "status", None) != 503:
        return
    try:
        payload = await resp.json()
    except Exception:
        return
    if not is_maintenance_in_progress_response(503, payload):
        return
    await raise_maintenance_paused_from_response(
        workspace_id,
        context=context,
    )


def reraise_if_maintenance_paused(exc: BaseException) -> None:
    """Re-raise ``MaintenanceAdmissionPausedError`` so generic HTTP retry loops do not swallow it."""
    if isinstance(exc, MaintenanceAdmissionPausedError):
        raise exc


async def release_riders_if_maintenance_paused(
    exc: BaseException,
    release_riders: Callable[[], Awaitable[Any]] | None = None,
) -> None:
    """Release gathered wakeup riders when a maintenance pause exception unwinds delivery."""
    if isinstance(exc, MaintenanceAdmissionPausedError) and release_riders is not None:
        await release_riders()


async def enforce_maintenance_admission_for_wakeup(
    workspace_id: str | None,
    *,
    release_riders: Callable[[], Awaitable[Any]] | None = None,
    state_file: Path | str | None = None,
) -> MaintenanceAdmissionState:
    """Enforce maintenance admission before posting a wakeup turn, releasing gathered riders if paused."""
    blocked, state = is_admission_paused(workspace_id, state_file=state_file)
    if blocked:
        if release_riders is not None:
            await release_riders()
        raise MaintenanceAdmissionPausedError(
            state, workspace_id=workspace_id, context="wakeup_delivery"
        )
    return state


async def defer_outbox_job_if_paused(
    job: Mapping[str, Any],
    exc: BaseException,
    *,
    delay_seconds: float = 60.0,
) -> bool:
    """Reschedule a claimed outbox job without burning attempts when blocked by maintenance pause."""
    if not isinstance(exc, MaintenanceAdmissionPausedError):
        return False
    from src.server.database.runs import outbox as outbox_db

    job_id = str(job["hook_outbox_id"])
    attempts = int(job["attempts"])
    await outbox_db.reschedule_claimed_job(
        job_id,
        attempts=attempts,
        delay_seconds=delay_seconds,
    )
    logger.info(
        "[maintenance_gate] Deferred outbox job=%s type=%s for %.0fs while maintenance paused (attempts=%d)",
        job_id,
        job.get("hook_type"),
        delay_seconds,
        attempts,
    )
    return True


async def acquire_shared_maintenance_lock(conn: Any) -> None:
    """Acquire the transaction-scoped PostgreSQL shared advisory lock on ``conn``."""
    async with conn.cursor() as cur:
        await cur.execute(MAINTENANCE_SHARED_LOCK_SQL)


async def enforce_maintenance_admission_in_txn(
    conn: Any,
    *,
    workspace_id: str | None = None,
    runs_in: str | None = None,
    state_file: Path | str | None = None,
    context: str = "start_before_commit",
) -> MaintenanceAdmissionState:
    """Acquire shared PG xact advisory lock on ``conn``, then re-read and enforce the gate.

    Must run inside the caller's open ``conn.transaction()`` before COMMIT so host-side
    ``pg_advisory_xact_lock`` waits until any already-open START/claim commits, and any
    transaction arriving after the host writes the paused state rolls back with 503.
    """
    await acquire_shared_maintenance_lock(conn)
    return enforce_maintenance_admission(
        workspace_id=workspace_id,
        runs_in=runs_in,
        state_file=state_file,
        context=context,
    )


async def enforce_maintenance_admission_for_subagent_in_txn(
    conn: Any,
    *,
    thread_id: str | None = None,
    workspace_id: str | None = None,
    state_file: Path | str | None = None,
) -> MaintenanceAdmissionState:
    """Acquire shared PG xact lock on ``conn`` and enforce maintenance gate before ``INSERT INTO subagent_runs``."""
    await acquire_shared_maintenance_lock(conn)
    st = read_maintenance_state(state_file)
    if not st.paused:
        return st
    target_ws = str(workspace_id or "").strip() or None
    if target_ws is None and thread_id and st.allow_workspace_ids:
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT workspace_id FROM conversation_threads WHERE conversation_thread_id = %s",
                    (thread_id,),
                )
                row = await cur.fetchone()
                if row:
                    val = row["workspace_id"] if isinstance(row, Mapping) else row[0]
                    target_ws = str(val or "").strip() or None
        except Exception as exc:
            logger.warning(
                "[maintenance_gate] Failed reading workspace_id for subagent thread %s: %s",
                thread_id,
                type(exc).__name__,
            )
    if not st.allows_workspace(target_ws):
        raise MaintenanceAdmissionPausedError(
            st,
            workspace_id=target_ws,
            context="subagent_run_start",
        )
    return st


async def enforce_maintenance_admission_with_shared_lock(
    *,
    workspace_id: str | None = None,
    state_file: Path | str | None = None,
    context: str = "ingest",
) -> MaintenanceAdmissionState:
    """Enforce maintenance admission under a short PG shared advisory lock transaction (used by ingest tools).

    If the app-data DB pool is reachable, acquires ``pg_advisory_xact_lock_shared`` before
    reading the state file. If no DB pool is active (e.g., isolated unit tests), still enforces
    the on-disk gate deterministically.
    """
    try:
        from src.server.database import pool as db_pool

        has_pool = bool(getattr(db_pool, "_conversation_db_pool_cache", None))
    except Exception:
        db_pool = None
        has_pool = False

    if db_pool is not None and has_pool:
        try:
            async with db_pool.get_db_connection() as conn:
                async with conn.transaction():
                    return await enforce_maintenance_admission_in_txn(
                        conn,
                        workspace_id=workspace_id,
                        state_file=state_file,
                        context=context,
                    )
        except MaintenanceAdmissionPausedError:
            raise
        except Exception as exc:
            logger.warning(
                "[maintenance_gate] DB shared lock unavailable for %s (%s); enforcing on-disk gate",
                context,
                type(exc).__name__,
            )
    return enforce_maintenance_admission(
        workspace_id=workspace_id,
        state_file=state_file,
        context=context,
    )


def write_maintenance_state_atomic(
    state: MaintenanceAdmissionState | Mapping[str, Any],
    *,
    state_file: Path | str | None = None,
) -> MaintenanceAdmissionState:
    """Atomically write ``maintenance-admission.json`` (fsync + os.replace) and verify readback revision."""
    path = resolve_maintenance_state_file(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = state.to_dict() if isinstance(state, MaintenanceAdmissionState) else dict(state)
    expected_rev = int(payload.get("revision") or 0)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".maintenance-admission.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise

    readback = read_maintenance_state(path)
    if not readback.exists or readback.revision != expected_rev:
        raise RuntimeError(
            f"maintenance-admission.json readback revision mismatch: expected {expected_rev}, got {readback.revision}"
        )
    return readback


def _build_next_state(
    current: MaintenanceAdmissionState,
    *,
    paused: bool,
    allow_workspace_ids: Sequence[str] = (),
    pause_host_direct: bool | None = None,
    reason: str = "",
    operation: str = "",
    rehearsal_keep_paused_until: str | None = None,
) -> MaintenanceAdmissionState:
    next_rev = max(0, current.revision) + 1
    cleaned_allow = tuple(
        dict.fromkeys(
            str(ws).strip()
            for ws in allow_workspace_ids
            if isinstance(ws, str) and str(ws).strip()
        )
    )
    eff_host_direct = (
        current.pause_host_direct
        if pause_host_direct is None
        else bool(pause_host_direct)
    )
    if not paused:
        cleaned_allow = ()
        eff_host_direct = False if pause_host_direct is None else bool(pause_host_direct)
        rehearsal_keep_paused_until = None
    return MaintenanceAdmissionState(
        version=1,
        revision=next_rev,
        paused=bool(paused),
        pause_host_direct=eff_host_direct,
        allow_workspace_ids=cleaned_allow,
        reason=str(reason or ""),
        operation=str(operation or ""),
        rehearsal_keep_paused_until=rehearsal_keep_paused_until,
        updated_at=datetime.now(timezone.utc).isoformat(),
        exists=True,
    )


def update_maintenance_state_under_db_lock(
    *,
    paused: bool,
    allow_workspace_ids: Sequence[str] = (),
    pause_host_direct: bool | None = None,
    reason: str = "",
    operation: str = "",
    rehearsal_keep_paused_until: str | None = None,
    state_file: Path | str | None = None,
    dsn: str | None = None,
    conn_factory: Callable[[], Any] | None = None,
) -> MaintenanceAdmissionState:
    """Host-side synchronous gate update serialized with ``pg_advisory_xact_lock``."""
    import psycopg

    from src.server.database.pool import get_db_connection_string

    resolved_path = resolve_maintenance_state_file(state_file)
    conn_cm = (
        conn_factory()
        if conn_factory is not None
        else psycopg.connect(dsn or get_db_connection_string(), autocommit=False)
    )
    with conn_cm as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(MAINTENANCE_EXCLUSIVE_LOCK_SQL)
            current = read_maintenance_state(resolved_path)
            next_state = _build_next_state(
                current,
                paused=paused,
                allow_workspace_ids=allow_workspace_ids,
                pause_host_direct=pause_host_direct,
                reason=reason,
                operation=operation,
                rehearsal_keep_paused_until=rehearsal_keep_paused_until,
            )
            verified = write_maintenance_state_atomic(next_state, state_file=resolved_path)
    return verified


async def aupdate_maintenance_state_under_db_lock(
    *,
    paused: bool,
    allow_workspace_ids: Sequence[str] = (),
    pause_host_direct: bool | None = None,
    reason: str = "",
    operation: str = "",
    rehearsal_keep_paused_until: str | None = None,
    state_file: Path | str | None = None,
    conn: Any = None,
) -> MaintenanceAdmissionState:
    """Async gate update serialized with ``pg_advisory_xact_lock`` (for async tests/callers)."""
    resolved_path = resolve_maintenance_state_file(state_file)

    async def _run(active_conn: Any) -> MaintenanceAdmissionState:
        async with active_conn.transaction():
            async with active_conn.cursor() as cur:
                await cur.execute(MAINTENANCE_EXCLUSIVE_LOCK_SQL)
            current = read_maintenance_state(resolved_path)
            next_state = _build_next_state(
                current,
                paused=paused,
                allow_workspace_ids=allow_workspace_ids,
                pause_host_direct=pause_host_direct,
                reason=reason,
                operation=operation,
                rehearsal_keep_paused_until=rehearsal_keep_paused_until,
            )
            return write_maintenance_state_atomic(next_state, state_file=resolved_path)

    if conn is not None:
        return await _run(conn)
    from src.server.database.pool import get_db_connection

    async with get_db_connection() as pooled_conn:
        return await _run(pooled_conn)


@dataclass(frozen=True)
class ContainerGateVerificationResult:
    """Outcome of verifying the backend container's view of ``maintenance-admission.json`` and `:8000` probe."""

    status: Literal["verified", "unavailable", "mismatch"]
    container_available: bool
    container_revision: int | None
    probe_status_code: int | None
    reason: str
    elapsed_s: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "container_available": self.container_available,
            "container_revision": self.container_revision,
            "probe_status_code": self.probe_status_code,
            "reason": self.reason,
            "elapsed_s": round(self.elapsed_s, 3),
        }


def _default_docker_runner(args: list[str], timeout_s: float) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=max(0.1, timeout_s),
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "docker_timeout"
    except OSError as exc:
        return 127, "", str(exc)


def _default_http_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout_s: float,
) -> tuple[int | None, dict[str, Any] | None, str]:
    req = urllib.request.Request(
        url,
        data=body,
        headers=dict(headers),
        method=method,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=max(0.1, timeout_s)) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except ValueError:
                parsed = None
            return int(resp.status), parsed if isinstance(parsed, dict) else None, ""
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            parsed = None
        return int(exc.code), parsed if isinstance(parsed, dict) else None, ""
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def verify_container_maintenance_gate(
    *,
    expected_revision: int,
    expected_paused: bool = True,
    backend_url: str = "http://127.0.0.1:8000",
    service_token: str | None = None,
    container_name: str = DEFAULT_CONTAINER_NAME,
    container_state_file: str = DEFAULT_CONTAINER_STATE_FILE,
    deadline_s: float = DEFAULT_CONTAINER_VERIFY_DEADLINE_S,
    poll_interval_s: float = 1.0,
    docker_runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
    http_requester: Callable[..., tuple[int | None, dict[str, Any] | None, str]] | None = None,
    clock: Callable[[], float] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> ContainerGateVerificationResult:
    """Verify container-side revision and `:8000` admission probe within a bounded deadline.

    On rollback/recovery paths (§6.4b (1) #2), if ``container_name`` does not exist, is stopped,
    or its `:8000` API fails to respond / times out before ``deadline_s``, returns
    ``status="unavailable"`` (``container_available=False``) so the caller can proceed with
    forced ``backend`` + ``edge`` recreation and re-verify afterwards.
    """
    run_docker = docker_runner or _default_docker_runner
    do_http = http_requester or _default_http_request
    now_fn = clock or time.monotonic
    do_sleep = sleep_fn or time.sleep

    t0 = now_fn()
    deadline = t0 + max(0.0, float(deadline_s))
    base_url = backend_url.rstrip("/")
    tok = (
        str(service_token).strip()
        if service_token is not None
        else str(os.environ.get("INTERNAL_SERVICE_TOKEN") or "").strip()
    )
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if tok:
        headers["X-Service-Token"] = tok

    last_container_rev: int | None = None
    last_probe_code: int | None = None
    last_cat_rc: int | None = None
    last_reason = "container_api_timeout"

    def _deadline_result() -> ContainerGateVerificationResult:
        is_unavail = last_probe_code is None or last_cat_rc != 0
        return ContainerGateVerificationResult(
            status="unavailable" if is_unavail else "mismatch",
            container_available=not is_unavail,
            container_revision=last_container_rev,
            probe_status_code=last_probe_code,
            reason=last_reason,
            elapsed_s=max(0.0, now_fn() - t0),
        )

    while True:
        rem = deadline - now_fn()
        if rem <= 0.0:
            return _deadline_result()
        inspect_timeout = min(5.0, rem)

        rc, out, _err = run_docker(
            ["inspect", "-f", "{{.State.Running}}", container_name],
            inspect_timeout,
        )
        if rc != 0 or out.strip().lower() != "true":
            return ContainerGateVerificationResult(
                status="unavailable",
                container_available=False,
                container_revision=last_container_rev,
                probe_status_code=last_probe_code,
                reason="container_not_running" if rc == 0 else "container_absent",
                elapsed_s=max(0.0, now_fn() - t0),
            )

        # 1. Verify container file revision on the shared bind mount
        rem = deadline - now_fn()
        if rem <= 0.0:
            return _deadline_result()
        cat_timeout = min(5.0, rem)
        c_rc, c_out, _c_err = run_docker(
            ["exec", container_name, "cat", container_state_file],
            cat_timeout,
        )
        last_cat_rc = c_rc
        container_rev: int | None = None
        if c_rc == 0 and c_out.strip():
            try:
                c_data = json.loads(c_out)
                if isinstance(c_data, dict) and "revision" in c_data:
                    container_rev = int(c_data["revision"])
            except (ValueError, TypeError):
                container_rev = None
        last_container_rev = container_rev

        # 2. Verify `:8000` probe endpoint reflects expected pause state and revision
        rem = deadline - now_fn()
        if rem <= 0.0:
            return _deadline_result()
        http_timeout = min(5.0, rem)
        probe_code, probe_body, probe_err = do_http(
            "POST",
            f"{base_url}/api/v1/dalton/maintenance/admission/probe",
            headers=headers,
            body=b"{}",
            timeout_s=http_timeout,
        )
        last_probe_code = probe_code

        if probe_code is not None:
            detail = (
                probe_body.get("detail")
                if isinstance(probe_body, dict) and isinstance(probe_body.get("detail"), dict)
                else probe_body
            )
            probe_rev = (
                int(detail["revision"])
                if isinstance(detail, dict) and detail.get("revision") is not None
                else None
            )
            err_code = (
                str(detail.get("error_code") or detail.get("error") or "")
                if isinstance(detail, dict)
                else ""
            )
            rev_ok = (container_rev == expected_revision) and (
                probe_rev is None or probe_rev == expected_revision
            )
            if expected_paused:
                if probe_code == 503 and err_code == "maintenance_in_progress" and rev_ok:
                    return ContainerGateVerificationResult(
                        status="verified",
                        container_available=True,
                        container_revision=container_rev,
                        probe_status_code=probe_code,
                        reason="paused_verified",
                        elapsed_s=max(0.0, now_fn() - t0),
                    )
                if probe_code == 404:
                    return ContainerGateVerificationResult(
                        status="mismatch",
                        container_available=True,
                        container_revision=container_rev,
                        probe_status_code=probe_code,
                        reason="maintenance_gate_endpoint_missing",
                        elapsed_s=max(0.0, now_fn() - t0),
                    )
                last_reason = (
                    f"probe_unexpected_status:{probe_code}:rev={container_rev}/{probe_rev}"
                )
            else:
                if probe_code == 200 and rev_ok:
                    return ContainerGateVerificationResult(
                        status="verified",
                        container_available=True,
                        container_revision=container_rev,
                        probe_status_code=probe_code,
                        reason="open_verified",
                        elapsed_s=max(0.0, now_fn() - t0),
                    )
                last_reason = (
                    f"probe_expected_open_got:{probe_code}:rev={container_rev}/{probe_rev}"
                )
        else:
            last_reason = f"probe_connection_failed:{probe_err or 'unreachable'}"

        if now_fn() >= deadline:
            # Connection failure / timeout while container is running is treated as unavailable
            is_unavail = probe_code is None or c_rc != 0
            return ContainerGateVerificationResult(
                status="unavailable" if is_unavail else "mismatch",
                container_available=not is_unavail,
                container_revision=last_container_rev,
                probe_status_code=last_probe_code,
                reason=last_reason,
                elapsed_s=max(0.0, now_fn() - t0),
            )
        sleep_s = min(poll_interval_s, max(0.0, deadline - now_fn()))
        if sleep_s > 0:
            do_sleep(sleep_s)


def _cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dalton maintenance admission gate CLI")
    parser.add_argument("--state-file", default=None, help="Override maintenance-admission.json path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Print current maintenance-admission.json state")

    p_set = sub.add_parser("set", help="Update maintenance-admission.json under PG exclusive advisory lock")
    p_set.add_argument("--paused", required=True, choices=("true", "false"))
    p_set.add_argument("--allow-workspace-id", action="append", default=[])
    p_set.add_argument("--pause-host-direct", default=None, choices=("true", "false"))
    p_set.add_argument("--reason", default="")
    p_set.add_argument("--operation", default="")
    p_set.add_argument("--rehearsal-keep-paused-until", default=None)
    p_set.add_argument("--dsn", default=None)

    p_ver = sub.add_parser("verify-container", help="Verify backend container gate revision & :8000 probe")
    p_ver.add_argument("--expected-revision", type=int, required=True)
    p_ver.add_argument("--expected-paused", default="true", choices=("true", "false"))
    p_ver.add_argument("--backend-url", default="http://127.0.0.1:8000")
    p_ver.add_argument("--container-name", default=DEFAULT_CONTAINER_NAME)
    p_ver.add_argument("--deadline-s", type=float, default=DEFAULT_CONTAINER_VERIFY_DEADLINE_S)
    p_ver.add_argument("--allow-unavailable", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "status":
        st = read_maintenance_state(args.state_file)
        print(json.dumps({**st.to_dict(), "exists": st.exists}, ensure_ascii=False))
        return 0
    if args.cmd == "set":
        paused_bool = args.paused == "true"
        host_direct_bool = (
            None if args.pause_host_direct is None else (args.pause_host_direct == "true")
        )
        st = update_maintenance_state_under_db_lock(
            paused=paused_bool,
            allow_workspace_ids=args.allow_workspace_id,
            pause_host_direct=host_direct_bool,
            reason=args.reason,
            operation=args.operation,
            rehearsal_keep_paused_until=args.rehearsal_keep_paused_until,
            state_file=args.state_file,
            dsn=args.dsn,
        )
        print(json.dumps(st.to_dict(), ensure_ascii=False))
        return 0
    if args.cmd == "verify-container":
        res = verify_container_maintenance_gate(
            expected_revision=args.expected_revision,
            expected_paused=(args.expected_paused == "true"),
            backend_url=args.backend_url,
            container_name=args.container_name,
            deadline_s=args.deadline_s,
        )
        print(json.dumps(res.to_dict(), ensure_ascii=False))
        if res.status == "verified":
            return 0
        if res.status == "unavailable" and args.allow_unavailable:
            return 0
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli_main())
