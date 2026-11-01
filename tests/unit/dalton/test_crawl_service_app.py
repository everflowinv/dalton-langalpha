"""Crawl service HTTP surface (P6b): token auth, SSRF re-check before any
spawn, the serialized-page response shape, worker-failure mapping, the
concurrency gate, budget-aware queueing, caller-disconnect cancellation, and
process-tree teardown on both the timeout and the success path.

The worker subprocess is the double in ``crawl_worker_double.py`` (same
stdin/stdout JSON protocol, no browser). Its strays reproduce the production
hazard shapes: same-group helpers (plain children) and detached ones
(``start_new_session=True``, like Patchright's browser launch). The detached
descendants can only be swept by the Linux subreaper — those two tests skip
off Linux with the platform reason and are exercised in a container by the
lead; the same-group paths run everywhere.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import sys
import time
from pathlib import Path

import httpx
import pytest

from src.dalton.crawl_service.app import create_app
from src.dalton.crawl_service.runner import run_worker

WORKER_DOUBLE = [sys.executable, str(Path(__file__).parent / "crawl_worker_double.py")]
TOKEN = "unit-test-service-token"
_IS_LINUX = sys.platform.startswith("linux")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", TOKEN)
    monkeypatch.setenv("CRAWL_DOUBLE_MARK", str(tmp_path / "marks.jsonl"))
    return create_app(worker_cmd=WORKER_DOUBLE, browser_concurrency=1, wait_margin_s=2.0)


async def _post(app, url, *, token=TOKEN, tier="browser", timeout_ms=3000):
    headers = {"X-Service-Token": token} if token is not None else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://crawl.test") as client:
        return await client.post(
            "/fetch",
            json={"url": url, "tier": tier, "timeout_ms": timeout_ms},
            headers=headers,
        )


async def _wait_pid_gone(pid: int, deadline_s: float = 5.0) -> bool:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.05)
    return False


class TestHealthAndAuth:
    @pytest.mark.asyncio
    async def test_health_open_without_token(self, app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://crawl.test") as client:
            resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_fetch_without_token_header_401(self, app):
        resp = await _post(app, "https://ok.test/x", token=None)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_fetch_with_wrong_token_401(self, app):
        resp = await _post(app, "https://ok.test/x", token="not-the-token")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_unconfigured_token_fails_closed(self, monkeypatch):
        # INTERNAL_SERVICE_TOKEN unset on the service side: nothing may match.
        monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
        app = create_app(worker_cmd=WORKER_DOUBLE, browser_concurrency=1, wait_margin_s=2.0)
        resp = await _post(app, "https://ok.test/x", token="anything")
        assert resp.status_code == 401


class TestSsrfRecheck:
    @pytest.mark.asyncio
    async def test_loopback_rejected_before_worker_spawn(self, app, tmp_path):
        resp = await _post(app, "http://127.0.0.1:8000/admin")
        assert resp.status_code == 400
        assert "not allowed" in resp.json()["detail"]
        # The worker double would have left a start mark; none may exist.
        assert not (tmp_path / "marks.jsonl").exists()

    @pytest.mark.asyncio
    async def test_private_address_rejected(self, app):
        resp = await _post(app, "http://10.0.0.5/x")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_non_http_scheme_rejected(self, app):
        resp = await _post(app, "file:///etc/passwd")
        assert resp.status_code == 400


class TestFetchOk:
    @pytest.mark.asyncio
    async def test_response_carries_exactly_the_page_fields(self, app):
        resp = await _post(app, "https://ok.test/x")
        assert resp.status_code == 200
        payload = resp.json()
        assert set(payload) == {"status", "url", "encoding", "headers", "body_b64", "title"}
        assert base64.b64decode(payload["body_b64"]).startswith(b"<html>")
        assert payload["title"] == "Service Page"
        assert payload["status"] == 200
        assert payload["headers"]["content-type"] == "text/html; charset=utf-8"

    @pytest.mark.asyncio
    async def test_worker_failure_maps_to_502(self, app):
        resp = await _post(app, "https://fail.test/x")
        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert detail["error_type"] == "worker_failure"
        assert "double-induced failure" in detail["error"]


class TestProcessTreeTeardown:
    """The reaper must sweep the worker's strays on BOTH exit paths.

    Same-group strays die with the group kill everywhere; detached strays
    (the Patchright browser shape) need the Linux subreaper.
    """

    def _make_app(self, monkeypatch, tmp_path):
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", TOKEN)
        monkeypatch.setenv("CRAWL_DOUBLE_MARK", str(tmp_path / "marks.jsonl"))
        monkeypatch.setenv("CRAWL_DOUBLE_PIDS", str(tmp_path / "pids.json"))
        return create_app(worker_cmd=WORKER_DOUBLE, browser_concurrency=1, wait_margin_s=0.8)

    async def _stray_gone(self, tmp_path) -> tuple[bool, bool]:
        pids_path = tmp_path / "pids.json"
        deadline = time.monotonic() + 5.0
        pids: dict = {}
        # The orphan double writes "worker" first and an intermediate merges
        # "stray" a moment later, so wait for both keys, not just the file.
        while time.monotonic() < deadline:
            if pids_path.exists():
                try:
                    pids = json.loads(pids_path.read_text())
                except ValueError:
                    pids = {}  # caught mid-rewrite
                if "worker" in pids and "stray" in pids:
                    break
            await asyncio.sleep(0.05)
        assert "worker" in pids and "stray" in pids, f"double never recorded both pids: {pids}"
        return (
            await _wait_pid_gone(pids["worker"]),
            await _wait_pid_gone(pids["stray"]),
        )

    @pytest.mark.asyncio
    async def test_timeout_kills_worker_and_same_group_stray(self, monkeypatch, tmp_path):
        app = self._make_app(monkeypatch, tmp_path)
        resp = await _post(app, "https://hang.test/ingroup", timeout_ms=1000)
        assert resp.status_code == 504
        worker_gone, stray_gone = await self._stray_gone(tmp_path)
        assert worker_gone, "worker survived its budget kill"
        assert stray_gone, "same-group stray survived the group kill"

    @pytest.mark.skipif(
        not _IS_LINUX,
        reason=(
            "detached (setsid) descendants are only swept by the Linux subreaper "
            "(prctl + /proc); macOS degrades to the group kill — the same-group "
            "path is covered by test_timeout_kills_worker_and_same_group_stray, "
            "and the subreaper path is verified by the lead in a Linux container"
        ),
    )
    @pytest.mark.asyncio
    async def test_timeout_kills_worker_and_detached_stray(self, monkeypatch, tmp_path):
        app = self._make_app(monkeypatch, tmp_path)
        resp = await _post(app, "https://hang.test/detached", timeout_ms=1000)
        assert resp.status_code == 504
        worker_gone, stray_gone = await self._stray_gone(tmp_path)
        assert worker_gone, "worker survived its budget kill"
        assert stray_gone, "detached (setsid) stray survived the subreaper sweep"

    @pytest.mark.asyncio
    async def test_success_path_sweeps_same_group_stray(self, monkeypatch, tmp_path):
        # agy r1 #2: a worker that returns ok=true, exit=0 can still leave a
        # browser behind (a session.close() stalled past its 15s deadline);
        # the sweep must run on the success path too.
        app = self._make_app(monkeypatch, tmp_path)
        resp = await _post(app, "https://stray.test/ingroup", timeout_ms=5000)
        assert resp.status_code == 200
        worker_gone, stray_gone = await self._stray_gone(tmp_path)
        assert worker_gone, "worker process still alive after a successful fetch"
        assert stray_gone, "same-group stray survived the success-path sweep"

    @pytest.mark.skipif(
        not _IS_LINUX,
        reason=(
            "detached (setsid) descendants are only swept by the Linux subreaper "
            "(prctl + /proc); macOS degrades to the group kill — the same-group "
            "path is covered by test_success_path_sweeps_same_group_stray, and "
            "the subreaper path is verified by the lead in a Linux container"
        ),
    )
    @pytest.mark.asyncio
    async def test_success_path_sweeps_detached_stray(self, monkeypatch, tmp_path):
        app = self._make_app(monkeypatch, tmp_path)
        resp = await _post(app, "https://stray.test/detached", timeout_ms=5000)
        assert resp.status_code == 200
        worker_gone, stray_gone = await self._stray_gone(tmp_path)
        assert worker_gone, "worker process still alive after a successful fetch"
        assert stray_gone, "detached (setsid) stray survived the subreaper sweep"

    @pytest.mark.skipif(
        not _IS_LINUX,
        reason=(
            "the fork-and-exit race (r2 #2) resolves through subreaper adoption "
            "and the ECHILD termination condition — both Linux-only (prctl + "
            "/proc); verified by the lead in a Linux container"
        ),
    )
    @pytest.mark.asyncio
    async def test_success_path_sweeps_fork_exit_orphan(self, monkeypatch, tmp_path):
        """A descendant that forks a grandchild and exits instantly (the r2
        race that could hand the sweep a momentarily empty tree) — the
        orphaned grandchild, adopted by the subreaper, must still be swept."""
        app = self._make_app(monkeypatch, tmp_path)
        resp = await _post(app, "https://orphan.test/", timeout_ms=5000)
        assert resp.status_code == 200
        worker_gone, stray_gone = await self._stray_gone(tmp_path)
        assert worker_gone, "worker process still alive after a successful fetch"
        assert stray_gone, "fork-and-exit orphan survived the sweep"


class TestConcurrencyGate:
    @pytest.mark.asyncio
    async def test_requests_queue_behind_the_gate(self, app, tmp_path):
        started = time.monotonic()
        resp_a, resp_b = await asyncio.wait_for(
            asyncio.gather(
                _post(app, "https://slow.test/0.5"),
                _post(app, "https://slow.test/0.5"),
            ),
            timeout=30,
        )
        elapsed = time.monotonic() - started
        assert resp_a.status_code == 200
        assert resp_b.status_code == 200
        # Two 0.5s fetches through a single slot must serialize end to end.
        assert elapsed >= 0.8

        marks = [
            json.loads(line)
            for line in (tmp_path / "marks.jsonl").read_text().splitlines()
        ]
        starts = sorted(m["ts"] for m in marks if m["event"] == "start")
        ends = sorted(m["ts"] for m in marks if m["event"] == "end")
        assert len(starts) == 2 and len(ends) == 2
        # No overlap: the first fetch fully ended before the second began
        # (small epsilon for cross-process clock write ordering).
        assert starts[1] >= ends[0] - 0.05


class TestQueueDeadline:
    @pytest.mark.asyncio
    async def test_queued_request_returns_504_at_its_own_budget(
        self, monkeypatch, tmp_path
    ):
        """Queueing counts against the request's own budget, with a real timer
        (r2 #3): a queued request must return 504 when ITS budget expires, not
        camp on the semaphore until the running fetch releases it."""
        marks_path = tmp_path / "marks.jsonl"
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", TOKEN)
        monkeypatch.setenv("CRAWL_DOUBLE_MARK", str(marks_path))
        app = create_app(worker_cmd=WORKER_DOUBLE, browser_concurrency=1, wait_margin_s=2.0)

        # A holds the single slot for ~6s; B's whole budget (1s + 2s margin)
        # is 3s — B must return at ~3s, while A is still running.
        a_task = asyncio.create_task(_post(app, "https://slow.test/6", timeout_ms=9000))
        await asyncio.sleep(0.3)  # let A take the slot before B queues
        b_started = time.monotonic()
        resp_b = await asyncio.wait_for(
            _post(app, "https://ok.test/x", timeout_ms=1000), timeout=10
        )
        b_elapsed = time.monotonic() - b_started

        assert resp_b.status_code == 504
        assert "queued" in resp_b.json()["detail"]
        assert b_elapsed <= 3.5, (
            f"queued 504 took {b_elapsed:.1f}s — it camped on the semaphore "
            "instead of returning at its own budget"
        )
        assert not a_task.done(), "B's refusal must not have waited for A to finish"

        resp_a = await asyncio.wait_for(a_task, timeout=30)
        assert resp_a.status_code == 200

        marks = [json.loads(line) for line in marks_path.read_text().splitlines()]
        started_urls = [m["url"] for m in marks if m["event"] == "start"]
        assert started_urls == ["https://slow.test/6"], (
            f"the queued request must not start a worker: starts were {started_urls}"
        )


class TestWorkerCrashMapping:
    """r2 #4: no usable worker reply is a SERVICE fault (503 worker_crash),
    not a target-page worker_failure (502)."""

    @pytest.mark.asyncio
    async def test_worker_suicide_maps_to_503_worker_crash(self, monkeypatch, tmp_path):
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", TOKEN)
        monkeypatch.setenv("CRAWL_DOUBLE_MARK", str(tmp_path / "marks.jsonl"))
        app = create_app(worker_cmd=WORKER_DOUBLE, browser_concurrency=1, wait_margin_s=2.0)

        resp = await _post(app, "https://suicide.test/x", timeout_ms=5000)
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert detail["error_type"] == "worker_crash"
        assert "signal" in detail["error"]
        assert "worker_failure" not in json.dumps(detail)

    @pytest.mark.asyncio
    async def test_unparseable_stdout_maps_to_503_worker_crash(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", TOKEN)
        monkeypatch.setenv("CRAWL_DOUBLE_MARK", str(tmp_path / "marks.jsonl"))
        app = create_app(worker_cmd=WORKER_DOUBLE, browser_concurrency=1, wait_margin_s=2.0)

        resp = await _post(app, "https://garbage.test/x", timeout_ms=5000)
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert detail["error_type"] == "worker_crash"
        assert "signal" in detail["error"] or "exit code" in detail["error"]
        assert "worker_failure" not in json.dumps(detail)


class TestSweepUninterruptible:
    """r2 #1: SIGTERM may request a stop, never interrupt a running sweep."""

    @pytest.mark.skipif(
        not _IS_LINUX,
        reason=(
            "detached (setsid) descendants and the subreaper sweep need Linux "
            "prctl + /proc; macOS degrades to a single group kill with no "
            "interruptible window — verified by the lead in a Linux container"
        ),
    )
    @pytest.mark.asyncio
    async def test_repeated_sigterm_cannot_interrupt_the_sweep(
        self, monkeypatch, tmp_path
    ):
        pids_path = tmp_path / "pids.json"
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", TOKEN)
        monkeypatch.setenv("CRAWL_DOUBLE_MARK", str(tmp_path / "marks.jsonl"))
        monkeypatch.setenv("CRAWL_DOUBLE_PIDS", str(pids_path))
        app = create_app(worker_cmd=WORKER_DOUBLE, browser_concurrency=1, wait_margin_s=2.0)

        request_task = asyncio.create_task(
            _post(app, "https://swarm.test/", timeout_ms=8000)
        )
        deadline = time.monotonic() + 5.0
        while not pids_path.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert pids_path.exists(), "swarm double never recorded its pids"
        pids = json.loads(pids_path.read_text())
        reaper_pid = pids["reaper"]

        # First TERM ends the wait; the reaper then sweeps. Hammer it with
        # more TERMs — on the r1 design any of them landing mid-sweep killed
        # the reaper and leaked every detached stray.
        os.kill(reaper_pid, signal.SIGTERM)
        for _ in range(5):
            await asyncio.sleep(0.1)
            try:
                os.kill(reaper_pid, signal.SIGTERM)
            except ProcessLookupError:
                break

        resp = await asyncio.wait_for(request_task, timeout=20)
        assert resp.status_code == 503  # torn-down fetch → no reply → worker_crash

        for stray in pids["strays"]:
            assert await _wait_pid_gone(stray), (
                f"detached stray {stray} survived SIGTERMs that arrived "
                "during the sweep"
            )


class TestRunnerTeardownWait:
    """r2 #1: the runner's wait for a sweeping reaper is cancellation-proof."""

    @pytest.mark.asyncio
    async def test_cancel_during_teardown_still_waits_for_sweep(
        self, monkeypatch, tmp_path
    ):
        pids_path = tmp_path / "pids.json"
        monkeypatch.setenv("CRAWL_DOUBLE_PIDS", str(pids_path))
        monkeypatch.setenv("CRAWL_DOUBLE_MARK", str(tmp_path / "marks.jsonl"))

        task = asyncio.create_task(
            run_worker(
                {"url": "https://hang.test/ingroup", "tier": "browser"},
                timeout_s=1.5,
                worker_cmd=WORKER_DOUBLE,
            )
        )
        # Timeout fires at 1.5s → SIGTERM sent → runner is waiting out the
        # reaper's sweep; cancel it right in that window. (On fast platforms
        # the reaper may already have exited, so the teardown result —
        # WorkerTimeout — can complete the task just before the cancel lands;
        # either way the tree must be clean below.)
        await asyncio.sleep(2.0)
        task.cancel()
        from src.dalton.crawl_service.runner import WorkerCrash, WorkerTimeout

        with pytest.raises((asyncio.CancelledError, WorkerTimeout, WorkerCrash)):
            await asyncio.wait_for(task, timeout=20)

        deadline = time.monotonic() + 5.0
        while not pids_path.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert pids_path.exists()
        pids = json.loads(pids_path.read_text())
        # The cancelled teardown still waited for the reaper: the tree is gone
        # (the r2 bug SKiPs this wait and SIGKILLs a sweeping reaper).
        assert await _wait_pid_gone(pids["worker"]), "worker survived cancelled teardown"
        assert await _wait_pid_gone(pids["stray"]), "stray survived cancelled teardown"


class TestTeardownDuringReplyTransfer:
    """codex P6b r3: a teardown that lands while the reaper is writing a large
    reply must not leave it blocked on a full pipe (it could never sweep)."""

    @pytest.mark.asyncio
    async def test_teardown_mid_reply_write_drains_pipe_and_reaper_sweeps(
        self, monkeypatch, tmp_path
    ):
        from src.dalton.crawl_service import runner as runner_mod

        pids_path = tmp_path / "pids.json"
        monkeypatch.setenv("CRAWL_DOUBLE_PIDS", str(pids_path))
        # The production image sets PYTHONUNBUFFERED; keep it out so this test
        # does not depend on a signal-shortened raw write to unblock the reaper
        # (codex P6b r4) — only the drain may unblock it here.
        monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
        proc = await asyncio.create_subprocess_exec(
            *runner_mod._supervised_cmd(WORKER_DOUBLE),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        proc.stdin.write(json.dumps({"url": "https://bigfast.test/x", "tier": "browser"}).encode())
        await proc.stdin.drain()
        proc.stdin.close()
        # First bytes of the reply: the reaper now holds the whole ~8 MiB reply
        # and is blocked writing it into a pipe nobody else reads.
        first = await asyncio.wait_for(proc.stdout.read(4096), timeout=20)
        assert first.startswith(b"{")
        started = time.monotonic()
        await runner_mod._terminate_and_reap(proc)
        elapsed = time.monotonic() - started
        # Without the drain the reaper stays blocked until the 10s cap SIGKILLs
        # it (returncode -9, no sweep); with it, it finishes the write, sweeps
        # and exits on its own.
        # (Not asserting the returncode: off Linux the reaper's degraded sweep
        # kills its own group, itself included, so -9 there is by design.)
        assert elapsed < 8.0, f"teardown took {elapsed:.1f}s (reaper blocked on its write?)"
        pids = json.loads(pids_path.read_text())
        assert await _wait_pid_gone(pids["worker"]), "worker survived teardown"
        if _IS_LINUX:
            # Detached strays are swept only by the Linux subreaper.
            assert await _wait_pid_gone(pids["stray"]), "detached stray survived teardown"


class TestCallerDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_cancels_running_fetch_and_frees_slot(
        self, monkeypatch, tmp_path
    ):
        """A caller that walks away must take its worker down with it — and the
        semaphore must be free again afterwards (codex r1 #2)."""

        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", TOKEN)
        monkeypatch.setenv("CRAWL_DOUBLE_MARK", str(tmp_path / "marks.jsonl"))
        pids_path = tmp_path / "pids.json"
        monkeypatch.setenv("CRAWL_DOUBLE_PIDS", str(pids_path))
        app = create_app(worker_cmd=WORKER_DOUBLE, browser_concurrency=1, wait_margin_s=2.0)

        disconnect_gate = asyncio.Event()
        body_sent = {"sent": False}
        sent: list[dict] = []

        async def receive() -> dict:
            if not body_sent["sent"]:
                body_sent["sent"] = True
                return {
                    "type": "http.request",
                    "body": json.dumps(
                        {"url": "https://slow.test/30", "tier": "browser", "timeout_ms": 30000}
                    ).encode(),
                    "more_body": False,
                }
            await disconnect_gate.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/fetch",
            "raw_path": b"/fetch",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"x-service-token", TOKEN.encode()),
                (b"content-type", b"application/json"),
            ],
            "client": ("127.0.0.1", 123),
            "server": ("crawl.test", 80),
        }

        app_task = asyncio.create_task(app(scope, receive, send))
        deadline = time.monotonic() + 5.0
        while not pids_path.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert pids_path.exists(), "worker never started before the disconnect"

        disconnect_gate.set()
        await asyncio.wait_for(app_task, timeout=15)

        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        assert status == 503

        worker_pid = json.loads(pids_path.read_text())["worker"]
        assert await _wait_pid_gone(worker_pid), "cancelled fetch left its worker running"

        # The slot is free again: a fresh request runs straight through.
        resp = await asyncio.wait_for(_post(app, "https://ok.test/x"), timeout=15)
        assert resp.status_code == 200


class TestSweepPidfdInProcess:
    """r2 #2, in-process (Linux-only): sweep kills go through pidfd with an
    identity re-check, and the ECHILD+rescan termination catches a descendant
    that forked-and-exited mid-enumeration (the momentarily-empty-tree race)."""

    @pytest.mark.skipif(
        not _IS_LINUX,
        reason=(
            "needs prctl subreaper, /proc, and os.pidfd_open — Linux only; "
            "the app-level sweep paths above cover the wiring everywhere, and "
            "this in-process check runs where the lead verifies in a container"
        ),
    )
    def test_sweep_uses_pidfd_and_catches_fork_exit_orphan(
        self, monkeypatch, tmp_path
    ):
        import subprocess as subprocess_mod

        from src.dalton.crawl_service import reaper as reaper_mod

        assert reaper_mod._enable_subreaper() is True
        reaper_mod._kill_backend = None
        try:
            real_pidfd_open = os.pidfd_open
            opened: list[int] = []

            def spying_pidfd_open(pid, flags=0):
                opened.append(pid)
                return real_pidfd_open(pid, flags)

            monkeypatch.setattr(os, "pidfd_open", spying_pidfd_open)

            orphan_pid_file = tmp_path / "orphan_pid"
            code = (
                "import subprocess,sys;"
                "gc=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],"
                "start_new_session=True,"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
                "stderr=subprocess.DEVNULL);"
                "open(sys.argv[1],'w').write(str(gc.pid))"
            )
            subprocess_mod.run(
                [sys.executable, "-c", code, str(orphan_pid_file)], check=True
            )
            deadline = time.monotonic() + 5.0
            while not orphan_pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert orphan_pid_file.exists(), "intermediate never recorded the orphan"
            orphan_pid = int(orphan_pid_file.read_text())
            # let the kernel reparent the orphan to this (subreaper) process
            time.sleep(0.3)

            reaper_mod.sweep_descendants(os.getpid())

            assert opened, "the sweep made no pidfd kill attempt"
            try:
                os.kill(orphan_pid, 0)
                gone = False
            except ProcessLookupError:
                gone = True
            assert gone, "fork-and-exit orphan survived the in-process sweep"
        finally:
            reaper_mod._kill_backend = None
