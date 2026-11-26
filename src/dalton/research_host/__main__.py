"""CLI entrypoint for ``python -m src.dalton.research_host``."""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
from collections.abc import Mapping, MutableMapping
from pathlib import Path

import uvicorn

from src.dalton.capabilities import load_deployment_capability_env
from src.dalton.config_env import parse_and_resolve_dotenv
from src.dalton.research_host.app import ResearchHostConfig, create_app

logger = logging.getLogger("dalton-research-host")

# Strict allowlist for optional host-specific env files (`--env-file` /
# `DALTON_RESEARCH_HOST_ENV_FILE`). Only connection, token, path, and host-runtime
# keys are accepted; any profile or capability-affecting key is ignored with a warning
# so research-host capabilities can never diverge from the backend's shared `.env`.
# Extra connector modules loaded via `DALTON_RESEARCH_HOST_EXTRA_MODULES` may declare
# additional credential/runtime keys via `EXTRA_HOST_ENV_KEYS`.
ALLOWED_HOST_ENV_KEYS: frozenset[str] = frozenset(
    {
        "DALTON_RESEARCH_HOME",
        "DALTON_WIKI_DATA_DIR",
        "DALTON_RESEARCH_CODE",
        "DALTON_RESEARCH_HOST_BIND",
        "DALTON_RESEARCH_HOST_PORT",
        "DALTON_RESEARCH_HOST_TOKEN",
        "DALTON_RESEARCH_ADMIN_TOKEN",
        "DALTON_BRIDGE_ADMIN_TOKEN",
        "DALTON_LEGACY_BRIDGE_MCP_URL",
        "DALTON_BRIDGE_TOKEN",
        "DALTON_WIKI_INGEST_AUDIT_FILE",
        "DALTON_RESEARCH_FANOUT_GRACE_S",
        "DALTON_RESEARCH_HOST_EXTRA_MODULES",
        "DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH",
        "DALTON_HOST_OWNERSHIP_FILE",
    }
)


def _discover_extra_module_env_keys(
    env: Mapping[str, str],
    host_env_file: str | None = None,
) -> frozenset[str]:
    extra_mods_raw = str(env.get("DALTON_RESEARCH_HOST_EXTRA_MODULES") or "").strip()
    extra_paths_raw = str(env.get("DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH") or "").strip()
    if host_env_file and (not extra_mods_raw or not extra_paths_raw):
        host_path = Path(host_env_file).expanduser()
        if host_path.is_file():
            parsed = parse_and_resolve_dotenv(host_path.resolve(), base_env=env, override=False)
            if not extra_mods_raw:
                extra_mods_raw = str(parsed.get("DALTON_RESEARCH_HOST_EXTRA_MODULES") or "").strip()
            if not extra_paths_raw:
                extra_paths_raw = str(parsed.get("DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH") or "").strip()
    if not extra_mods_raw:
        return frozenset()
    for raw_p in extra_paths_raw.split(","):
        p = raw_p.strip()
        if p and p not in sys.path:
            sys.path.insert(0, p)
    discovered: set[str] = set()
    for raw_m in extra_mods_raw.split(","):
        mod_name = raw_m.strip()
        if not mod_name:
            continue
        try:
            mod = importlib.import_module(mod_name)
            keys = getattr(mod, "EXTRA_HOST_ENV_KEYS", None)
            if keys:
                for k in keys:
                    if isinstance(k, str) and k.strip():
                        discovered.add(k.strip())
        except Exception as exc:
            logger.debug("Skipping EXTRA_HOST_ENV_KEYS discovery for %s: %s", mod_name, exc)
    return frozenset(discovered)


def _is_allowed_host_env_key(key: str) -> bool:
    return key.strip() in ALLOWED_HOST_ENV_KEYS


def load_shared_and_host_env(
    *,
    env_file: str | None = None,
    repo_env_file: str | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> list[Path]:
    """Load shared backend `.env` and optional host env file via `load_deployment_capability_env`."""
    target_env: MutableMapping[str, str] = os.environ if environ is None else environ
    loaded: list[Path] = []
    host_raw = env_file or target_env.get("DALTON_RESEARCH_HOST_ENV_FILE", "").strip()
    extra_keys = _discover_extra_module_env_keys(target_env, host_raw or None)
    merged = load_deployment_capability_env(
        repo_env_file=repo_env_file,
        service_env_file=host_raw or None,
        environ=target_env,
        allowed_service_keys=ALLOWED_HOST_ENV_KEYS | extra_keys,
        loaded_files=loaded,
    )
    for k, v in merged.items():
        target_env.setdefault(k, v)
    return loaded


def build_config_from_env(
    *,
    env_file: str | None = None,
    repo_env_file: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ResearchHostConfig:
    """Build `ResearchHostConfig` using the shared backend capability env + host connection settings."""
    if environ is None:
        load_shared_and_host_env(env_file=env_file, repo_env_file=repo_env_file)
        return ResearchHostConfig.from_env()
    merged = dict(environ)
    load_shared_and_host_env(env_file=env_file, repo_env_file=repo_env_file, environ=merged)
    return ResearchHostConfig.from_env(merged)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dalton Research Host (MCP + ingest service)")
    parser.add_argument("--host", default=None, help="Bind host (default from DALTON_RESEARCH_HOST_BIND or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default from DALTON_RESEARCH_HOST_PORT or 8951)")
    parser.add_argument("--env-file", default=None, help="Optional host env file (connection/token/path settings only)")
    parser.add_argument("--repo-env-file", default=None, help="Optional shared backend .env file path")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = build_config_from_env(env_file=args.env_file, repo_env_file=args.repo_env_file)
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = int(args.port)

    app = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
