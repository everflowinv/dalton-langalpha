"""One fetch per supervised process tree, with guaranteed teardown.

The runner never spawns the worker directly: it spawns the reaper
(``src.dalton.crawl_service.reaper``) in its own session, and the reaper — a
Linux child subreaper — spawns the worker, gives it a private ``TMPDIR``, and
sweeps every ppid-descendant of itself after the worker exits, no matter how
far those descendants wandered into other sessions and process groups
(Patchright starts browsers ``detached``/``setsid``'d — the round-1 group kill
never reached them). The reaper exits only after its sweep, so a reply the
runner reads implies a swept task — on the success path too, which is exactly
where a stalled ``session.close()`` used to leave browsers alive with the
semaphore already released.

The failure split (r2 review): a worker that ANSWERED with a valid
``ok=false`` reply raises :class:`WorkerReportedFailure` (the target page's
own failure — mapped to 502 ``worker_failure``); anything without a usable
reply (killed by a signal — an OOM kill included — nonzero exit, unparseable
output, spawn failure) raises :class:`WorkerCrash` (the service side's
failure — mapped to 503 ``worker_crash``).

Runner-side exit paths:
  - budget expiry / caller cancellation / no-reply crash: SIGTERM the reaper
    (it sweeps, SIGTERM-blocked, and exits), then wait out its exit. That
    wait is deliberately cancellation-resistant: the reaper's exit is the
    proof the tree is clean, so a caller disconnect landing mid-teardown
    holds the wait open (bounded) instead of SIGKILLing a sweeping reaper —
    which is precisely how r2's review showed detached browsers leaking.
    Only a reaper that outlives ``_REAP_AFTER_TERM_S`` gets the group SIGKILL
    backstop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

#: Fixed margin the parent adds over the request's ``timeout_ms`` before
#: giving up on the whole supervised fetch: interpreter boots (reaper + worker,
#: a few seconds), scrapling import, plus the shielded session close, which
#: ``fetch_with_session`` bounds at 15s itself. A fetch that cannot finish
#: inside its own timeout plus this margin is by definition runaway.
WORKER_WAIT_MARGIN_S = 25.0

#: How long a SIGTERMed reaper gets to sweep and exit before the group SIGKILL.
_REAP_AFTER_TERM_S = 10.0

#: How long a SIGKILLed process gets to be reaped before tini is left to do it.
_KILL_REAP_S = 10.0


class WorkerTimeout(Exception):
    """The supervised fetch outlived its budget; its tree was killed."""


class WorkerReportedFailure(Exception):
    """The worker answered a valid ``ok=false`` reply — the TARGET page failed."""


class WorkerCrash(Exception):
    """No usable worker reply (spawn failure, signal death, bad output).

    This is the service side's failure (an OOM-killed worker included), not
    the target page's.
    """


def default_worker_cmd() -> list[str]:
    return [sys.executable, "-m", "src.dalton.crawl_service.worker"]


def _supervised_cmd(worker_cmd: Sequence[str]) -> list[str]:
    """The reaper command line wrapping the given worker command."""
    return [sys.executable, "-m", "src.dalton.crawl_service.reaper", "--", *worker_cmd]


def _exit_description(returncode: int | None) -> str:
    """Human-facing exit/signal summary for error payloads (no other data)."""
    if returncode is None:
        return "exit status unknown"
    if returncode < 0:
        return f"killed by signal {-returncode}"
    return f"exit code {returncode}"


async def _kill_group_and_reap(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the reaper's whole process group and reap the direct child.

    ``start_new_session`` made pgid == proc.pid, so one killpg reaches every
    supervised process that stayed in the group even once the leader is gone —
    an orphan keeps its dead leader's pgid until it exits.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # group already empty — nothing to signal
    except OSError as e:
        logger.warning("crawl supervisor killpg(%s) failed: %r", proc.pid, e)
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=_KILL_REAP_S)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass  # tini reaps what we could not; the SIGKILL above is the guarantee


async def _wait_reaper_exit(proc: asyncio.subprocess.Process, timeout_s: float) -> bool:
    """Bounded wait for the reaper's own exit; cancellation does not skip it.

    The reaper SIGTERM-blocks its sweep and exits only when the tree is
    provably clean, so this wait IS the cleanup guarantee. A cancellation
    arriving mid-wait (the caller disconnected during a timeout teardown) is
    consumed and the wait continues for its remaining budget; a repeated
    cancellation (hard loop shutdown) propagates rather than SIGKILL a
    sweeping reaper.
    """
    deadline = time.monotonic() + timeout_s
    wait_task = asyncio.ensure_future(proc.wait())
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            await asyncio.wait_for(asyncio.shield(wait_task), timeout=remaining)
            return True
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            continue


async def _drain(stream: asyncio.StreamReader | None) -> None:
    """Read a pipe to EOF and discard it."""
    if stream is None:
        return
    try:
        while await stream.read(65536):
            pass
    except (OSError, ValueError):
        pass  # pipe torn down under us — nothing left to unblock


async def _terminate_and_reap(proc: asyncio.subprocess.Process) -> None:
    """Ask the reaper to sweep and exit; SIGKILL its group only past the cap.

    The reply pipe keeps being drained while we wait: a teardown that lands
    mid-reply (timeout or disconnect during a large page) would otherwise
    leave the reaper blocked in its stdout write, unable to reach the sweep
    until the cap SIGKILLs it (codex P6b r3).
    """
    drain = asyncio.ensure_future(_drain(proc.stdout))
    try:
        await _terminate_and_reap_inner(proc)
    finally:
        drain.cancel()


async def _terminate_and_reap_inner(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
    exited = await _wait_reaper_exit(proc, _REAP_AFTER_TERM_S)
    if not exited:
        logger.warning(
            "crawl supervisor did not exit %.1fs after SIGTERM; group-killing",
            _REAP_AFTER_TERM_S,
        )
        await _kill_group_and_reap(proc)


def _parse_reply(stdout: bytes, returncode: int | None) -> dict[str, Any]:
    """The worker's reply: the last stdout line that parses as JSON with ``ok``.

    Last-not-first because scrapling or a browser may print to stdout before
    the worker's own line; the worker always writes its reply last.
    """
    for line in reversed(stdout.decode("utf-8", errors="replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            reply = json.loads(line)
        except ValueError:
            continue
        if isinstance(reply, dict) and "ok" in reply:
            return reply
    raise WorkerCrash(
        f"worker produced no reply ({_exit_description(returncode)}; "
        f"stdout_tail={stdout[-200:]!r})"
    )


async def run_worker(
    job: Mapping[str, Any],
    *,
    timeout_s: float,
    worker_cmd: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run one fetch job under the reaper; return its JSON reply.

    Raises :class:`WorkerTimeout` when the tree outlives ``timeout_s`` (killed
    first), :class:`WorkerCrash` when it dies without a usable reply,
    :class:`WorkerReportedFailure` when it answers ``ok=false``. Cancelling
    the await sweeps the tree too, then propagates.
    """
    worker = list(worker_cmd) if worker_cmd else default_worker_cmd()
    try:
        proc = await asyncio.create_subprocess_exec(
            *_supervised_cmd(worker),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # The reaper's own warnings (subreaper degradation, sweep overruns)
            # belong in the container log; the worker's stderr dies with it.
            stderr=None,
            start_new_session=True,
        )
    except OSError as e:
        raise WorkerCrash(f"could not start supervised worker: {e}") from e
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(json.dumps(dict(job)).encode("utf-8")),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        await _terminate_and_reap(proc)
        raise WorkerTimeout(
            f"worker tree killed after {timeout_s:.1f}s budget"
        ) from None
    except asyncio.CancelledError:
        await _terminate_and_reap(proc)
        raise

    try:
        reply = _parse_reply(stdout, proc.returncode)
    except WorkerCrash as e:
        # The full detail (stdout tail included) stays in the service log;
        # the raised message carries only the exit/signal summary.
        logger.warning("crawl worker crash detail: %s", e)
        await _terminate_and_reap(proc)
        raise WorkerCrash(
            f"worker died without a usable reply ({_exit_description(proc.returncode)})"
        ) from e
    if not reply.get("ok"):
        # A failed fetch's teardown may not have completed; make sure the tree
        # is gone rather than trust the close path of a session that errored.
        await _terminate_and_reap(proc)
        err = str(reply.get("error") or "worker reported failure")
        raise WorkerReportedFailure(f"{reply.get('error_type', 'Error')}: {err}")
    if proc.returncode != 0:
        # Reply arrived, but the reaper (or worker) exited nonzero after it —
        # sweep strays the reaper may not have reached before its own death.
        await _kill_group_and_reap(proc)
    return reply
