#!/usr/bin/env python3
"""Dump the effective merged ModelConfig catalog for P0N.1 / P0N.2 equivalence checks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dump effective LangAlpha + Dalton LLM catalog.")
    parser.add_argument(
        "--exclude-prefix",
        default="",
        help="Optional model key prefix to exclude (e.g. 'ocb-').",
    )
    args = parser.parse_args(argv)

    from src.llms.llm import ModelConfig

    cfg = ModelConfig()
    prefix = args.exclude_prefix.strip()

    llm_config = {
        k: v
        for k, v in sorted(cfg.llm_config.items())
        if not (prefix and k.startswith(prefix))
    }
    flat_providers = {
        k: v
        for k, v in sorted(cfg._flat_providers.items())
        if not (prefix and k == "openclaw-broker")
    }
    pricing_models = {
        k: v
        for k, v in sorted((cfg.manifest.get("models") or {}).items())
        if not (prefix and k == "openclaw-broker")
    }

    payload = {
        "llm_config": llm_config,
        "flat_providers": flat_providers,
        "pricing_models": pricing_models,
    }
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
