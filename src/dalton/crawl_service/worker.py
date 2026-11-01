"""One browser/stealth fetch per process: job JSON on stdin, reply on stdout.

Runs as the leader of its own process group under the crawl service (see
``runner``), so the parent can kill the whole tree. Stdlib-only at import and
scrapling imported lazily inside ``_fetch``, so even a broken environment
yields a parseable error reply instead of a child that dies silently.

Session construction mirrors ``ScraplingCrawler._tier2_fetch`` /
``_tier3_fetch`` exactly (browser: disable_resources + network_idle; stealth:
network_idle + the caller-decided CF solver) — the service must not drift
from the in-process tiers it replaced, or the same URL would behave
differently depending on where it was fetched.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import traceback
from typing import Any

#: Wire fields of a serialized page — exactly the surface
#: ``ScraplingCrawler.crawl_with_metadata`` and its helpers read off a
#: scrapling Response, and nothing more:
#:   status           tier logic (429/terminal checks, _needs_stealth)
#:   body (body_b64)  _page_html's decode + _binary_payload's sniff
#:   encoding         _page_html's decode
#:   headers          _binary_payload's content-type check
#:   url              _binary_output's final_url
#:   title            _extract_title's css("title::text").get()
PAGE_FIELDS = ("status", "url", "encoding", "headers", "body_b64", "title")


def _serialize_page(page: Any) -> dict[str, Any]:
    """The page object reduced to the fields the backend's tier logic reads."""
    body = getattr(page, "body", b"") or b""
    if not isinstance(body, (bytes, bytearray)):
        body = str(body).encode("utf-8", errors="replace")
    headers: dict[str, str] = {}
    try:
        raw_headers = getattr(page, "headers", None) or {}
        headers = {str(k).lower(): str(v) for k, v in raw_headers.items()}
    except Exception:  # noqa: BLE001 - headers are optional to every reader
        headers = {}
    title = ""
    try:
        title = page.css("title::text").get() or ""
    except Exception:  # noqa: BLE001 - a page without parsable HTML has no title
        title = ""
    try:
        status = int(getattr(page, "status", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    encoding = getattr(page, "encoding", None)
    return {
        "status": status,
        "url": str(getattr(page, "url", "") or ""),
        "encoding": str(encoding) if encoding else None,
        "headers": headers,
        "body_b64": base64.b64encode(bytes(body)).decode("ascii"),
        "title": str(title),
    }


async def _fetch(job: dict[str, Any]) -> dict[str, Any]:
    from mcp_servers._browser import fetch_with_session, make_session

    timeout_ms = int(job.get("timeout_ms") or 30000)
    if job.get("tier") == "browser":
        session = make_session(
            "browser",
            timeout_ms=timeout_ms,
            disable_resources=True,
            network_idle=True,
        )
        page, _html, _status = await fetch_with_session(session, job["url"])
    else:
        session = make_session(
            "stealth", timeout_ms=timeout_ms, network_idle=True
        )
        page, _html, _status = await fetch_with_session(
            session, job["url"], solve_cloudflare=bool(job.get("solve_cloudflare"))
        )
    return {"ok": True, "page": _serialize_page(page)}


def main() -> None:
    try:
        job = json.loads(sys.stdin.read())
        reply = asyncio.run(_fetch(job))
    except BaseException as e:  # noqa: BLE001 - the reply channel is the only reporter
        reply = {
            "ok": False,
            "error_type": type(e).__name__,
            "error": f"{e}"[:2000],
            "traceback": traceback.format_exc()[-4000:],
        }
    sys.stdout.write(json.dumps(reply, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
