"""CLI entrypoint for ``python -m src.dalton.research_host``."""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path

import uvicorn

from src.dalton.config_env import load_shared_dotenv, parse_and_resolve_dotenv
from src.dalton.research_host.app import ResearchHostConfig, create_app

logger = logging.getLogger("dalton-research-host")

# Strict allowlist for optional host-specific env files (`--env-file` /
# `DALTON_RESEARCH_HOST_ENV_FILE`). Only connection, token, path, and host-runtime
# keys are accepted; any profile or capability-affecting key (e.g. `APP_ENV`,
# `DALTON_PROFILE`, `DALTON_CAP_*`, `CHANNEL_GATEWAY_URL`, `DALTON_TRANSCRIPTION_ENABLED`,
# `DALTON_RESEARCHDATA_URL`, `DALTON_BRIDGE_URL`, etc.) is ignored with a warning so
# research-host capabilities can never diverge from the backend's shared `.env`.
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
        "DALTON_ALPHAENGINE_USAGE_FILE",
        "DALTON_WIKI_INGEST_AUDIT_FILE",
        "DALTON_RESEARCH_FANOUT_GRACE_S",
        "DALTON_RESEARCH_HOST_EXTRA_MODULES",
        "DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH",
        "GUIDEPOINT_CLIENT_ID",
        "GUIDEPOINT_CLIENT_SECRET",
        "GUIDEPOINT_TOKENS_FILE",
        "DALTON_GUIDEPOINT_TOKENS_FILE",
    }
)


def _is_allowed_host_env_key(key: str) -> bool:
    return key.strip() in ALLOWED_HOST_ENV_KEYS


def load_shared_and_host_env(
    *,
    env_file: str | None = None,
    repo_env_file: str | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> list[Path]:
    """Load shared backend `.env` via ``src.dalton.config_env.load_shared_dotenv`` and optional host env file.

    Precedence (highest to lowest):
      1. Explicit process environment variables.
      2. Shared backend repo `.env` parsed via ``src.dalton.config_env.load_shared_dotenv``,
         guaranteeing identical profile, interpolation, and capability resolution between
         backend and research-host.
      3. Optional host-specific env file (``--env-file`` / ``DALTON_RESEARCH_HOST_ENV_FILE``)
         restricted to ``ALLOWED_HOST_ENV_KEYS`` ONLY — any other key is ignored with a warning
         so it can never alter capability evaluation.
    """
    target_env: MutableMapping[str, str] = os.environ if environ is None else environ
    loaded: list[Path] = []

    shared_loaded = load_shared_dotenv(repo_env_file, environ=target_env)
    if shared_loaded is not None:
        loaded.append(shared_loaded)

    host_raw = (env_file or target_env.get("DALTON_RESEARCH_HOST_ENV_FILE", "").strip())
    if host_raw:
        host_path = Path(host_raw).expanduser()
        if host_path.is_file():
            resolved_host = host_path.resolve()
            parsed_host = parse_and_resolve_dotenv(
                resolved_host,
                base_env=target_env,
                override=False,
            )
            for k, v in parsed_host.items():
                if not k:
                    continue
                if not _is_allowed_host_env_key(k):
                    logger.warning(
                        "Ignoring non-allowlisted key %r in research-host env file %s "
                        "(only connection/token/path keys are permitted in host env files)",
                        k,
                        resolved_host,
                    )
                    continue
                target_env.setdefault(k, v)
            loaded.append(resolved_host)

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
