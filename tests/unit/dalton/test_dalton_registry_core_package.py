"""The core release package strips Everflow-only skills; the registry must still load.

Skipping missing capability-gated ``SKILL.md`` files is active ONLY under the
``core`` deployment profile. Under ``everflow`` (the default), any missing
``SKILL.md`` — including capability-gated skills — must still fail loud.
"""

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


def _set_profile(monkeypatch: pytest.MonkeyPatch, profile: str) -> None:
    if profile == "core":
        monkeypatch.setenv("APP_ENV", "core")
        monkeypatch.setenv("DALTON_PROFILE", "core")
    else:
        monkeypatch.setenv("APP_ENV", "dalton")
        monkeypatch.delenv("DALTON_PROFILE", raising=False)


def test_every_registered_skill_is_defined_from_the_repo():
    defs = build_dalton_skill_definitions()
    assert set(defs) == {name for name, _, _ in _DALTON_SKILL_SPECS}


def test_core_profile_omits_stripped_capability_gated_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _set_profile(monkeypatch, "core")
    assert DALTON_SKILL_CAPABILITY_MAP.get(EVERFLOW_ONLY) is not None
    defs = build_dalton_skill_definitions(_copy_skills(tmp_path, EVERFLOW_ONLY))
    assert EVERFLOW_ONLY not in defs
    assert "dalton-conventions" in defs


def test_everflow_profile_still_fails_loud_on_missing_capability_gated_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _set_profile(monkeypatch, "everflow")
    assert DALTON_SKILL_CAPABILITY_MAP.get(EVERFLOW_ONLY) is not None
    with pytest.raises(FileNotFoundError, match=EVERFLOW_ONLY):
        build_dalton_skill_definitions(_copy_skills(tmp_path, EVERFLOW_ONLY))


@pytest.mark.parametrize("profile", ["core", "everflow"])
def test_a_missing_core_skill_still_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
):
    _set_profile(monkeypatch, profile)
    assert DALTON_SKILL_CAPABILITY_MAP.get("dalton-conventions") is None
    with pytest.raises(FileNotFoundError, match="dalton-conventions"):
        build_dalton_skill_definitions(_copy_skills(tmp_path, "dalton-conventions"))
