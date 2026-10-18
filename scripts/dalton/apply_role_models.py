#!/usr/bin/env python3
"""Update `plugins/dalton/agents/dalton-reviewer.md` model based on lead model family (P0.6).

Selects the first tool-capable model in `publish.reviewer_models` whose vendor
family (`family_of()`) differs from the active lead model, updates the YAML
frontmatter of `plugins/dalton/agents/dalton-reviewer.md`, and prints any
role policy warnings from `check_roles()`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.dalton.llm.role_policy import (  # noqa: E402
    check_roles,
    family_of,
    select_independent_reviewer,
)
from src.dalton.settings import get_dalton_settings  # noqa: E402

AGENT_CONFIG_PATH = REPO_ROOT / "agent_config.dalton.yaml"
REVIEWER_MD_PATH = REPO_ROOT / "plugins" / "dalton" / "agents" / "dalton-reviewer.md"


def _update_reviewer_frontmatter_model(md_path: Path, new_model: str) -> bool:
    if not md_path.exists():
        return False
    text = md_path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return False
    end_idx = text.find("\n---\n", 4)
    if end_idx == -1:
        return False
    fm = text[4:end_idx]
    body = text[end_idx + 5 :]
    if re.search(r"^model:\s*.*$", fm, flags=re.MULTILINE):
        fm_new = re.sub(r"^model:\s*.*$", f"model: {new_model}", fm, flags=re.MULTILINE)
    else:
        fm_new = fm.rstrip() + f"\nmodel: {new_model}"
    md_path.write_text(f"---\n{fm_new}\n---\n{body}", encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply role model policy and update dalton-reviewer.md")
    parser.add_argument(
        "--lead-model",
        default=None,
        help="Override lead model key (defaults to llm.name in agent_config.dalton.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not modify dalton-reviewer.md on disk",
    )
    args = parser.parse_args(argv)

    cfg = {}
    if AGENT_CONFIG_PATH.exists():
        cfg = yaml.safe_load(AGENT_CONFIG_PATH.read_text(encoding="utf-8")) or {}

    if args.lead_model:
        cfg.setdefault("llm", {})["name"] = args.lead_model

    lead_model = str((cfg.get("llm") or {}).get("name") or "claude-opus-5-5-oauth")
    candidates = list(get_dalton_settings().publish.reviewer_models)

    # Draft-stage subagent reviewer needs tool calling to read sources
    subagent_reviewer = select_independent_reviewer(
        lead_model, candidates, require_tool_calling=True
    )
    # Server-side publish reviewer does not require tool calling
    server_reviewer = select_independent_reviewer(
        lead_model, candidates, require_tool_calling=False
    )
    warnings = check_roles(cfg, reviewer_models=candidates)

    updated = False
    if subagent_reviewer and not args.dry_run:
        updated = _update_reviewer_frontmatter_model(REVIEWER_MD_PATH, subagent_reviewer)

    result = {
        "lead_model": lead_model,
        "lead_family": family_of(lead_model),
        "server_reviewer_model": server_reviewer,
        "server_reviewer_family": family_of(server_reviewer) if server_reviewer else None,
        "subagent_reviewer_model": subagent_reviewer,
        "subagent_reviewer_family": family_of(subagent_reviewer) if subagent_reviewer else None,
        "reviewer_md_updated": updated,
        "warnings": warnings,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
