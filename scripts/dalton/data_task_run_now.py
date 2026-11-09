"""One-shot manual trial runner for disabled OC-1 data tasks (`scripts/dalton/data_task_run_now.py`).

Enqueues a ``trigger_source = 'manual'`` trial run for ``--job-key <key>`` when the task has
``enabled = FALSE`` (for enabled tasks, exits with an error directing the caller to use the
in-chat immediate run) and always waits until that ``run_id`` reaches a terminal status (or
``deadline_utc + 10 minutes``):
- If the scheduler advisory lock is held by the persistent daemon loop, polls the run row
  until it reaches ``TERMINAL_RUN_STATUSES``.
- If the lock is acquired but the run cannot be claimed yet (e.g. container memory admission),
  releases the lock, waits briefly, and retries until the run reaches a terminal status.
- If ``deadline_utc + 10 minutes`` is exceeded before reaching a terminal status, prints the
  current status and exits with a non-zero code, leaving finalization to the persistent loop
  or the next takeover recovery.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from psycopg.rows import dict_row  # noqa: E402

from src.dalton.data_jobs.container_runner import (  # noqa: E402
    DataTaskDockerClient,
    DataTaskPathMap,
    build_path_map,
)
from src.dalton.data_jobs.freeze import (  # noqa: E402
    FREEZE_DIR_NAME,
    format_freeze_slot_utc,
)
from src.dalton.data_jobs.host_runner import DataTaskHostBridgeClient  # noqa: E402
from src.dalton.data_jobs.manifest import sync_manifest_to_db  # noqa: E402
from src.dalton.data_jobs.scheduler import (  # noqa: E402
    DEFAULT_TIMEOUT_SECONDS,
    TERMINAL_RUN_STATUSES,
    check_scheduler_lock,
    claim_next_queued_run,
    enqueue_manual_task_run,
    execute_claimed_container_run,
    execute_claimed_host_run,
    open_dedicated_lock_connection,
    recover_on_takeover,
    release_scheduler_lock,
    try_scheduler_lock,
)
from src.server.database.pool import get_or_create_pool  # noqa: E402

STDOUT_SUMMARY_MAX_CHARS = 1000
WAIT_GRACE_PAST_DEADLINE = timedelta(minutes=10)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _summarize_stdout(log_tail: str | None, max_chars: int = STDOUT_SUMMARY_MAX_CHARS) -> str:
    if not log_tail:
        return ""
    text = str(log_tail).strip()
    if len(text) <= max_chars:
        return text
    return f"...{text[-max_chars:]}"


def _extract_row_field(row: Any, key: str, index: int, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    if isinstance(row, (tuple, list)):
        return row[index] if len(row) > index else default
    return default


def _ensure_utc(dt: Any, fallback: datetime) -> datetime:
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return fallback


async def _fetch_run_state(
    conn: Any,
    run_id: str,
    *,
    paths: DataTaskPathMap,
    fallback_now: datetime,
) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT run_id,
                   job_key,
                   status,
                   exit_code,
                   failure_reason,
                   log_tail,
                   notify_status,
                   notify_preview,
                   scheduled_slot_utc,
                   deadline_utc
            FROM dalton_data_task_runs
            WHERE run_id = %(run_id)s::uuid
            """,
            {"run_id": run_id},
        )
        run_row = await cur.fetchone()

    if run_row is None:
        raise RuntimeError(f"Manual run row {run_id} not found")

    resolved_job_key = str(_extract_row_field(run_row, "job_key", 1, ""))
    status_str = str(_extract_row_field(run_row, "status", 2, "queued"))
    exit_code = _extract_row_field(run_row, "exit_code", 3, None)
    failure_reason = _extract_row_field(run_row, "failure_reason", 4, None)
    log_tail = _extract_row_field(run_row, "log_tail", 5, None)
    notify_status = _extract_row_field(run_row, "notify_status", 6, None)
    notify_preview = _extract_row_field(run_row, "notify_preview", 7, None)
    scheduled_slot_utc = _extract_row_field(run_row, "scheduled_slot_utc", 8, None)
    raw_deadline_utc = _extract_row_field(run_row, "deadline_utc", 9, None)

    if isinstance(notify_preview, str):
        try:
            notify_preview = json.loads(notify_preview)
        except Exception:
            pass

    slot_dt = _ensure_utc(scheduled_slot_utc, fallback_now)
    deadline_dt = _ensure_utc(
        raw_deadline_utc,
        slot_dt + timedelta(seconds=DEFAULT_TIMEOUT_SECONDS),
    )
    wait_deadline_utc = deadline_dt + WAIT_GRACE_PAST_DEADLINE

    slot_str = (
        format_freeze_slot_utc(scheduled_slot_utc)
        if scheduled_slot_utc is not None
        else ""
    )
    freeze_dir = (
        str(paths.backend_data_root / FREEZE_DIR_NAME / resolved_job_key / slot_str)
        if slot_str
        else ""
    )

    return {
        "run_id": run_id,
        "job_key": resolved_job_key,
        "waited": True,
        "status": status_str,
        "terminal": status_str in TERMINAL_RUN_STATUSES,
        "exit_code": exit_code,
        "failure_reason": failure_reason,
        "stdout_summary": _summarize_stdout(log_tail),
        "notify_status": notify_status,
        "notify_preview": notify_preview,
        "freeze_dir": freeze_dir,
        "deadline_utc": deadline_dt,
        "wait_deadline_utc": wait_deadline_utc,
        "timed_out_waiting": False,
    }


async def run_manual_task_now(
    job_key: str,
    *,
    wait: bool = True,
    conn: Any = None,
    lock_conn: Any = None,
    docker_client: DataTaskDockerClient | None = None,
    host_client: DataTaskHostBridgeClient | None = None,
    path_map: DataTaskPathMap | None = None,
    sync_manifest: bool = True,
    poll_interval_seconds: float = 1.0,
    now_fn: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Enqueue a manual data task run and wait until `run_id` reaches a terminal status (or `deadline_utc + 10m`)."""
    _ = wait  # Always waits until terminal status regardless of whether --wait was passed.
    paths = path_map or build_path_map()
    owns_conn = conn is None
    owns_lock_conn = lock_conn is None
    owns_docker = docker_client is None
    opened_pool = None

    work_conn = conn
    sched_lock_conn = lock_conn
    client = docker_client
    bridge_client = host_client

    try:
        if owns_conn:
            pool = get_or_create_pool()
            if pool.closed:
                await pool.open()
                opened_pool = pool
            work_conn = await open_dedicated_lock_connection()

        if sync_manifest:
            async with work_conn.transaction():
                await sync_manifest_to_db(work_conn)

        enqueue_time = now_fn().astimezone(timezone.utc)
        run_id = await enqueue_manual_task_run(
            work_conn,
            job_key,
            now_utc=enqueue_time,
        )

        if owns_lock_conn:
            sched_lock_conn = await open_dedicated_lock_connection()

        if client is None:
            client = DataTaskDockerClient()
        if bridge_client is None:
            bridge_client = DataTaskHostBridgeClient()

        while True:
            acquired = await try_scheduler_lock(sched_lock_conn)
            if acquired:
                try:
                    await check_scheduler_lock(sched_lock_conn)
                    async with work_conn.cursor(row_factory=dict_row) as cur:
                        await cur.execute(
                            "SELECT slot_state FROM dalton_data_task_slot WHERE slot_id = 1"
                        )
                        slot_row = await cur.fetchone()
                    slot_state = (
                        str(_extract_row_field(slot_row, "slot_state", 0, "idle"))
                        if slot_row is not None
                        else "idle"
                    )
                    if slot_state != "idle":
                        await recover_on_takeover(
                            work_conn,
                            lock_conn=sched_lock_conn,
                            docker_client=client,
                            host_client=bridge_client,
                            path_map=paths,
                        )

                    await check_scheduler_lock(sched_lock_conn)
                    claimed = await claim_next_queued_run(
                        work_conn,
                        now_utc=now_fn().astimezone(timezone.utc),
                        only_run_id=run_id,
                    )
                    if claimed is not None:
                        if claimed.runner == "host":
                            await execute_claimed_host_run(
                                work_conn,
                                lock_conn=sched_lock_conn,
                                host_client=bridge_client,
                                claimed=claimed,
                                path_map=paths,
                            )
                        else:
                            await execute_claimed_container_run(
                                work_conn,
                                lock_conn=sched_lock_conn,
                                docker_client=client,
                                claimed=claimed,
                                path_map=paths,
                            )
                finally:
                    await release_scheduler_lock(sched_lock_conn, close_conn=False)

            state = await _fetch_run_state(
                work_conn,
                run_id,
                paths=paths,
                fallback_now=enqueue_time,
            )
            if state["terminal"]:
                return state

            current_now = now_fn().astimezone(timezone.utc)
            if current_now >= state["wait_deadline_utc"]:
                state["timed_out_waiting"] = True
                return state

            await asyncio.sleep(poll_interval_seconds)
    finally:
        if owns_lock_conn and sched_lock_conn is not None and hasattr(sched_lock_conn, "close"):
            try:
                await sched_lock_conn.close()
            except Exception:
                pass
        if owns_docker and client is not None:
            await client.close()
        if owns_conn and work_conn is not None and hasattr(work_conn, "close"):
            try:
                await work_conn.close()
            except Exception:
                pass
        if opened_pool is not None:
            try:
                await opened_pool.close()
            except Exception:
                pass


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enqueue and execute an immediate manual OC-1 data task trial run, waiting until terminal status."
    )
    parser.add_argument(
        "--job-key",
        required=True,
        help="Data task job_key (e.g. megacap-relative-signal-monitor) or task_code (e.g. T18).",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        default=True,
        help="Wait for the run to reach a terminal status (always enabled by default; retained for CLI compatibility).",
    )
    return parser


async def async_main(argv: Sequence[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    try:
        result = await run_manual_task_now(
            args.job_key,
            wait=True,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"run_id: {result['run_id']}")
    print(f"job_key: {result['job_key']}")
    print(f"status: {result['status']}")
    print(f"exit_code: {result['exit_code']}")
    if result.get("failure_reason"):
        print(f"failure_reason: {result['failure_reason']}")
    print(f"notify_status: {result['notify_status']}")
    print(
        f"notify_preview: {json.dumps(result['notify_preview'], ensure_ascii=False)}"
    )
    print(f"freeze_dir: {result['freeze_dir']}")
    print("stdout_summary:")
    print(result["stdout_summary"] or "(empty)")

    if result.get("timed_out_waiting") or not result.get("terminal"):
        print(
            f"ERROR: 等待手动运行 {result['run_id']} 进入终态超时（超过 deadline_utc + 10 分钟），当前状态: {result['status']}",
            file=sys.stderr,
        )
        return 1

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
