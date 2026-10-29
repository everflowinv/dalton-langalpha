"""Dalton built-in skill registry definitions.

Merged into ``SKILL_REGISTRY`` at the end of ``registry.py`` via
``DALTON-HOOK(skills-registry)``. Each entry's ``description`` is loaded once at
import time from ``plugins/dalton/skills/<name>/SKILL.md`` YAML frontmatter so
the registry and ``SKILL.md`` never drift.
"""

from __future__ import annotations

import os
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

# Hidden skills are activated programmatically via additional_context and stay
# out of user-facing slash listings and sandbox skill manifests.
_HIDDEN_DALTON_SKILLS: frozenset[str] = frozenset({"dalton-onboarding"})

# The transcription master switch (A1): with it off, `dalton-transcript-polish`
# hides the same way `_HIDDEN_DALTON_SKILLS` entries do — out of the skill
# manifest, the listings and the sandbox. Env-only by design: this module
# builds the registry at import time, and a storage probe there would run (and
# log config warnings) on every import; the storage half of the switch lives
# in `is_transcription_enabled` (src/dalton/services/transcriptions.py).
_TRANSCRIPTION_SWITCH_VALUES = ("1", "true", "yes", "on")


def _is_transcription_switch_on() -> bool:
    """Whether `DALTON_TRANSCRIPTION_ENABLED` is set to an on value (case-insensitive)."""
    return (
        os.getenv("DALTON_TRANSCRIPTION_ENABLED", "").strip().lower()
        in _TRANSCRIPTION_SWITCH_VALUES
    )

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
    ("dalton-transcript-polish", "transcribe", ("dalton-conventions", "research-conventions")),
    ("dalton-onboarding", None, ()),
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
            hidden=(
                name in _HIDDEN_DALTON_SKILLS
                or (
                    name == "dalton-transcript-polish"
                    and not _is_transcription_switch_on()
                )
            ),
        )
    return definitions


DALTON_SKILL_DEFINITIONS: dict[str, SkillDefinition] = build_dalton_skill_definitions()
