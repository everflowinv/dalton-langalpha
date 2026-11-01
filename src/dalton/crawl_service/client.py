"""Backend→crawl-service client: scrapling Tier 2/3 fetched out of container.

Failure policy (deliberate): when the service is unreachable, refuses or
errors, the fetch FAILS — there is no fallback to an in-process browser. A
fallback would move Chromium straight back into the memory-capped backend
container, recreating the exact blast radius (2026-10-31: browser tree
~3.2 GiB inside the 4 GiB backend) this split exists to remove. Callers see
:class:`CrawlServiceError`, which rides the same tier-failure path as a
crashed in-process browser would.

Multi-worker rule: the HTTP client is opened per call (``_open_client``), and
the service token is read from the environment at call time — never a
module-level client or an import-time token.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from src.config.tool_settings import get_crawler_browser_service_url

logger = logging.getLogger(__name__)

#: Client budget over the request's ``timeout_ms``. It must exceed the
#: service's own outer bound (timeout_ms + 25s close/spawn margin) plus HTTP
#: overhead, so the service's classified 502/504 — which carry the killpg
#: guarantee — reach us instead of an opaque client timeout. Deep queueing at
#: the service surfaces here as a timeout, the same way it surfaced as the
#: outer wait_for expiring on the old in-process semaphore queue.
_CLIENT_MARGIN_S = 35.0


class CrawlServiceError(RuntimeError):
    """A browser-tier fetch through the crawl service failed.

    ``crawl_service_fault`` splits the two families the wrapper routes
    differently: True — the service itself was unreachable, refused us, or
    broke protocol (our infrastructure; infra breaker, never the host's
    fault); False — a healthy service reported the TARGET page's fetch
    failing (a 502 ``worker_failure`` carrying the browser's own error text,
    or a 504 fetch-budget overrun). The message for the False family is the
    service's error text verbatim, so downstream keyword classification sees
    exactly what the in-process path would have raised.
    """

    def __init__(
        self,
        message: str,
        *,
        error_type: str = "CrawlServiceError",
        crawl_service_fault: bool = True,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.crawl_service_fault = crawl_service_fault


class _CaseInsensitiveHeaders(Mapping[str, str]):
    """Header mapping preserving scrapling's httpx.Headers ``.get`` semantics."""

    def __init__(self, raw: Mapping[str, Any]) -> None:
        self._d = {str(k).lower(): str(v) for k, v in raw.items()}

    def __getitem__(self, key: str) -> str:
        return self._d[key.lower()]

    def __iter__(self) -> Iterator[str]:
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)


class _TitleQuery:
    """The ``.get()``-bearing node ``_extract_title`` expects from ``page.css``."""

    def __init__(self, value: str | None) -> None:
        self._value = value

    def get(self) -> str | None:
        return self._value


@dataclass(frozen=True)
class ServicePage:
    """A page reconstructed from the crawl service's serialized response.

    Carries exactly the surface ``crawl_with_metadata`` reads off a scrapling
    Response — the service serializes those six facts (see
    ``worker.PAGE_FIELDS``) and nothing else, so no behavior can quietly
    depend on an attribute the service never sent.
    """

    status: int
    url: str
    encoding: str | None
    headers: Mapping[str, str]
    body: bytes
    title: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ServicePage":
        try:
            body = base64.b64decode(payload.get("body_b64") or "", validate=False)
        except (binascii.Error, TypeError, ValueError) as e:
            raise CrawlServiceError(
                f"service sent undecodable body_b64: {e}", error_type="BadPayload"
            ) from e
        raw_headers = payload.get("headers")
        if not isinstance(raw_headers, Mapping):
            raw_headers = {}
        try:
            status = int(payload.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        encoding = payload.get("encoding")
        return cls(
            status=status,
            url=str(payload.get("url") or ""),
            encoding=str(encoding) if encoding else None,
            headers=_CaseInsensitiveHeaders(raw_headers),
            body=body,
            title=str(payload.get("title") or ""),
        )

    @property
    def html(self) -> str:
        """The ``_page_html`` decode the in-process fetcher would have done."""
        return self.body.decode(self.encoding or "utf-8", errors="replace")

    def css(self, selector: str) -> _TitleQuery:
        # ``title::text`` is the only selector this DTO exists to serve — the
        # one ``_extract_title`` issues. A serialized page has no DOM behind
        # any other selector, so anything else yields nothing rather than a lie.
        if selector.strip().lower() == "title::text":
            return _TitleQuery(self.title or None)
        return _TitleQuery(None)


def _open_client(timeout_s: float) -> httpx.AsyncClient:
    """A per-call client (multi-worker rule: no module-level client state).

    A module-level singleton would outlive the event loop that created it;
    each request gets its own, sized to its own budget. Tests monkeypatch this
    factory to install a ``MockTransport``.
    """
    return httpx.AsyncClient(timeout=timeout_s)


def _warn_unavailable(tier: str, started: float, err: CrawlServiceError) -> None:
    logger.warning(
        "crawl_service_unavailable: %s",
        json.dumps(
            {
                "event": "crawl_service_unavailable",
                "tier": tier,
                "error_type": err.error_type,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "detail": str(err)[:300],
            },
            ensure_ascii=False,
        ),
    )


def _error_from_response(resp: httpx.Response) -> CrawlServiceError:
    """Classify a non-200 reply: whose fault, and with what full text.

    The split that matters for the warning and the breakers:
      - 502 whose body is the service's structured ``worker_failure`` — the
        TARGET page failed inside a healthy service. The browser's full error
        text rides ``detail["error"]`` (the service truncates at 1000; the
        client does not cut it again), so keyword classification downstream
        reads the same text the in-process path would have raised.
      - 504 — the supervised fetch overran its own budget (slow/hung target
        page). Same family: the service did its job, the page did not.
      - anything else (401 auth, 400 policy, 500/503 service bug or overload,
        non-JSON bodies) — the service side itself; this is the family the
        ``crawl_service_unavailable`` warning names.
    """
    try:
        detail = resp.json().get("detail")
    except ValueError:
        detail = None

    if resp.status_code == 502 and isinstance(detail, dict):
        if detail.get("error_type") == "worker_failure":
            error = str(detail.get("error") or "worker reported failure")
            return CrawlServiceError(
                error, error_type="worker_failure", crawl_service_fault=False
            )
        return CrawlServiceError(
            f"crawl service returned malformed 502 detail: {str(detail)[:300]}",
            error_type="bad_reply",
        )
    if resp.status_code == 503:
        # The service's own worker died without a usable reply (OOM kill,
        # crash, would not start) or the service is overloaded — our
        # infrastructure either way, never the page's.
        if isinstance(detail, dict) and detail.get("error_type") == "worker_crash":
            return CrawlServiceError(
                f"crawl service worker crashed: "
                f"{str(detail.get('error') or 'no detail')}",
                error_type="worker_crash",
            )
        text = detail if isinstance(detail, str) else resp.text[:300]
        return CrawlServiceError(
            f"crawl service returned HTTP 503: {text}",
            error_type="http_503",
        )
    if resp.status_code == 504:
        text = detail if isinstance(detail, str) else resp.text[:300]
        return CrawlServiceError(
            f"target page fetch overran its budget: {text}",
            error_type="worker_timeout",
            crawl_service_fault=False,
        )
    text = detail if isinstance(detail, str) else resp.text[:300]
    return CrawlServiceError(
        f"crawl service returned HTTP {resp.status_code}: {text}",
        error_type=f"http_{resp.status_code}",
    )


async def fetch_via_service(
    url: str,
    *,
    tier: str,
    solve_cloudflare: bool = False,
    timeout_ms: int,
) -> tuple[ServicePage, str, int]:
    """Fetch one URL through the crawl service.

    Returns the same ``(page, html_body, status)`` shape as
    ``fetch_with_session``; raises :class:`CrawlServiceError` on any failure
    (see the module docstring for why there is no in-process fallback).
    """
    base = get_crawler_browser_service_url()
    if not base:
        raise CrawlServiceError("crawler.browser_service_url is not set")
    started = time.monotonic()
    try:
        headers: dict[str, str] = {}
        token = os.environ.get("INTERNAL_SERVICE_TOKEN", "")
        if token:
            headers["X-Service-Token"] = token
        async with _open_client(timeout_ms / 1000.0 + _CLIENT_MARGIN_S) as client:
            resp = await client.post(
                f"{base}/fetch",
                json={
                    "url": url,
                    "tier": tier,
                    "solve_cloudflare": solve_cloudflare,
                    "timeout_ms": timeout_ms,
                },
                headers=headers,
            )
        if resp.status_code != 200:
            raise _error_from_response(resp)
        page = ServicePage.from_payload(resp.json())
    except asyncio.CancelledError:
        raise
    except CrawlServiceError as e:
        # Only the service's own failures are an availability event; a target
        # page failing through a healthy service is an ordinary crawl failure
        # (the tier logic logs enough about those already).
        if e.crawl_service_fault:
            _warn_unavailable(tier, started, e)
        raise
    except Exception as e:
        err = CrawlServiceError(
            f"crawl service request failed: {type(e).__name__}: {e}",
            error_type=type(e).__name__,
        )
        _warn_unavailable(tier, started, err)
        raise err from e
    return page, page.html, page.status
