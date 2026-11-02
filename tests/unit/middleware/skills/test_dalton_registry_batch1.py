"""The three late-registered Dalton skills are platform-tier, not user-supplied.

提示词第 1 批 #4：dalton-sector-delta / dalton-init-sector / dalton-language
补登进 `_DALTON_SKILL_SPECS` 后，必须以平台技能身份出现——描述取自各自
SKILL.md frontmatter，requires 按各自 SKILL.md 实际引用的技能填写，且不再
经沙箱发现进入"Your skills（用户提供的、不可信）"分组。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from ptc_agent.agent.middleware.skills.discovery import SkillMetadata
from ptc_agent.agent.middleware.skills.middleware import SkillsMiddleware
from ptc_agent.agent.middleware.skills.registry import (
    SKILL_REGISTRY,
    get_skill_registry,
    list_skills,
    skills_outside_sandbox,
)

THREE_SKILLS = ("dalton-sector-delta", "dalton-init-sector", "dalton-language")

EXPECTED_REQUIRES = {
    # 行认知按 dalton-knowledge 的读序读、收尾按 dalton-agenda 的三步（SKILL.md 首段）。
    "dalton-sector-delta": (
        "dalton-conventions",
        "research-conventions",
        "dalton-knowledge",
        "dalton-agenda",
    ),
    # Fund methodology + 知识层读写规范（dalton-knowledge 行业一节）。
    "dalton-init-sector": ("dalton-conventions", "research-conventions", "dalton-knowledge"),
    # 语言审查规则自成一体，不引用其他技能。
    "dalton-language": (),
}


@pytest.mark.parametrize("name", THREE_SKILLS)
def test_registered_as_platform_skills(name: str) -> None:
    assert name in SKILL_REGISTRY
    skill = SKILL_REGISTRY[name]
    assert skill.origin == "platform"
    assert skill.hidden is False
    assert skill.command is None
    assert skill.skill_md_path == f"skills/{name}/SKILL.md"
    assert skill.requires == EXPECTED_REQUIRES[name]
    assert name in get_skill_registry()
    assert name in {entry["name"] for entry in list_skills()}
    # 非隐藏技能随沙箱同步（不属于 skills_outside_sandbox）。
    assert name not in skills_outside_sandbox()


def test_requires_are_registered_names() -> None:
    for name in THREE_SKILLS:
        for dep in SKILL_REGISTRY[name].requires:
            assert dep in SKILL_REGISTRY, f"{name} requires unknown skill {dep}"


@pytest.mark.asyncio
async def test_filesystem_discovery_no_longer_claims_the_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before registration these three surfaced as discovered user skills; now
    the registry owns them, so the discovery filter drops them: they never
    reach state, and the manifest lists them in the platform group while a
    genuinely unregistered skill lands in the untrusted "Your skills" group."""
    from ptc_agent.agent.middleware.skills import middleware as mw_mod

    def _meta(name: str) -> SkillMetadata:
        return SkillMetadata(
            path=f"/sandbox/.agents/skills/{name}/SKILL.md",
            name=name,
            description=f"discovered copy of {name}",
            license=None,
            compatibility=None,
            metadata={},
            allowed_tools=[],
        )

    on_disk = [_meta(n) for n in THREE_SKILLS] + [_meta("my-custom-skill")]

    async def _fake_discover(backend, source_path, known_skills):
        return list(on_disk)

    monkeypatch.setattr(mw_mod, "adiscover_skills", _fake_discover)
    mw = SkillsMiddleware(backend=object(), sources=["/sandbox/.agents/skills"])
    state = {"messages": [HumanMessage(content="hi", id="u1")], "loaded_skills": []}
    out = await mw.abefore_agent(state, None, config={"configurable": {}})

    # The registry owns the three: discovery drops them; the unknown skill stays.
    assert [s["name"] for s in out["discovered_skills"]] == ["my-custom-skill"]

    manifest = mw.build_manifest({"discovered_skills": out["discovered_skills"]})
    assert manifest is not None
    platform_part, your_part = (
        manifest.split("### Your skills", 1) if "### Your skills" in manifest else (manifest, "")
    )
    for name in THREE_SKILLS:
        assert f"**{name}**" in platform_part
        assert name not in your_part
    assert "**my-custom-skill**" in your_part


def test_manifest_lists_three_as_platform_entries() -> None:
    mw = SkillsMiddleware()
    manifest = mw.build_manifest({"discovered_skills": []})
    assert manifest is not None
    platform_part = manifest.split("### Your skills", 1)[0]
    for name in THREE_SKILLS:
        assert f"**{name}**" in platform_part


def test_registry_spec_tuple_matches_definitions() -> None:
    from ptc_agent.agent.middleware.skills.dalton_registry import (
        _DALTON_SKILL_SPECS,
        build_dalton_skill_definitions,
    )

    names = {spec[0] for spec in _DALTON_SKILL_SPECS}
    assert set(THREE_SKILLS) <= names
    for name, _command, _requires in (s for s in _DALTON_SKILL_SPECS if s[0] in THREE_SKILLS):
        defn = build_dalton_skill_definitions()[name]
        assert defn.skill_md_path == f"skills/{name}/SKILL.md"
