"""Dalton built-in skill registry definitions.

Merged into ``SKILL_REGISTRY`` at the end of ``registry.py`` via
``DALTON-HOOK(skills-registry)``. Each entry's ``description`` is loaded once at
import time from ``plugins/dalton/skills/<name>/SKILL.md`` YAML frontmatter so
the registry and ``SKILL.md`` never drift.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
import os
import re
from pathlib import Path
from types import MappingProxyType

import yaml

from src.dalton.capabilities import (
    CORE_CAPABILITY,
    CapabilityId,
    effective_capabilities,
    has_capability,
)
from src.dalton.profile import is_core_profile

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

# Upstream task skills a Dalton skill already does inside the Dalton loop
# (knowledge layer, five-word verdict, language review, completion contract).
# Left in the registry, the model can route a covered company's task to the
# upstream one and the loop breaks: no verdict, no thesis update, output in
# DOCX. Building blocks Dalton workflows call by path (research-conventions,
# check-model, model-update, 3-statements, dcf-model, comps-analysis) stay.
SUPERSEDED_UPSTREAM_SKILLS: frozenset[str] = frozenset({
    "thesis-tracker",        # dalton-thesis-delta + dalton-knowledge + dalton-agenda
    "earnings-analysis",     # dalton-earnings-flash + dalton-earnings-deep
    "earnings-preview",      # dalton-earnings-preview
    "morning-note",          # dalton-thesis-delta + dalton-stock-move + weekly push
    "catalyst-calendar",     # dalton-earnings-calendar + agenda watch events
    "initiating-coverage",   # dalton-initial-coverage + dalton-full-coverage
    "company-profile",       # dalton-company-background
    "trade-pitch",           # dalton-call + dalton-bullbear
    "sector-overview",       # dalton-init-sector + dalton-topic-research
})

# (skill_name, command, requires)
_DALTON_SKILL_SPECS: tuple[tuple[str, str | None, tuple[str, ...]], ...] = (
    ("dalton-conventions", None, ("research-conventions",)),
    ("dalton-init-company", "dalton-init-company", ("dalton-conventions", "research-conventions")),
    ("dalton-init-sector", None, ("dalton-conventions", "research-conventions", "dalton-knowledge")),
    ("dalton-knowledge", None, ("dalton-conventions", "research-conventions")),
    ("dalton-publish", "dalton-publish", ("dalton-conventions", "research-conventions")),
    ("dalton-agenda", None, ("dalton-conventions", "research-conventions")),
    ("dalton-research-director", "dalton-director", ("dalton-conventions", "research-conventions")),
    ("dalton-financial-lenses", "dalton-financial-lenses", ("dalton-conventions", "research-conventions")),
    ("dalton-model", "dalton-model", ("dalton-conventions", "research-conventions")),
    ("dalton-company-background", "dalton-company-background", ("dalton-conventions", "research-conventions")),
    ("dalton-bullbear", "bullbear", ("dalton-conventions", "research-conventions", _RW)),
    ("dalton-initial-coverage", "initial-coverage", ("dalton-conventions", "research-conventions", _RW)),
    ("dalton-full-coverage", "full-coverage", ("dalton-conventions", "research-conventions", "dalton-model", _RW)),
    ("dalton-earnings-calendar", "dalton-earnings-calendar", ("dalton-conventions", "research-conventions")),
    ("dalton-earnings-preview", "dalton-earnings-preview", ("dalton-conventions", "research-conventions")),
    ("dalton-earnings-flash", "dalton-earnings-flash", ("dalton-conventions", "research-conventions")),
    ("dalton-earnings-deep", "dalton-earnings-deep", ("dalton-conventions", "research-conventions", "dalton-model", _RW)),
    ("dalton-stock-move", "dalton-stock-move", ("dalton-conventions", "research-conventions")),
    ("dalton-thesis-delta", "dalton-thesis-delta", ("dalton-conventions", "research-conventions")),
    ("dalton-bullbear-tracker", None, ("dalton-thesis-delta", "dalton-conventions", "research-conventions")),
    ("dalton-sector-delta", None, ("dalton-conventions", "research-conventions", "dalton-knowledge", "dalton-agenda")),
    ("dalton-filing-review", "dalton-filing-review", ("dalton-conventions", "research-conventions")),
    ("dalton-weekly-review", "weekly-review", ("dalton-conventions", "research-conventions", _RW)),
    ("dalton-topic-research", "topic-research", ("dalton-conventions", "research-conventions", _RW)),
    ("dalton-call", "dalton-call", ("dalton-conventions", "research-conventions")),
    ("dalton-pm-qa", "pm-qa", ("dalton-conventions", "research-conventions")),
    ("dalton-transcript-polish", "transcribe", ("dalton-conventions", "research-conventions")),
    ("dalton-ai-finance-daily", None, ()),
    ("dalton-market-digest", None, ()),
    ("dalton-us-weekly-movers", None, ("dalton-bullbear-tracker",)),
    ("dalton-x-accounts-daily", None, ("dalton-bullbear-tracker",)),
    ("dalton-humanoid-robot-weekly", None, ("dalton-bullbear-tracker",)),
    ("dalton-weekly-trend-impact-scan", None, ()),
    ("dalton-ct-semi-sentiment", None, ()),
    ("dalton-china-consumer-weekly", None, ()),
    ("dalton-global-consumer-weekly", None, ()),
    ("dalton-daily-filing-alert", None, ("dalton-bullbear-tracker",)),
    ("dalton-tob-industry-weekly", None, ()),
    ("dalton-vip-news-alert", None, ("dalton-bullbear-tracker",)),
    ("dalton-mark-weekly-newenergy", None, ()),
    ("dalton-tmeet-weekly-minutes", None, ()),
    ("dalton-ai-app-weekly-monitor", None, ()),
    ("dalton-richard-earnings", None, ()),
    ("dalton-richard-coverage-weekly", None, ()),
    ("dalton-13f-tracker", None, ()),
    ("dalton-option-put-skew", None, ()),
    ("dalton-short-screen", None, ()),
    ("dalton-bdc-analyst", None, ()),
    ("dalton-wiki-auto-ingest", None, ("dalton-conventions", "research-conventions")),
    ("dalton-podcast-weekly", None, ()),  # DALTON-HOOK(skills-registry)
    ("dalton-language", None, ()),
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


# Explicit deployment capability binding for every built-in Dalton skill (§3.4 #1).
# CORE_CAPABILITY (None) = available in Standalone Core & Everflow alike.
# A CapabilityId or tuple[CapabilityId, ...] = requires all named capabilities.
DALTON_SKILL_CAPABILITY_MAP: Mapping[
    str, CapabilityId | tuple[CapabilityId, ...] | None
] = MappingProxyType(
    {
        # --- Core investment research skeleton (30 skills) ---
        "dalton-conventions": CORE_CAPABILITY,
        "dalton-init-company": CORE_CAPABILITY,
        "dalton-init-sector": CORE_CAPABILITY,
        "dalton-knowledge": CORE_CAPABILITY,
        "dalton-publish": CORE_CAPABILITY,
        "dalton-agenda": CORE_CAPABILITY,
        "dalton-research-director": CORE_CAPABILITY,
        "dalton-financial-lenses": CORE_CAPABILITY,
        "dalton-model": CORE_CAPABILITY,
        "dalton-company-background": CORE_CAPABILITY,
        "dalton-bullbear": CORE_CAPABILITY,
        "dalton-initial-coverage": CORE_CAPABILITY,
        "dalton-full-coverage": CORE_CAPABILITY,
        "dalton-earnings-calendar": CORE_CAPABILITY,
        "dalton-earnings-preview": CORE_CAPABILITY,
        "dalton-earnings-flash": CORE_CAPABILITY,
        "dalton-earnings-deep": CORE_CAPABILITY,
        "dalton-stock-move": CORE_CAPABILITY,
        "dalton-thesis-delta": CORE_CAPABILITY,
        "dalton-sector-delta": CORE_CAPABILITY,
        "dalton-filing-review": CORE_CAPABILITY,
        "dalton-weekly-review": CORE_CAPABILITY,
        "dalton-topic-research": CORE_CAPABILITY,
        "dalton-call": CORE_CAPABILITY,
        "dalton-pm-qa": CORE_CAPABILITY,
        "dalton-option-put-skew": CORE_CAPABILITY,
        "dalton-short-screen": CORE_CAPABILITY,
        "dalton-bdc-analyst": CORE_CAPABILITY,
        "dalton-language": CORE_CAPABILITY,
        "dalton-onboarding": CORE_CAPABILITY,
        # --- Optional / Everflow-coupled skills (22 skills) ---
        "dalton-transcript-polish": CapabilityId.TRANSCRIPTION,
        "dalton-bullbear-tracker": CapabilityId.FUND_AUTOMATIONS,
        "dalton-ai-finance-daily": CapabilityId.FUND_AUTOMATIONS,
        "dalton-market-digest": CapabilityId.FUND_AUTOMATIONS,
        "dalton-us-weekly-movers": CapabilityId.FUND_AUTOMATIONS,
        "dalton-x-accounts-daily": CapabilityId.FUND_AUTOMATIONS,
        "dalton-humanoid-robot-weekly": CapabilityId.FUND_AUTOMATIONS,
        "dalton-weekly-trend-impact-scan": CapabilityId.FUND_AUTOMATIONS,
        "dalton-ct-semi-sentiment": CapabilityId.FUND_AUTOMATIONS,
        "dalton-china-consumer-weekly": CapabilityId.FUND_AUTOMATIONS,
        "dalton-global-consumer-weekly": CapabilityId.FUND_AUTOMATIONS,
        "dalton-daily-filing-alert": CapabilityId.FUND_AUTOMATIONS,
        "dalton-tob-industry-weekly": CapabilityId.FUND_AUTOMATIONS,
        "dalton-vip-news-alert": CapabilityId.FUND_AUTOMATIONS,
        "dalton-mark-weekly-newenergy": CapabilityId.FUND_AUTOMATIONS,
        "dalton-tmeet-weekly-minutes": (
            CapabilityId.FUND_AUTOMATIONS,
            CapabilityId.FEISHU_DOCS,
        ),
        "dalton-ai-app-weekly-monitor": CapabilityId.FUND_AUTOMATIONS,
        "dalton-richard-earnings": CapabilityId.FUND_AUTOMATIONS,
        "dalton-richard-coverage-weekly": CapabilityId.FUND_AUTOMATIONS,
        "dalton-13f-tracker": CapabilityId.FUND_AUTOMATIONS,
        "dalton-wiki-auto-ingest": (
            CapabilityId.COMPANY_WIKI,
            CapabilityId.CHANNEL_DISCORD,
        ),
        "dalton-podcast-weekly": CapabilityId.FUND_AUTOMATIONS,
    }
)


def is_dalton_skill_enabled(
    name: str,
    *,
    capabilities: frozenset[CapabilityId] | None = None,
) -> bool:
    """Return True iff Dalton skill *name* is permitted by the active capabilities.

    Evaluated dynamically on every lookup (no import-time freeze) so runtime
    profile/capability changes take effect immediately across registry accessors.
    """
    req = DALTON_SKILL_CAPABILITY_MAP.get(name)
    if req is None:
        return True
    active = effective_capabilities() if capabilities is None else capabilities
    if name == "dalton-transcript-polish" and not is_core_profile():
        # Preserve Everflow's existing hidden-but-registered parity for
        # dalton-transcript-polish while still gating it off when the host
        # bridge is disabled.
        return CapabilityId.RESEARCHDATA_BRIDGE in active
    required = req if isinstance(req, tuple) else (req,)
    return all(cap in active for cap in required)


def is_dalton_skill_hidden(name: str, default_hidden: bool = False) -> bool:
    """Resolve dynamic ``hidden`` flag for Dalton skills."""
    if name == "dalton-transcript-polish":
        if is_core_profile():
            return not has_capability(CapabilityId.TRANSCRIPTION)
        return not (
            _is_transcription_switch_on() or has_capability(CapabilityId.TRANSCRIPTION)
        )
    return default_hidden


def disabled_dalton_skills(
    *,
    capabilities: frozenset[CapabilityId] | None = None,
) -> frozenset[str]:
    """Return built-in Dalton skill names currently disabled by capabilities."""
    active = effective_capabilities() if capabilities is None else capabilities
    return frozenset(
        name
        for name in DALTON_SKILL_CAPABILITY_MAP
        if not is_dalton_skill_enabled(name, capabilities=active)
    )


def _skill_capability_gate(name: str) -> Callable[[], bool] | None:
    if DALTON_SKILL_CAPABILITY_MAP.get(name) is None:
        return None
    return partial(is_dalton_skill_enabled, name)


def build_dalton_skill_definitions(
    skills_dir: Path = DALTON_SKILLS_DIR,
) -> dict[str, SkillDefinition]:
    """Build ``SkillDefinition`` entries for all Dalton skills from ``SKILL.md`` frontmatter."""
    definitions: dict[str, SkillDefinition] = {}
    for name, command, requires in _DALTON_SKILL_SPECS:
        if (
            is_core_profile()
            and DALTON_SKILL_CAPABILITY_MAP.get(name) is not None
            and not (skills_dir / name / "SKILL.md").is_file()
        ):
            # The core release package strips Everflow-only skills
            # (scripts/standalone/package_release.sh DENY_PATHS); only under the
            # core profile is a stripped capability-gated skill omitted. Core
            # skills and Everflow deployments still fail loud on missing SKILL.md.
            continue
        definitions[name] = SkillDefinition(
            name=name,
            description=read_skill_description(name, skills_dir=skills_dir),
            skill_md_path=f"skills/{name}/SKILL.md",
            command=command,
            requires=requires,
            hidden=is_dalton_skill_hidden(name, name in _HIDDEN_DALTON_SKILLS),
            system_gate=_skill_capability_gate(name),
        )
    return definitions


DALTON_SKILL_DEFINITIONS: dict[str, SkillDefinition] = build_dalton_skill_definitions()
