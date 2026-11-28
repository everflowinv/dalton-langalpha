"""The core release package strips Everflow-only skills; the registry must still load."""

import shutil
from pathlib import Path

import pytest

from ptc_agent.agent.middleware.skills.dalton_registry import (
    _DALTON_SKILL_SPECS,
    DALTON_SKILL_CAPABILITY_MAP,
    DALTON_SKILLS_DIR,
    build_dalton_skill_definitions,
)

EVERFLOW_ONLY = "dalton-ai-app-weekly-monitor"


def _copy_skills(tmp_path: Path, drop: str) -> Path:
    dst = tmp_path / "skills"
    shutil.copytree(DALTON_SKILLS_DIR, dst)
    shutil.rmtree(dst / drop)
    return dst


def test_every_registered_skill_is_defined_from_the_repo():
    defs = build_dalton_skill_definitions()
    assert set(defs) == {name for name, _, _ in _DALTON_SKILL_SPECS}


def test_a_capability_gated_skill_stripped_from_the_package_is_left_out(tmp_path):
    assert DALTON_SKILL_CAPABILITY_MAP.get(EVERFLOW_ONLY) is not None
    defs = build_dalton_skill_definitions(_copy_skills(tmp_path, EVERFLOW_ONLY))
    assert EVERFLOW_ONLY not in defs
    assert "dalton-conventions" in defs


def test_a_missing_core_skill_still_fails_loud(tmp_path):
    assert DALTON_SKILL_CAPABILITY_MAP.get("dalton-conventions") is None
    with pytest.raises(FileNotFoundError):
        build_dalton_skill_definitions(_copy_skills(tmp_path, "dalton-conventions"))
