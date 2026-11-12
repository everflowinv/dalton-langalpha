"""HTTP GET used by the filing scan. Stdlib only. Tests replace ``fetch_bytes``."""

from __future__ import annotations

import os
import threading
import time
import urllib.request

_LOCK = threading.Lock()
_LAST_REQUEST = 0.0


def sec_user_agent() -> str:
    """SEC fair-access identity. Prefer the operator's env value.

    The fallback is the same string as ``get_sec_user_agent`` in
    ``src/dalton/settings.py``. This script cannot import that module.
    """
    for key in ("EDGAR_IDENTITY", "SEC_USER_AGENT", "DALTON_SEC_USER_AGENT"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return "DaltonResearch research@everflowinv.com"


def sec_headers() -> dict[str, str]:
    return {
        "User-Agent": sec_user_agent(),
        "Accept": "application/json,text/html,application/xml;q=0.9,*/*;q=0.8",
    }


def fetch_bytes(
    url: str,
    timeout: int = 20,
    headers: dict[str, str] | None = None,
    min_interval: float = 0.0,
) -> bytes:
    """GET ``url`` and return the body. ``min_interval`` spaces calls from this process."""
    global _LAST_REQUEST
    if min_interval > 0:
        with _LOCK:
            gap = time.time() - _LAST_REQUEST
            if gap < min_interval:
                time.sleep(min_interval - gap)
            _LAST_REQUEST = time.time()
    request = urllib.request.Request(url, headers=dict(headers or {}))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            import gzip

            payload = gzip.decompress(payload)
    return payload
