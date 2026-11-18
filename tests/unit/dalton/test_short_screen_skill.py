"""Offline unit tests for dalton-short-screen skill."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_NAME = "dalton-short-screen"
SKILL_DIR = REPO_ROOT / "plugins" / "dalton" / "skills" / SKILL_NAME
TESTS_DIR = REPO_ROOT / "tests" / "unit" / "dalton"


def test_registry_frontmatter_paths_and_layout() -> None:
    from ptc_agent.agent.middleware.skills.dalton_registry import (
        DALTON_SKILL_DEFINITIONS,
        _DALTON_SKILL_SPECS,
        read_skill_description,
    )
    from ptc_agent.agent.middleware.skills.registry import SKILL_REGISTRY

    assert (SKILL_NAME, None, ()) in _DALTON_SKILL_SPECS
    assert SKILL_NAME in DALTON_SKILL_DEFINITIONS
    assert SKILL_NAME in SKILL_REGISTRY

    skill_md_path = SKILL_DIR / "SKILL.md"
    skill_md = skill_md_path.read_text(encoding="utf-8")
    assert skill_md.startswith("---\n")
    end_idx = skill_md.find("\n---\n", 4)
    assert end_idx != -1
    meta = yaml.safe_load(skill_md[4:end_idx])
    assert set(meta) == {"name", "description"}
    assert meta["name"] == SKILL_NAME
    desc = meta["description"].strip()
    assert 0 < len(desc) <= 120
    assert "触发词" not in desc
    assert desc == read_skill_description(SKILL_NAME)

    # Pure prompt + references skill: no scripts, run.sh, or self-test
    assert not (SKILL_DIR / "scripts").exists()
    assert not (SKILL_DIR / "run.sh").exists()
    assert "self-test" not in skill_md
    assert (SKILL_DIR / "references" / "four-dimensions.md").is_file()
    assert (SKILL_DIR / "references" / "output-template.md").is_file()

    dalton_paths = (REPO_ROOT / "scripts" / "dalton" / "dalton_paths.txt").read_text(encoding="utf-8")
    assert f"plugins/dalton/skills/{SKILL_NAME}/" in dalton_paths
    assert "tests/unit/dalton/test_short_screen_skill.py" in dalton_paths


def test_skill_tool_calls_and_refs_valid() -> None:
    sys.path.insert(0, str(TESTS_DIR))
    try:
        import test_skill_tool_calls as stc
    finally:
        sys.path.pop(0)

    registry = stc.build_tool_signature_registry()
    all_calls: list[dict] = []
    for md_path in sorted(SKILL_DIR.rglob("*.md")):
        rel_path = md_path.relative_to(stc.SKILLS_ROOT).as_posix()
        text = md_path.read_text(encoding="utf-8")
        calls = stc.extract_tool_calls_from_markdown(text, registry)
        errors = stc.validate_extracted_calls(rel_path, calls, registry)
        assert not errors, f"Invalid tool calls in {rel_path}: {errors}"
        all_calls.extend(calls)

    tool_names = {c["tool_name"] for c in all_calls}
    assert "researchdata.finance" in tool_names
    assert "researchdata.community" in tool_names
    assert "researchdata.research" in tool_names
    assert "get_company_overview" in tool_names
    assert "web_search" in tool_names
    assert "WebFetch" in tool_names
    assert "feishu_create_doc" in tool_names


def test_preserves_required_short_screen_rules() -> None:
    skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    refs_md = (SKILL_DIR / "references" / "four-dimensions.md").read_text(encoding="utf-8")
    tmpl_md = (SKILL_DIR / "references" / "output-template.md").read_text(encoding="utf-8")
    combined = "\n".join([skill_md, refs_md, tmpl_md])

    assert "20-F" in combined and "Item 6.B" in combined
    assert 'item="6"' in combined and 'item="6B"' not in combined
    assert "Reddit 未覆盖" in combined
    assert "URL" in combined and "每条" in combined
    assert "dalton-financial-lenses" in combined
    assert "earnings_quality.py" in combined and "netcash.py" in combined
    assert "allow_paid=True" in combined and "默认" in combined
    assert "drafts/<YYYY-MM-DD>-<TICKER>-short-screen.md" in combined
    assert "Short Interest" in combined and "Short Volume" in combined
