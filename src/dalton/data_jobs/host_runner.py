"""Host bridge runner client for OC-1 deterministic data tasks (`T1` / `T29`, §2.3 item 3, §3.2–§3.4).

Calls the host bridge management endpoints (`POST /internal/data-tasks/host-run` and
`POST /internal/data-tasks/host-kill`) authenticated with `DALTON_BRIDGE_ADMIN_TOKEN`
via `get_dalton_bridge_base_url()` and `get_dalton_bridge_admin_token()`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Any, Literal

import httpx

from src.dalton.data_jobs.runner_wrapper import (
    EXIT_CODE_NO_OUTPUT_TIMEOUT,
    EXIT_CODE_TOTAL_TIMEOUT,
)
from src.dalton.settings import (
    get_dalton_bridge_admin_token,
    get_dalton_bridge_base_url,
)

logger = logging.getLogger(__name__)

HOST_RUN_ROUTE = "/internal/data-tasks/host-run"
HOST_KILL_ROUTE = "/internal/data-tasks/host-kill"
DEFAULT_HOST_KILL_HTTP_TIMEOUT_S: float = 20.0
HOST_RUN_HTTP_OVERHEAD_S: float = 30.0

HostMode = Literal["mirror", "run"]
TimeoutKind = Literal["total", "no_output"]


class HostBridgeError(Exception):
    """Transport, authentication, or protocol error from the host bridge data-task routes."""

    def __init__(
        self,
        code: str,
        detail: str = "",
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.payload: dict[str, Any] = payload or {}


@dataclass
class HostRunOutcome:
    """Result returned by `POST /internal/data-tasks/host-run` (§2.3 item 3, §3.4)."""

    ok: bool
    exit_code: int | None = None
    status_code: int = 200
    timed_out: bool = False
    timeout_kind: TimeoutKind | None = None
    killed_by_host_kill: bool = False
    stdout: str = ""
    stderr: str = ""
    log_tail: str = ""
    failure_reason: str | None = None
    bridge_unreachable: bool = False
    result_unknown: bool = False
    # False when the bridge could not confirm the task's process group is gone.
    exit_confirmed: bool = True
    fence_lost: bool = False
    auth_failed: bool = False

    def __post_init__(self) -> None:
        if self.log_tail and not self.stdout:
            self.stdout = self.log_tail
        elif self.stdout and not self.log_tail:
            self.log_tail = (
                f"{self.stdout}\n{self.stderr}" if self.stderr else self.stdout
            )


@dataclass
class HostKillOutcome:
    """Result returned by `POST /internal/data-tasks/host-kill` (§2.3 item 3, §3.3)."""

    ok: bool
    confirmed: bool
    status_code: int = 200
    max_seen_generation: int = 0
    stopped_pgids: list[int] = field(default_factory=list)
    remaining_pgids: list[int] = field(default_factory=list)
    error: str | None = None
    failure_reason: str | None = None
    bridge_unreachable: bool = False
    result_unknown: bool = False
    fence_lost: bool = False
    auth_failed: bool = False


def _format_scheduled_slot_utc(slot: datetime | str) -> str:
    if isinstance(slot, datetime):
        dt = slot if slot.tzinfo is not None else slot.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(slot).strip()


async def _post_bridge_data_tasks(
    route: str,
    body: dict[str, Any],
    *,
    http_timeout_s: float,
) -> dict[str, Any]:
    """Call `POST {bridge}{route}` with `DALTON_BRIDGE_ADMIN_TOKEN` (following `src/dalton/feishu.py:84`)."""
    base_url = get_dalton_bridge_base_url().rstrip("/")
    token = get_dalton_bridge_admin_token()  # DALTON-HOOK(data-jobs): /internal/data-tasks/* are admin routes
    if not token:
        raise HostBridgeError(
            "bridge_unauthorized",
            "DALTON_BRIDGE_ADMIN_TOKEN is not configured",
        )
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Dalton-Bridge-Token": token,
    }
    try:
        async with httpx.AsyncClient(timeout=http_timeout_s, trust_env=False) as client:
            resp = await client.post(f"{base_url}{route}", json=body, headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        logger.warning(
            "Host data-task bridge call %s failed before request sent: %s",
            route,
            type(exc).__name__,
        )
        raise HostBridgeError("bridge_unreachable", type(exc).__name__) from exc
    except httpx.HTTPError as exc:
        logger.warning(
            "Host data-task bridge call %s failed after request sent (result unknown): %s",
            route,
            type(exc).__name__,
        )
        raise HostBridgeError("bridge_result_unknown", type(exc).__name__) from exc

    if resp.status_code == 401:
        raise HostBridgeError("bridge_unauthorized", "bridge rejected the admin token (401)")
    if resp.status_code == 403:
        raise HostBridgeError("bridge_forbidden", "bridge rejected the token as non-admin (403)")
    if not resp.is_success:
        try:
            error_body = resp.json() if resp.content else {}
        except ValueError:
            error_body = {}
        if isinstance(error_body, dict) and str(error_body.get("error") or "").strip():
            raise HostBridgeError(
                str(error_body["error"]),
                str(error_body.get("message") or "")[:200],
                payload=error_body,
            )
        raise HostBridgeError(f"http_{resp.status_code}")

    try:
        data = resp.json() if resp.content else {}
    except ValueError as exc:
        raise HostBridgeError("invalid_json") from exc
    if not isinstance(data, dict):
        raise HostBridgeError("invalid_json")
    return data


async def post_bridge_host_run(
    *,
    generation: int,
    run_id: str | int,
    job_key: str,
    host_mode: HostMode,
    scheduled_slot_utc: datetime | str,
    timeout: int,
    no_output_timeout: int | None = None,
) -> HostRunOutcome:
    """Send `POST /internal/data-tasks/host-run` and wait synchronously for the outcome."""
    body: dict[str, Any] = {
        "generation": int(generation),
        "run_id": run_id if isinstance(run_id, int) else str(run_id),
        "job_key": str(job_key),
        "host_mode": str(host_mode),
        "scheduled_slot_utc": _format_scheduled_slot_utc(scheduled_slot_utc),
        "timeout": int(timeout),
        "no_output_timeout": (int(no_output_timeout) if no_output_timeout is not None else None),
    }
    http_timeout = max(30.0, float(timeout) + HOST_RUN_HTTP_OVERHEAD_S)
    try:
        payload = await _post_bridge_data_tasks(
            HOST_RUN_ROUTE,
            body,
            http_timeout_s=http_timeout,
        )
    except HostBridgeError as exc:
        if exc.code == "bridge_unreachable":
            return HostRunOutcome(
                ok=False,
                status_code=0,
                exit_code=None,
                failure_reason="bridge_unreachable",
                bridge_unreachable=True,
            )
        if exc.code == "bridge_result_unknown":
            return HostRunOutcome(
                ok=False,
                status_code=0,
                exit_code=None,
                failure_reason="bridge_result_unknown",
                result_unknown=True,
            )
        if exc.code == "fence_lost":
            return HostRunOutcome(
                ok=False,
                status_code=409,
                exit_code=None,
                failure_reason="fence_lost",
                fence_lost=True,
            )
        if exc.code in ("bridge_unauthorized", "bridge_forbidden"):
            detail_suffix = f":{exc.detail}" if exc.detail else ""
            return HostRunOutcome(
                ok=False,
                status_code=401 if exc.code == "bridge_unauthorized" else 403,
                exit_code=None,
                failure_reason=f"host_{exc.code}{detail_suffix}",
                auth_failed=True,
            )
        return HostRunOutcome(
            ok=False,
            status_code=500,
            exit_code=None,
            failure_reason=f"host_bridge_error:{exc.code}",
        )

    raw_exit = payload.get("exit_code")
    exit_code = (
        int(raw_exit)
        if isinstance(raw_exit, int) and not isinstance(raw_exit, bool)
        else None
    )
    timed_out = bool(payload.get("timed_out")) or exit_code in (
        EXIT_CODE_TOTAL_TIMEOUT,
        EXIT_CODE_NO_OUTPUT_TIMEOUT,
    )
    raw_kind = payload.get("timeout_kind")
    if raw_kind in ("total", "no_output"):
        timeout_kind: TimeoutKind | None = raw_kind
    elif exit_code == EXIT_CODE_NO_OUTPUT_TIMEOUT:
        timeout_kind = "no_output"
    elif timed_out:
        timeout_kind = "total"
    else:
        timeout_kind = None

    killed_by_host_kill = bool(payload.get("killed_by_host_kill"))
    stdout = str(payload.get("stdout") or payload.get("log_tail") or "")
    stderr = str(payload.get("stderr") or "")
    log_tail = str(payload.get("log_tail") or (f"{stdout}\n{stderr}" if stderr else stdout))
    ok = bool(payload.get("ok")) and exit_code == 0 and not timed_out and not killed_by_host_kill
    failure_reason: str | None = None
    if not ok:
        if timed_out:
            failure_reason = (
                "no_output_timed_out" if timeout_kind == "no_output" else "total_timed_out"
            )
        elif payload.get("error"):
            failure_reason = str(payload["error"])

    return HostRunOutcome(
        ok=ok,
        status_code=200,
        exit_code=exit_code,
        timed_out=timed_out,
        timeout_kind=timeout_kind,
        killed_by_host_kill=killed_by_host_kill,
        stdout=stdout,
        stderr=stderr,
        log_tail=log_tail,
        failure_reason=failure_reason,
        # Strict: a reply without the field is treated as unconfirmed.
        exit_confirmed=payload.get("exit_confirmed") is True,
    )


async def post_bridge_host_kill(
    *,
    generation: int,
    run_id: str | int | None = None,
    reason: str | None = None,
    http_timeout_s: float = DEFAULT_HOST_KILL_HTTP_TIMEOUT_S,
) -> HostKillOutcome:
    """Send `POST /internal/data-tasks/host-kill` with the bumped generation."""
    body: dict[str, Any] = {"generation": int(generation)}
    if run_id is not None:
        body["run_id"] = run_id if isinstance(run_id, int) else str(run_id)
    if reason:
        body["reason"] = str(reason)
    try:
        payload = await _post_bridge_data_tasks(
            HOST_KILL_ROUTE,
            body,
            http_timeout_s=http_timeout_s,
        )
    except HostBridgeError as exc:
        logger.warning(
            "Host bridge kill failed (generation=%s, run_id=%s): %s",
            generation,
            run_id,
            exc,
        )
        err_msg = f"{exc.code}:{exc.detail}" if exc.detail else exc.code
        return HostKillOutcome(
            ok=False,
            confirmed=False,
            status_code=401 if exc.code == "bridge_unauthorized" else (403 if exc.code == "bridge_forbidden" else 0),
            max_seen_generation=generation,
            error=err_msg,
            failure_reason=err_msg,
            bridge_unreachable=(exc.code == "bridge_unreachable"),
            auth_failed=(exc.code in ("bridge_unauthorized", "bridge_forbidden")),
        )

    confirmed = bool(payload.get("confirmed"))
    ok = bool(payload.get("ok")) and confirmed
    raw_max_gen = payload.get("max_seen_generation", generation)
    max_gen = (
        int(raw_max_gen)
        if isinstance(raw_max_gen, int) and not isinstance(raw_max_gen, bool)
        else generation
    )
    stopped_pgids = [
        int(x)
        for x in (payload.get("stopped_pgids") or [])
        if isinstance(x, int) and not isinstance(x, bool)
    ]
    remaining_pgids = [
        int(x)
        for x in (payload.get("remaining_pgids") or [])
        if isinstance(x, int) and not isinstance(x, bool)
    ]
    err = str(payload.get("error") or "") or None
    return HostKillOutcome(
        ok=ok,
        confirmed=confirmed,
        status_code=200,
        max_seen_generation=max_gen,
        stopped_pgids=stopped_pgids,
        remaining_pgids=remaining_pgids,
        error=err,
        failure_reason=err,
    )


class DataTaskHostBridgeClient:
    """High-level client used by the data-task scheduler for `runner = 'host'` tasks (`T1` / `T29`)."""

    async def run_host_task(
        self,
        *,
        generation: int,
        run_id: str | int,
        job_key: str,
        host_mode: HostMode,
        scheduled_slot_utc: datetime | str,
        timeout: int,
        no_output_timeout: int | None = None,
    ) -> HostRunOutcome:
        """Execute a host data task via `POST /internal/data-tasks/host-run` and return a structured outcome."""
        return await post_bridge_host_run(
            generation=generation,
            run_id=run_id,
            job_key=job_key,
            host_mode=host_mode,
            scheduled_slot_utc=scheduled_slot_utc,
            timeout=timeout,
            no_output_timeout=no_output_timeout,
        )

    async def kill_host_tasks(
        self,
        *,
        generation: int,
        run_id: str | int | None = None,
        reason: str | None = None,
    ) -> HostKillOutcome:
        """Stop all host data-task process groups on the bridge via `POST /internal/data-tasks/host-kill`."""
        return await post_bridge_host_kill(
            generation=generation,
            run_id=run_id,
            reason=reason,
        )
