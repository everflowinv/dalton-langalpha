"""Shared uvicorn runtime configuration for Dalton services.

Keeps Dalton's standalone uvicorn services (crawl_service, research_host,
transcription_service) consistent with upstream ``server.py``:
- Run on ``uvloop`` (when not on Windows).
- Size libuv's thread pool (``UV_THREADPOOL_SIZE``) using the same rule as
  upstream ``server.py`` so DNS lookups do not pile up on libuv's 4-thread
  default.
"""

from __future__ import annotations

import os
import sys


def configure_uv_threadpool_size() -> str:
    """Set ``UV_THREADPOOL_SIZE`` if unset, matching upstream ``server.py``."""
    return os.environ.setdefault(
        "UV_THREADPOOL_SIZE",
        str(min(32, (os.process_cpu_count() or 1) + 4)),
    )


def default_uvicorn_loop() -> str:
    """Return the uvicorn ``loop`` value matching upstream ``server.py``."""
    return "uvloop" if sys.platform != "win32" else "asyncio"
