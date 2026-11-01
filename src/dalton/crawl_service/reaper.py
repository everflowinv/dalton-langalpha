"""Task-level subreaper supervisor: one per fetch, between runner and worker.

Why the round-1 process-group kill was not enough: Patchright/Playwright's
launcher starts the browser with ``detached: true`` on Linux, which
``setsid()``s it into a brand-new process group — so ``killpg(worker)`` reaches
the worker and the Node driver but never the browser tree. A hung browser
therefore survived every kill we had (exactly the 10-31 leak class), and tini
only reaps the dead; it does not signal the living.

The fix is ancestry, not groups: after ``prctl(PR_SET_CHILD_SUBREAPER)``, any
descendant of this process that outlives its parent — however far it wandered
into other sessions and groups — is reparented HERE instead of tini. So after
the worker exits (or is killed), sweeping every ppid-descendant of this pid
is total: it reaches the detached browser tree by construction. The worker
also gets a fresh ``TMPDIR`` (removed after the sweep), so a strong-killed
driver's ``/tmp/playwright-*`` litter goes with the fetch.

Protocol: argv is ``-- <worker command...>``; stdin (the job JSON) is
forwarded to the worker, the worker's stdout is forwarded verbatim to our
stdout; our own logs go to stderr.

Three properties the sweep depends on (r2 review):
  - **Uninterruptible once started.** SIGTERM only sets a flag — the handler
    never raises, so it can never cut a scan/kill pass short; the read loops
    poll the flag in short slices, and the sweep itself blocks SIGTERM until
    it finishes. A second TERM (impatient runner, double timeout) is held,
    not honored.
  - **Provable termination.** The loop ends only when ``waitpid(-1)`` reports
    ECHILD (the subreaper has no children left — every live descendant chain
    roots at a direct child, orphans included) AND a fresh ``/proc`` scan
    confirms no descendants, alive or zombie. A descendant that forks-and-
    exits mid-enumeration can no longer hand the sweep a momentarily empty
    tree.
  - **PID-reuse-safe kills.** Each kill goes through ``pidfd_open`` (pinning
    the process's identity), with a ``/proc/<pid>/stat`` re-read confirming
    the pid still names OUR descendant, then ``pidfd_send_signal``. Where the
    kernel lacks pidfds the sweep falls back to bare ``os.kill`` with one
    warning.

Non-Linux (macOS dev / unit tests) has no ``prctl`` and no ``/proc``: the
sweep degrades to killing this process's own group — the reaper included —
which still covers the worker and any same-group strays, and a startup
warning names the gap. The Linux-only behaviors are exercised by the lead in
a container; the unit suite skips them with an explicit platform reason.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_IS_LINUX = sys.platform.startswith("linux")

_PR_SET_CHILD_SUBREAPER = 36

#: How long the sweep keeps killing/reaping before giving up (a wedged kernel
#: side or an unkillable D-state process is beyond us; the runner's SIGKILL
#: backstop and the container's death take over from there).
_SWEEP_BUDGET_S = 5.0

_SLEEP_BETWEEN_PASSES_S = 0.05

#: Slice for the flag-aware read loops; bounds how long a SIGTERM stays
#: unnoticed while blocked on a pipe.
_SELECT_POLL_S = 0.1

# Set ONLY by the SIGTERM handler; read by the read loops and main flow.
_terminate_requested = False

# "pidfd" / "kill", fixed on first kill attempt; the fallback warns once.
_kill_backend: str | None = None


def _on_sigterm(signum, frame) -> None:
    """Record the request. Deliberately nothing else.

    Raising here (the r1 design) could unwind straight out of the sweep's
    ``finally`` and strand exactly the detached descendants this process
    exists to collect; a flag plus short select slices in the read loops
    makes the wait return without any exception at all.
    """
    global _terminate_requested
    _terminate_requested = True


def _enable_subreaper() -> bool:
    """``prctl(PR_SET_CHILD_SUBREAPER, 1)``; False where unavailable."""
    if not _IS_LINUX:
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
            errno_value = ctypes.get_errno()
            logger.warning(
                "crawl reaper prctl(PR_SET_CHILD_SUBREAPER) failed (errno=%d); "
                "descendant sweep degraded to the process-group kill",
                errno_value,
            )
            return False
        return True
    except Exception as e:  # noqa: BLE001 - a missing ctypes/prctl is degradation, not death
        logger.warning("crawl reaper could not enable subreaper (%r)" % e)
        return False


def _read_fd_until_eof_or_term(fd: int) -> bytes:
    """Drain ``fd`` until EOF or a SIGTERM, in ``_SELECT_POLL_S`` slices.

    ``select`` with a short timeout is the wakeup mechanism: a signal
    interrupts it (the handler runs, the flag is set) and even without the
    interrupt the timeout re-checks the flag within one slice.
    """
    os.set_blocking(fd, False)
    chunks: list[bytes] = []
    while not _terminate_requested:
        try:
            readable, _, _ = select.select([fd], [], [], _SELECT_POLL_S)
        except InterruptedError:
            continue
        if not readable:
            continue
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            continue
        if chunk == b"":
            break
        chunks.append(chunk)
    os.set_blocking(fd, True)
    return b"".join(chunks)


def _proc_tree() -> dict[int, tuple[int, str]]:
    """``pid -> (ppid, state)`` off ``/proc/*/stat``; empty off Linux-less hosts.

    ``comm`` may contain spaces and parens, so the stat line is split after
    the LAST ``)`` — the same parse ``browser_memory`` uses.
    """
    tree: dict[int, tuple[int, str]] = {}
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return tree
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="ascii", errors="replace")
            _, right = stat.rsplit(")", 1)
            fields = right.split()
            tree[int(entry.name)] = (int(fields[1]), fields[0])
        except (OSError, ValueError, IndexError):
            continue
    return tree


def _scan_descendants(
    root_pid: int, tree: dict[int, tuple[int, str]]
) -> tuple[set[int], set[int]]:
    """``(alive, zombie)`` ppid-descendants of ``root_pid`` in ``tree``.

    Zombies count as descendants — they are still unreaped exit state rooting
    the loop — but are never signalled; ours land on the subreaper, where
    ``_reap_children`` collects them.
    """
    alive: set[int] = set()
    zombies: set[int] = set()
    seen = {root_pid}
    frontier = [root_pid]
    while frontier:
        next_frontier: list[int] = []
        for parent in frontier:
            for pid, (ppid, state) in tree.items():
                if ppid == parent and pid not in seen:
                    seen.add(pid)
                    if state == "Z":
                        zombies.add(pid)
                    else:
                        alive.add(pid)
                    next_frontier.append(pid)
        frontier = next_frontier
    return alive, zombies


def _freshly_still_descendant(
    pid: int, root_pid: int, tree: dict[int, tuple[int, str]]
) -> bool:
    """Re-read ``/proc/<pid>/stat`` and confirm the pid still names OUR descendant.

    The re-read closes the scan→kill gap: if the process died and the pid was
    reused (or the chain moved out from under us), the fresh ppid chain no
    longer reaches ``root_pid`` and the kill is skipped instead of firing at
    an innocent process.
    """
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(
            encoding="ascii", errors="replace"
        )
        _, right = stat.rsplit(")", 1)
        cur = int(right.split()[1])
    except (OSError, ValueError, IndexError):
        return False
    seen: set[int] = set()
    while cur != root_pid and cur not in seen:
        entry = tree.get(cur)
        if entry is None:
            return False
        seen.add(cur)
        cur = entry[0]
    return cur == root_pid


def _kill_descendant(
    pid: int, root_pid: int, tree: dict[int, tuple[int, str]]
) -> None:
    """SIGKILL one descendant through a pidfd, identity-checked.

    Falls back to bare ``os.kill`` (warning once) only where the kernel has
    no pidfds.
    """
    global _kill_backend
    if _kill_backend != "kill":
        try:
            pidfd = os.pidfd_open(pid)
        except AttributeError:
            _use_kill_backend()
        except OSError as e:
            if e.errno == errno.ESRCH:
                return  # already gone
            _use_kill_backend()  # ENOSYS / EPERM / anything exotic
        else:
            _kill_backend = "pidfd"
            try:
                if _freshly_still_descendant(pid, root_pid, tree):
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                # else: pid reused / chain changed between scan and now — skip
            except OSError as e:
                if e.errno != errno.ESRCH:
                    logger.warning(
                        "crawl reaper pidfd_send_signal(%s) failed: %r", pid, e
                    )
            finally:
                os.close(pidfd)
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _use_kill_backend() -> None:
    """Switch to the bare-``os.kill`` fallback, warning exactly once."""
    global _kill_backend
    if _kill_backend == "kill":
        return
    _kill_backend = "kill"
    logger.warning(
        "crawl reaper: pidfd unavailable; sweep falls back to bare os.kill "
        "(a reused pid could be signalled in the scan→kill gap)"
    )


def _reap_children() -> bool:
    """WNOHANG-reap until nothing more is reapable.

    Returns True on ECHILD — the subreaper has no children left at all.
    """
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        if pid == 0:
            return False


def sweep_descendants(root_pid: int) -> None:
    """SIGKILL the descendant tree of ``root_pid`` until provably empty.

    Done means BOTH ``waitpid(-1)`` hitting ECHILD (no children remain — every
    live descendant chain roots at a direct child, orphans included) AND a
    fresh ``/proc`` scan finding no descendants, alive or zombie. See the
    module docstring for why either check alone is not enough.
    """
    deadline = time.monotonic() + _SWEEP_BUDGET_S
    while True:
        tree = _proc_tree()
        alive, _zombies = _scan_descendants(root_pid, tree)
        for pid in sorted(alive):
            _kill_descendant(pid, root_pid, tree)
        no_children_left = _reap_children()
        if no_children_left:
            fresh = _proc_tree()
            alive_left, zombie_left = _scan_descendants(root_pid, fresh)
            if not alive_left and not zombie_left:
                return
        if time.monotonic() >= deadline:
            fresh = _proc_tree()
            alive_left, zombie_left = _scan_descendants(root_pid, fresh)
            logger.error(
                "crawl reaper sweep incomplete after %.1fs: alive=%s zombies=%s",
                _SWEEP_BUDGET_S,
                sorted(alive_left),
                sorted(zombie_left),
            )
            return
        time.sleep(_SLEEP_BETWEEN_PASSES_S)


def _kill_own_group() -> None:
    """Non-Linux degrade: take the whole group (this process included) down.

    Called as the reaper's LAST act — nothing runs after it. The runner gave
    this process its own session, so the blast radius is exactly the worker
    and whatever stayed in its group; descendants that ``setsid()``'d away
    cannot be reached without ``prctl`` (the warned-about gap).
    """
    try:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as e:  # noqa: BLE001 - nothing left to try; die uncleanly
        logger.warning("crawl reaper group self-kill failed: %r", e)
        os._exit(1)


def _write_reply(reply: bytes) -> None:
    """Write the whole reply to stdout, unless a teardown asks us to stop.

    With ``PYTHONUNBUFFERED`` (the production image) stdout is a raw FileIO,
    so a signal-interrupted write returns short instead of retrying; looping
    on ``os.write`` makes the reply all-or-teardown on every configuration
    rather than silently truncated.
    """
    sys.stdout.flush()
    fd = sys.stdout.fileno()
    view = memoryview(reply)
    while view and not _terminate_requested:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        view = view[written:]


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s %(message)s"
    )
    argv = sys.argv[1:]
    if not argv or argv[0] != "--":
        logger.error("usage: python -m src.dalton.crawl_service.reaper -- <worker cmd>")
        sys.exit(2)
    worker_cmd = argv[1:]
    if not worker_cmd:
        logger.error("crawl reaper: empty worker command")
        sys.exit(2)

    subreaper = _enable_subreaper()
    if not subreaper:
        logger.warning(
            "crawl reaper: no subreaper on this platform (%s); the sweep covers "
            "only this process's group — descendants that left the group survive",
            sys.platform,
        )
    # Installed before anything allocates: the flag-only handler can never
    # raise, so no window exists in which a TERM skips the sweep below.
    signal.signal(signal.SIGTERM, _on_sigterm)

    tmpdir = tempfile.mkdtemp(prefix="crawl-fetch-")
    worker_rc = 143 if _terminate_requested else 0
    try:
        job = _read_fd_until_eof_or_term(sys.stdin.fileno())
        if not _terminate_requested:
            env = dict(os.environ)
            env["TMPDIR"] = tmpdir
            worker = subprocess.Popen(
                worker_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # Browsers and scrapling chatter on stderr; the reply protocol
                # owns stdout and nothing may interleave there.
                stderr=subprocess.DEVNULL,
                env=env,
            )
            try:
                assert worker.stdin is not None and worker.stdout is not None
                worker.stdin.write(job)
                worker.stdin.close()
                reply = _read_fd_until_eof_or_term(worker.stdout.fileno())
                worker.stdout.close()
                if _terminate_requested:
                    worker_rc = 143
                else:
                    _write_reply(reply)
                    worker_rc = worker.wait()
            except OSError as e:  # noqa: BLE001 - dead worker pipe or spawn failure
                logger.warning("crawl reaper worker interaction failed: %r", e)
                worker_rc = 126
    finally:
        # The sweep, once started, must run to completion: SIGTERM is blocked
        # for its whole duration, so a second TERM (impatient runner, double
        # timeout) is held at the mask and dies with the process — it can no
        # longer cut a pass short and strand the detached descendants.
        try:
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        except (AttributeError, OSError, ValueError):
            pass  # exotic platform: degrade to an interruptible sweep, as r1
        try:
            if subreaper:
                sweep_descendants(os.getpid())
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            if not subreaper:
                _kill_own_group()
    if worker_rc < 0:
        # Propagate a signal death faithfully (exit codes are 8-bit; a bare
        # sys.exit(-9) would arrive as 247 and read as a normal exit).
        os.kill(os.getpid(), -worker_rc)
    sys.exit(worker_rc)


if __name__ == "__main__":
    main()
