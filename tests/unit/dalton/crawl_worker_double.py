"""Test double for the crawl-service worker.

Speaks the same stdin-JSON / stdout-JSON protocol as
``src.dalton.crawl_service.worker`` but never touches a browser. Behavior is
keyed on the job URL:

  https://ok.test/...           canned HTML page reply
  https://pdf.test/...          canned PDF binary reply
  https://fail.test/...         ``{"ok": false}`` reply (valid protocol reply)
  https://suicide.test/...      SIGKILLs itself with NO reply (crash shape)
  https://garbage.test/...      prints a non-JSON line, exits 0 (crash shape)
  https://slow.test/<secs>      sleeps <secs> (default 0.5), records its pid
                               to ``CRAWL_DOUBLE_PIDS``, then the HTML reply
  https://stray.test/<mode>     spawns a stray grandchild sleeper (detached —
                               ``start_new_session=True``, like Patchright's
                               ``detached: true`` browser launch — when <mode>
                               contains "detached", same-group otherwise),
                               records pids, then replies ok and exits 0:
                               the success-path sweep target
  https://hang.test/<mode>      same stray spawn + pids record, then hangs:
                               the timeout-path sweep target
  https://swarm.test/...        spawns many detached sleepers, records their
                               pids plus the reaper's (``os.getppid()``), then
                               hangs: the sweep-interruption target
  https://orphan.test/...       spawns an intermediate that forks a detached
                               grandchild and exits immediately (the r2
                               fork-and-exit race), then replies ok

With ``CRAWL_DOUBLE_MARK`` set, appends one JSON line per start/end so tests
can prove a worker never spawned (SSRF rejection, queue-deadline refusal) or
observe semaphore ordering across two workers. ``CRAWL_DOUBLE_PIDS`` receives
``{"worker": ..., "stray": ...}`` (swarm adds ``"reaper"`` and ``"strays"``).
"""

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import time

SLEEP_S = 120  # outlives every test budget; only a kill can end it early

HTML_BODY = (
    b"<html><head><title>Service Page</title></head><body>"
    + b"<p>content</p>" * 40
    + b"</body></html>"
)
PDF_BODY = b"%PDF-1.4\n%" + b"x" * 400

# The intermediate for https://orphan.test/: forks a detached grandchild,
# merges its pid into CRAWL_DOUBLE_PIDS, and exits at once — the grandchild
# is orphaned mid-flight exactly like the r2 review's fork-and-exit race.
_ORPHAN_INTERMEDIATE = """
import json, os, subprocess, sys
gc = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep({sleep})"],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
path = os.environ.get("CRAWL_DOUBLE_PIDS")
if path:
    with open(path, "r+") as fh:
        pids = json.load(fh)
        pids["stray"] = gc.pid
        fh.seek(0)
        fh.truncate()
        json.dump(pids, fh)
"""


def _mark(event: str, url: str) -> None:
    path = os.environ.get("CRAWL_DOUBLE_MARK")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"event": event, "url": url, "pid": os.getpid(), "ts": time.time()}
            )
            + "\n"
        )


def _record_pids(stray_pid: int | None = None) -> None:
    path = os.environ.get("CRAWL_DOUBLE_PIDS")
    if not path:
        return
    pids: dict = {"worker": os.getpid()}
    if stray_pid is not None:
        pids["stray"] = stray_pid
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(pids, fh)


def _spawn_stray(detached: bool) -> int:
    # start_new_session=True reproduces the production hazard: Patchright's
    # launcher starts the browser detached (setsid), outside the worker's
    # process group — a plain group kill cannot reach it. DEVNULL stdio so the
    # stray never inherits the worker's stdout pipe write-end (the reaper
    # reads that pipe to EOF for the reply; a stray holding it open would
    # hang the reply — real browsers pipe to the driver, not to the worker).
    proc = subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({SLEEP_S})"],
        start_new_session=detached,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.pid


def _page_reply(body: bytes, content_type: str, title: str = "Service Page") -> dict:
    return {
        "ok": True,
        "page": {
            "status": 200,
            "url": "https://ok.test/final",
            "encoding": "utf-8",
            "headers": {"content-type": content_type},
            "body_b64": base64.b64encode(body).decode("ascii"),
            "title": title,
        },
    }


def main() -> None:
    job = json.loads(sys.stdin.read())
    url = job.get("url", "")
    _mark("start", url)
    try:
        if url.startswith("https://hang.test/"):
            stray = _spawn_stray(detached="detached" in url)
            _record_pids(stray)
            time.sleep(SLEEP_S)
            reply = {"ok": True, "page": {}}  # unreachable within a test budget
        elif url.startswith("https://swarm.test/"):
            strays = [_spawn_stray(detached=True) for _ in range(8)]
            path = os.environ.get("CRAWL_DOUBLE_PIDS")
            if path:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(
                        {
                            "worker": os.getpid(),
                            "reaper": os.getppid(),
                            "strays": strays,
                        },
                        fh,
                    )
            time.sleep(SLEEP_S)
            reply = {"ok": True, "page": {}}
        elif url.startswith("https://stray.test/"):
            stray = _spawn_stray(detached="detached" in url)
            _record_pids(stray)
            reply = _page_reply(HTML_BODY, "text/html; charset=utf-8")
        elif url.startswith("https://orphan.test/"):
            _record_pids()
            intermediate = subprocess.Popen(
                [sys.executable, "-c", _ORPHAN_INTERMEDIATE.replace("{sleep}", str(SLEEP_S))],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Let the intermediate record the grandchild and exit before we
            # reply: the reaper starts sweeping as soon as this worker exits,
            # and would otherwise kill the intermediate before the grandchild
            # pid is written. The grandchild is orphaned onto the reaper here.
            intermediate.wait(timeout=10)
            reply = _page_reply(HTML_BODY, "text/html; charset=utf-8")
        elif url.startswith("https://suicide.test/"):
            _record_pids()
            os.kill(os.getpid(), signal.SIGKILL)  # no reply, no clean exit
            reply = {}  # unreachable
        elif url.startswith("https://garbage.test/"):
            _record_pids()
            sys.stdout.write("definitely not json\n")
            sys.stdout.flush()
            return  # exit 0 with unparseable stdout
        elif url.startswith("https://bigfast.test/"):
            # A detached stray plus an ~8 MiB reply written at once: the reaper
            # holds the whole reply and is mid-write when a teardown lands
            # (codex P6b r3) — it must still reach its sweep.
            _record_pids(_spawn_stray(detached=True))
            reply = _page_reply(b"A" * (6 * 1024 * 1024), "text/html; charset=utf-8")
        elif url.startswith("https://slow.test/"):
            _record_pids()
            parts = url.rsplit("/", 1)
            try:
                time.sleep(float(parts[1]))
            except (IndexError, ValueError):
                time.sleep(0.5)
            reply = _page_reply(HTML_BODY, "text/html; charset=utf-8")
        elif url.startswith("https://pdf.test/"):
            reply = _page_reply(PDF_BODY, "application/pdf", title="")
        elif url.startswith("https://fail.test/"):
            reply = {
                "ok": False,
                "error_type": "BrowserCrash",
                "error": "double-induced failure",
            }
        else:
            reply = _page_reply(HTML_BODY, "text/html; charset=utf-8")
    finally:
        _mark("end", url)
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
