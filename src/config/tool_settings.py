"""
Tool-specific configuration access module.

This module provides configuration helpers for tools (crawler, sitemap, search, etc.)
that read from agent_config.yaml via the shared YAML cache.
"""

import logging
import os
import re
from typing import Any

from ptc_agent.config.file_utils import _deep_merge, _read_yaml
from src.config.core import AGENT_CONFIG_FILE, find_config_file, load_agent_config

logger = logging.getLogger(__name__)

_get_agent_config_dict = load_agent_config

_ENV_VAR_PLACEHOLDER_RE = re.compile(
    r"^\s*(?:\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*)\s*$"
)


def _raw_yaml_uses_env_var(key_path: str) -> bool:
    """True only when the pre-substitution YAML entry at ``key_path`` is a
    ``${VAR}`` / ``$VAR`` placeholder; False for literal strings (e.g. ``"6"``)
    or when the raw YAML cannot be inspected (DALTON-HOOK(concurrency-startup-log),
    P0 fix1 10 + fix2 F: never guess ``env_var`` from ``isinstance(value, str)``,
    and merge ``APP_ENV`` overlays via ``_deep_merge`` so a ``null`` in the
    overlay does not clobber the base entry)."""
    if _get_agent_config_dict is not load_agent_config:
        return False
    try:
        path = find_config_file(AGENT_CONFIG_FILE, env_var="PTC_CONFIG_FILE")
        if path is None or not path.is_file():
            return False
        raw = _read_yaml(path)
        app_env = os.getenv("APP_ENV", "").strip()
        if app_env:
            overlay = path.with_name(f"{path.stem}.{app_env}{path.suffix}")
            if overlay.is_file():
                overlay_raw = _read_yaml(overlay)
                if isinstance(overlay_raw, dict):
                    raw = _deep_merge(raw, overlay_raw)
        cur: Any = raw
        for key in key_path.split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                return False
        return isinstance(cur, str) and bool(_ENV_VAR_PLACEHOLDER_RE.match(cur))
    except Exception:
        return False


def _get_tool_config(key_path: str, default: Any = None) -> Any:
    """
    Get tool configuration from agent_config.yaml.

    Args:
        key_path: Dot-separated key path (e.g., 'crawler.max_concurrent_crawls')
        default: Default value if key not found

    Returns:
        Configuration value or default
    """
    agent_config = _get_agent_config_dict()
    keys = key_path.split('.')
    value = agent_config

    for key in keys:
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return default

    return value


# =============================================================================
# Search Configuration
# =============================================================================

def get_search_api(default: str = "tavily") -> str:
    """The deployment's default search engine; a per-user preference overrides it."""
    return str(_get_tool_config('search_api', default))


# =============================================================================
# Crawler Configuration
# =============================================================================

def get_crawler_http_concurrency(default: int = 20) -> int:
    """Concurrent Tier-1 (curl_cffi) calls. Cheap; cap is high."""
    # Honor legacy max_concurrent_crawls if set, so existing deployments don't break.
    legacy = _get_tool_config('crawler.max_concurrent_crawls')
    if legacy is not None:
        return int(legacy)
    return int(_get_tool_config('crawler.http_concurrency', default))


def resolve_crawler_browser_concurrency(default: int = 2) -> tuple[int, str]:
    """The effective crawl browser concurrency with the channel that set it.

    Source labels: ``config_file`` — a literal ``crawler.browser_concurrency``
    in the merged YAML; ``env_var`` — that YAML value was a ``${VAR}``
    substitution, which the loader leaves as a str (a literal number parses
    as int); ``config_file_legacy`` — the retired ``max_concurrent_crawls``,
    capped at ``default``; ``default`` — the code default.

    DALTON-HOOK(concurrency-startup-log): production ships
    ``agent_config.yaml`` with 6 while the code default is 2, so a startup
    line that logs only the number cannot say which channel won — this
    resolver names the channel for that line.
    """
    explicit = _get_tool_config('crawler.browser_concurrency')
    if explicit is not None:
        source = (
            "env_var"
            if _raw_yaml_uses_env_var("crawler.browser_concurrency")
            else "config_file"
        )
        return int(explicit), source
    legacy = _get_tool_config('crawler.max_concurrent_crawls')
    if legacy is not None:
        return min(int(legacy), default), "config_file_legacy"
    return default, "default"


def get_crawler_browser_concurrency(default: int = 2) -> int:
    """Concurrent Tier-2/3 (Chromium/Camoufox) calls. RAM-bounded."""
    # DALTON-HOOK(crawler-browser-ram-cap): the default is sized for the 4 GB
    # backend container, where the server process itself takes ~0.7–0.8 GB and
    # a running turn more: each stealth browser costs ~400 MB, so 6 (~2.4 GB)
    # pushed the container to 3.99/4 GB and nearly an OOM kill when the
    # pre-market insight tasks opened 3 at once (2026-10). 2 keeps browsers at
    # ~0.8 GB. The value, not the semaphore, changed — the concurrency control
    # is upstream's and untouched.
    return resolve_crawler_browser_concurrency(default)[0]


def get_crawler_page_timeout(default: int = 60000) -> int:
    """Get crawler page timeout in milliseconds."""
    return int(_get_tool_config('crawler.page_timeout', default))


def get_crawler_circuit_failure_threshold(default: int = 5) -> int:
    """Get circuit breaker failure threshold."""
    return int(_get_tool_config('crawler.circuit_breaker.failure_threshold', default))


def get_crawler_circuit_recovery_timeout(default: int = 60) -> int:
    """Get circuit breaker recovery timeout in seconds."""
    return int(_get_tool_config('crawler.circuit_breaker.recovery_timeout', default))


def get_crawler_circuit_success_threshold(default: int = 2) -> int:
    """Get circuit breaker success threshold for closing."""
    return int(_get_tool_config('crawler.circuit_breaker.success_threshold', default))


def get_crawler_queue_max_size(default: int = 100) -> int:
    """Get maximum crawler queue size (admission-control counter)."""
    return int(_get_tool_config('crawler.queue.max_size', default))


def get_crawler_backend(default: str = "scrapling") -> str:
    """Get the crawler backend to use (default: 'scrapling')."""
    return str(_get_tool_config('crawler.backend', default))


def get_crawl_provider(default: str = "firecrawl") -> str:
    """Read crawl_provider from agent_config.yaml; defaults to firecrawl."""
    provider = _get_tool_config('crawl_provider')
    if isinstance(provider, str) and provider:
        return provider
    return default


# =============================================================================
# Sitemap Configuration
# =============================================================================

def is_sitemap_enabled() -> bool:
    """Check if sitemap fetching is enabled."""
    return bool(_get_tool_config('web_fetch.sitemap_enabled', True))


def get_sitemap_max_urls(default: int = 100) -> int:
    """Get maximum URLs to fetch from sitemap."""
    return int(_get_tool_config('web_fetch.sitemap_max_urls', default))


def get_sitemap_max_examples(default: int = 3) -> int:
    """Get maximum example URLs per path prefix in sitemap summary."""
    return int(_get_tool_config('web_fetch.sitemap_max_examples', default))


def get_sitemap_timeout(default: int = 10) -> int:
    """Get sitemap fetch timeout in seconds."""
    return int(_get_tool_config('web_fetch.sitemap_timeout', default))
