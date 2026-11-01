"""HTTP surface of the crawl service: ``POST /fetch`` and ``GET /health``.

Single process on purpose (see ``__main__``): the browser concurrency budget
is the in-app semaphore below, sourced from the same
``crawler.browser_concurrency`` the backend reads, so both sides of the split
agree on how many browsers may exist. The service publishes no ports — it is
reachable only inside the compose ``crawl`` network as
``http://crawler:8100``, and every ``/fetch`` must present ``X-Service-Token``
equal to the deployment's ``INTERNAL_SERVICE_TOKEN`` (the same variable, from
the same ``.env``, the backend authenticates its internal callers with). The
token is read at call time, never at import.

A request's ``timeout_ms`` covers its WHOLE stay here, queueing included: the
deadline is taken when the request enters, and a request that acquires the
semaphore past its deadline is refused without ever starting a worker. From
that same moment the caller's connection is polled for disconnects — uvicorn
does not cancel a handler task when its client walks away, and without this a
batch of requests the backend has already abandoned would keep taking the
semaphore one by one and starve the live ones.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
import weakref
from collections.abc import Sequence
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from mcp_servers._browser import url_block_reason

from .runner import (
    WORKER_WAIT_MARGIN_S,
    WorkerCrash,
    WorkerReportedFailure,
    WorkerTimeout,
    run_worker,
)

logger = logging.getLogger(__name__)

#: Disconnect poll cadence. ASGI has no push for ``http.disconnect``; a poll
#: is the only way a running handler learns its caller is gone.
_DISCONNECT_POLL_S = 0.5


class FetchRequest(BaseModel):
    url: str
    tier: Literal["browser", "stealth"]
    solve_cloudflare: bool = False
    timeout_ms: int = Field(default=30000, ge=1000, le=180000)


class _QueueDeadlinePassed(Exception):
    """The request spent its whole budget waiting for the semaphore."""


class _LoopLocalGate:
    """A browser-concurrency bound, scoped per event loop.

    An ``asyncio.Semaphore`` acquired in one loop cannot be released from
    another; production runs one loop forever, but each test client brings its
    own, so the gate keys a fresh semaphore per loop (weakly — a retired loop
    takes its semaphore with it). This is an execution resource like the
    crawler's own ``_browser_sem``, not cross-request state: nothing reads it
    as truth.
    """

    def __init__(self, value: int) -> None:
        self._value = value
        self._by_loop: weakref.WeakKeyDictionary[Any, asyncio.Semaphore] = (
            weakref.WeakKeyDictionary()
        )

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        sem = self._by_loop.get(loop)
        if sem is None:
            sem = asyncio.Semaphore(self._value)
            self._by_loop[loop] = sem
        return sem

    async def acquire(self, timeout_s: float) -> asyncio.Semaphore:
        """Take a slot within ``timeout_s``; raises ``asyncio.TimeoutError``
        past it, so a queued request outliving its own budget returns now
        instead of camping on the semaphore until some other fetch frees it.
        """
        sem = self._semaphore()
        await asyncio.wait_for(sem.acquire(), timeout=timeout_s)
        return sem


def _require_service_token(request: Request) -> None:
    """401 unless ``X-Service-Token`` equals ``INTERNAL_SERVICE_TOKEN``.

    Fail closed: with the variable unset nothing matches — a tokenless
    deployment misconfiguration must not open the browser spawner to every
    container on the network.
    """
    expected = os.environ.get("INTERNAL_SERVICE_TOKEN", "")
    supplied = request.headers.get("X-Service-Token", "")
    if not expected or not supplied or not hmac.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Invalid service token")


async def _watch_disconnect(request: Request) -> None:
    """Returns once the caller disconnected (polled; ASGI has no push)."""
    while True:
        if await request.is_disconnected():
            return
        await asyncio.sleep(_DISCONNECT_POLL_S)


def create_app(
    *,
    worker_cmd: Sequence[str] | None = None,
    browser_concurrency: int | None = None,
    wait_margin_s: float | None = None,
) -> FastAPI:
    """App factory; the keyword arguments exist as test seams.

    Production resolves the browser concurrency from the merged agent config
    (the same ``crawler.browser_concurrency`` the backend reads) and uses the
    real worker entry point. ``__main__`` is the only production caller —
    there is deliberately no module-level app instance (no import-time config
    load, no second app in tests).
    """
    if browser_concurrency is None:
        from src.config.tool_settings import resolve_crawler_browser_concurrency

        browser_concurrency = resolve_crawler_browser_concurrency()[0]
    gate = _LoopLocalGate(max(1, int(browser_concurrency)))
    margin = WORKER_WAIT_MARGIN_S if wait_margin_s is None else float(wait_margin_s)

    app = FastAPI(
        title="dalton-crawl-service",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/fetch")
    async def fetch(
        req: FetchRequest, request: Request, _: None = Depends(_require_service_token)
    ) -> dict[str, Any]:
        # The SSRF line re-checked here, not only at the caller: this service
        # is the process that dials the URL, so the guard must live at the
        # dial regardless of what the caller already verified.
        reason = url_block_reason(req.url)
        if reason is not None:
            raise HTTPException(status_code=400, detail=reason)

        # The budget spans queueing: an abandoned-in-queue request must not
        # still buy a browser after its caller gave up or timed out.
        started = time.monotonic()
        deadline = started + req.timeout_ms / 1000.0 + margin
        job: dict[str, Any] = {
            "url": req.url,
            "tier": req.tier,
            "solve_cloudflare": req.solve_cloudflare,
            "timeout_ms": req.timeout_ms,
        }

        async def _run_supervised() -> dict[str, Any]:
            # The slot wait itself is bounded by the remaining budget: without
            # this, a queued request whose caller has already given up keeps
            # camping on the semaphore and returns only when some other fetch
            # deigns to release it (or never — the client times out first and
            # miscounts the outage as an infra failure).
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                raise _QueueDeadlinePassed
            try:
                sem = await gate.acquire(timeout_s=remaining_s)
            except asyncio.TimeoutError:
                raise _QueueDeadlinePassed from None
            try:
                budget_s = deadline - time.monotonic()
                if budget_s <= 0:
                    raise _QueueDeadlinePassed
                return await run_worker(
                    job, timeout_s=budget_s, worker_cmd=worker_cmd
                )
            finally:
                sem.release()

        run_task = asyncio.create_task(_run_supervised(), name="crawl-fetch")
        watch_task = asyncio.create_task(
            _watch_disconnect(request), name="crawl-disconnect-watch"
        )
        try:
            done, _ = await asyncio.wait(
                {run_task, watch_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if run_task not in done:
                # Caller gone: cancel the fetch and WAIT OUT its teardown —
                # the reaper sweep and the semaphore release — before
                # returning, so the next request inherits a clean slot.
                run_task.cancel()
                try:
                    await run_task
                except (
                    asyncio.CancelledError,
                    WorkerTimeout,
                    WorkerCrash,
                    WorkerReportedFailure,
                ):
                    pass
                raise HTTPException(status_code=503, detail="client disconnected")
            reply = run_task.result()
        except WorkerTimeout as e:
            logger.warning(
                "crawl fetch budget exceeded; supervised tree killed: "
                "tier=%s url=%s",
                req.tier,
                req.url,
            )
            raise HTTPException(status_code=504, detail=str(e)) from e
        except WorkerReportedFailure as e:
            # The worker ANSWERED: the target page's own failure, with the
            # browser's error text — a target fault, not a service one.
            raise HTTPException(
                status_code=502,
                detail={"error_type": "worker_failure", "error": str(e)[:1000]},
            ) from e
        except WorkerCrash as e:
            # No usable reply — the worker was killed (an OOM kill included),
            # crashed, or would not start. That is this service's failure, not
            # the page's; the detail carries only the exit/signal summary.
            logger.warning(
                "crawl worker crashed: tier=%s url=%s %s",
                req.tier,
                req.url,
                e,
            )
            raise HTTPException(
                status_code=503,
                detail={"error_type": "worker_crash", "error": str(e)},
            ) from e
        except _QueueDeadlinePassed:
            logger.warning(
                "crawl fetch spent its whole budget queued: tier=%s url=%s",
                req.tier,
                req.url,
            )
            raise HTTPException(
                status_code=504,
                detail="request budget expired while queued for a browser slot",
            ) from None
        finally:
            watch_task.cancel()
            try:
                await watch_task
            except asyncio.CancelledError:
                pass

        page = reply.get("page")
        if not isinstance(page, dict):
            # ok=true without a page is not a valid protocol reply either.
            raise HTTPException(
                status_code=503,
                detail={"error_type": "worker_crash", "error": "reply carried no page"},
            )
        logger.info(
            "crawl fetch done: tier=%s status=%s elapsed_ms=%.0f url=%s",
            req.tier,
            page.get("status"),
            (time.monotonic() - started) * 1000,
            req.url,
        )
        return page

    return app
