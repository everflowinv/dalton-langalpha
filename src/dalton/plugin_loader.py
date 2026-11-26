"""Dalton plugin bundle discovery, env expansion, and partial-override helpers.

Keeps upstream ``src/ptc_agent/config/plugins.py`` and ``src/ptc_agent/config/utils.py``
limited to thin single-line ``DALTON-HOOK(standalone-core)`` delegation calls.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ptc_agent.config.file_utils import substitute_env_vars

logger = logging.getLogger(__name__)


def append_extra_bundle_dirs(base_dirs: list[Path]) -> list[Path]:
    """Append optional commercial connector bundles from ``DALTON_EXTRA_PLUGINS_DIR``."""
    raw_extra = os.environ.get("DALTON_EXTRA_PLUGINS_DIR", "").strip()
    if not raw_extra:
        return base_dirs
    seen_names = {p.name for p in base_dirs}
    extra_dirs: list[Path] = []
    for raw_root in raw_extra.split(os.pathsep):
        cleaned = raw_root.strip()
        if not cleaned:
            continue
        root_path = Path(cleaned).expanduser()
        if not root_path.is_dir():
            logger.warning("DALTON_EXTRA_PLUGINS_DIR entry is not a directory: %s", root_path)
            continue
        try:
            sub_entries = sorted(root_path.iterdir())
        except OSError as exc:
            logger.warning("DALTON_EXTRA_PLUGINS_DIR cannot be read (%s): %s", root_path, exc)
            continue
        for sub in sub_entries:
            if sub.is_dir() and not sub.name.startswith((".", "_")) and sub.name not in seen_names:
                seen_names.add(sub.name)
                extra_dirs.append(sub)
    return base_dirs + extra_dirs


def resolve_bundled_server_env_defaults() -> dict[str, str]:
    """Return profile-aware connector URL defaults unified with backend research-host resolution."""
    from src.dalton.settings import (
        get_dalton_research_host_base_url,
        resolve_connector_env_defaults,
    )

    env_defaults = dict(resolve_connector_env_defaults())
    resolved_rh = get_dalton_research_host_base_url()
    if resolved_rh:
        normalized = resolved_rh.rstrip("/")
        env_defaults["DALTON_RESEARCH_HOST_URL"] = normalized
        env_defaults["DALTON_RESEARCH_HOST_BASE_URL"] = normalized
    return env_defaults


def expand_bundled_server_value(
    value: Any,
    defaults: Mapping[str, str] | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
) -> Any:
    """Recursively expand ``${VAR}`` placeholders with profile-aware defaults and resolved overrides."""
    if isinstance(value, str):
        if (defaults or overrides) and "${" in value:

            def _sub(match: re.Match[str]) -> str:
                var = match.group(1)
                if overrides and var in overrides and overrides[var]:
                    return overrides[var]
                env_val = os.getenv(var)
                if env_val is not None and env_val != "":
                    return env_val
                if defaults and var in defaults:
                    return defaults[var]
                return match.group(0)

            value = re.sub(r"\$\{([^}]+)\}", _sub, value)
        return substitute_env_vars(value)
    if isinstance(value, list):
        return [expand_bundled_server_value(v, defaults, overrides=overrides) for v in value]
    if isinstance(value, dict):
        return {k: expand_bundled_server_value(v, defaults, overrides=overrides) for k, v in value.items()}
    return value


def build_bundled_server_transport_kwargs(
    server_key: str,
    transport: str,
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    """Expand transport fields and inject capability-gated env switches (e.g. ``DALTON_SEC_EXTENDED_TOOLS``)."""
    from src.dalton.capabilities import is_batch3_connectors_enabled
    from src.dalton.settings import get_dalton_research_host_base_url

    env_defaults = resolve_bundled_server_env_defaults()
    resolved_rh = get_dalton_research_host_base_url()
    url_overrides: dict[str, str] = {}
    if resolved_rh:
        normalized_rh = resolved_rh.rstrip("/")
        url_overrides["DALTON_RESEARCH_HOST_URL"] = normalized_rh
        url_overrides["DALTON_RESEARCH_HOST_BASE_URL"] = normalized_rh
    if transport == "stdio":
        env_map = expand_bundled_server_value(dict(entry.get("env") or {}), env_defaults, overrides=url_overrides)
        if server_key == "dalton_sec" and is_batch3_connectors_enabled():
            env_map.setdefault("DALTON_SEC_EXTENDED_TOOLS", "1")
        return {
            "command": expand_bundled_server_value(entry.get("command"), env_defaults, overrides=url_overrides),
            "args": expand_bundled_server_value(list(entry.get("args") or []), env_defaults, overrides=url_overrides),
            "env": env_map,
        }
    return {
        "url": expand_bundled_server_value(entry.get("url"), env_defaults, overrides=url_overrides),
        "headers": expand_bundled_server_value(dict(entry.get("headers") or {}), env_defaults, overrides=url_overrides),
    }


def should_skip_unmounted_partial_override(fields: Mapping[str, Any]) -> bool:
    """Return True when a YAML MCP override lacks command/url because its extra plugin is not mounted."""
    if "command" not in fields and "url" not in fields:
        logger.debug("Skipping partial MCP override for unmounted server %r", fields.get("name"))
        return True
    return False
