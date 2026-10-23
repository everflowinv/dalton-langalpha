"""Dalton built-in skill registry definitions.

Merged into ``SKILL_REGISTRY`` at the end of ``registry.py`` via
``DALTON-HOOK(skills-registry)``. Each entry's ``description`` is loaded once at
import time from ``plugins/dalton/skills/<name>/SKILL.md`` YAML frontmatter so
the registry and ``SKILL.md`` never drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .registry import SkillDefinition

# src/ptc_agent/agent/middleware/skills/dalton_registry.py -> repo root
DALTON_SKILLS_DIR: Path = (
    Path(__file__).resolve().parents[5] / "plugins" / "dalton" / "skills"
)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)

# Orchestration skills that launch a workflow require run-workflow, so loading
# them unlocks RunWorkflow too (SkillsMiddleware unlocks required skills' tools)
# and a user disable of run-workflow alone cannot strand their RunWorkflow call.
_RW = "run-workflow"

# (skill_name, command, requires)
_DALTON_SKILL_SPECS: tuple[tuple[str, str | None, tuple[str, ...]], ...] = (
    ("dalton-conventions", None, ("research-conventions",)),
    ("dalton-init-company", "dalton-init-company", ("dalton-conventions", "research-conventions")),
    ("dalton-knowledge", None, ("dalton-conventions", "research-conventions")),
    ("dalton-publish", "dalton-publish", ("dalton-conventions", "research-conventions")),
    ("dalton-agenda", None, ("dalton-conventions", "research-conventions")),
    ("dalton-research-director", "dalton-director", ("dalton-conventions", "research-conventions")),
    ("dalton-financial-lenses", "dalton-financial-lenses", ("dalton-conventions", "research-conventions")),
    ("dalton-company-background", "dalton-company-background", ("dalton-conventions", "research-conventions")),
    ("dalton-bullbear", "bullbear", ("dalton-conventions", "research-conventions", _RW)),
    ("dalton-initial-coverage", "initial-coverage", ("dalton-conventions", "research-conventions", _RW)),
    ("dalton-full-coverage", "full-coverage", ("dalton-conventions", "research-conventions", _RW)),
    ("dalton-earnings-calendar", "dalton-earnings-calendar", ("dalton-conventions", "research-conventions")),
    ("dalton-earnings-preview", "dalton-earnings-preview", ("dalton-conventions", "research-conventions")),
    ("dalton-earnings-flash", "dalton-earnings-flash", ("dalton-conventions", "research-conventions")),
    ("dalton-earnings-deep", "dalton-earnings-deep", ("dalton-conventions", "research-conventions", _RW)),
    ("dalton-stock-move", "dalton-stock-move", ("dalton-conventions", "research-conventions")),
    ("dalton-thesis-delta", "dalton-thesis-delta", ("dalton-conventions", "research-conventions")),
    ("dalton-filing-review", "dalton-filing-review", ("dalton-conventions", "research-conventions")),
    ("dalton-weekly-review", "weekly-review", ("dalton-conventions", "research-conventions", _RW)),
    ("dalton-topic-research", "topic-research", ("dalton-conventions", "research-conventions", _RW)),
    ("dalton-call", "dalton-call", ("dalton-conventions", "research-conventions")),
    ("dalton-pm-qa", "pm-qa", ("dalton-conventions", "research-conventions")),
)


def read_skill_description(name: str, *, skills_dir: Path = DALTON_SKILLS_DIR) -> str:
    """Read and validate ``description`` from ``<skills_dir>/<name>/SKILL.md`` frontmatter."""
    skill_md = skills_dir / name / "SKILL.md"
    if not skill_md.is_file():
        raise FileNotFoundError(f"Missing Dalton SKILL.md for '{name}': {skill_md}")
    raw = skill_md.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(raw)
    if match is None:
        raise ValueError(f"Missing YAML frontmatter in {skill_md}")
    meta = yaml.safe_load(match.group(1))
    if not isinstance(meta, dict):
        raise ValueError(f"Invalid YAML frontmatter in {skill_md}")
    desc = meta.get("description")
    if not isinstance(desc, str) or not desc.strip():
        raise ValueError(f"Missing or empty 'description' in {skill_md}")
    return desc.strip()


def build_dalton_skill_definitions(
    skills_dir: Path = DALTON_SKILLS_DIR,
) -> dict[str, SkillDefinition]:
    """Build ``SkillDefinition`` entries for all Dalton skills from ``SKILL.md`` frontmatter."""
    definitions: dict[str, SkillDefinition] = {}
    for name, command, requires in _DALTON_SKILL_SPECS:
        definitions[name] = SkillDefinition(
            name=name,
            description=read_skill_description(name, skills_dir=skills_dir),
            skill_md_path=f"skills/{name}/SKILL.md",
            command=command,
            requires=requires,
        )
    return definitions


DALTON_SKILL_DEFINITIONS: dict[str, SkillDefinition] = build_dalton_skill_definitions()
