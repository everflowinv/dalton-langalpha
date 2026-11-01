"""``uv run python -m src.dalton.crawl_service``: run the crawl service.

One uvicorn worker on purpose: the browser budget is the in-app semaphore
(sourced from ``crawler.browser_concurrency``), and N workers would each spawn
that many browsers — silently multiplying the RAM the container is capped for.
The port stays fixed at 8100 (compose names it in ``crawler.browser_service_url``
and publishes nothing); ``CRAWL_SERVICE_PORT`` exists only for manual runs.
"""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from .app import create_app


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s %(message)s"
    )
    # Compose interpolates the token with a default-empty (${VAR:-}), so a
    # missing variable must not pass silently: say it once, loudly. The
    # service keeps running fail-closed (every /fetch 401s) rather than
    # crash-looping under restart: unless-stopped.
    if not os.environ.get("INTERNAL_SERVICE_TOKEN", "").strip():
        logging.getLogger(__name__).error(
            "INTERNAL_SERVICE_TOKEN is empty: every /fetch request will be "
            "refused (401). Set it in .env for the crawl service."
        )
    port = int(os.environ.get("CRAWL_SERVICE_PORT", "8100"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port, workers=1, log_level="info")


if __name__ == "__main__":
    main()
