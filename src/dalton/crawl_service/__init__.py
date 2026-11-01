"""Dedicated crawl-browser service (P6b).

The Dalton backend container (4 GB) used to host scrapling's Chromium /
Camoufox trees next to the server process; on 2026-10-31 00:18 one runaway
crawl was attributed ~3.2 GiB and the container hit 4095.96/4096 MiB, taking
chat and streaming down with it. This package is the side that owns the
browsers from then on: a single-process FastAPI service whose every fetch
runs in a throwaway process group, killed whole — Chromium helpers included —
on timeout, cancellation or crash.

Layout:
  - ``app``       HTTP surface (``POST /fetch``, ``GET /health``) + auth + SSRF
  - ``runner``    spawn-one-worker-per-fetch with guaranteed group teardown
  - ``worker``    the in-subprocess fetch + page serialization
  - ``client``    the backend-side caller (reconstructs the page object)
  - ``__main__``  ``uv run python -m src.dalton.crawl_service``

The backend opts in by setting ``crawler.browser_service_url`` (see
``agent_config.dalton.yaml``); with it unset, tier 2/3 keep running in-process
exactly as upstream does.
"""
