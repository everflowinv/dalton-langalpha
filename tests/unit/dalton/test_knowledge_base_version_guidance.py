"""Lock the base_version guidance to knowledge_read's latest_version_no.

提示词第 1 批 #3：`base_version` 的唯一读取来源是 `knowledge_read` 返回的
`latest_version_no`（无记录时传 null）。`data/<doc>.json` 里没有版本号，
docstring 与 version_conflict 的补救文字都不再让模型去读它。
"""

from __future__ import annotations

import re
from pathlib import Path

from src.dalton.tools.knowledge_tools import create_knowledge_tools

SERVICE_PY = (
    Path(__file__).resolve().parents[3] / "src" / "dalton" / "knowledge" / "service.py"
)
SKILL_MD = (
    Path(__file__).resolve().parents[3]
    / "plugins"
    / "dalton"
    / "skills"
    / "dalton-knowledge"
    / "SKILL.md"
)


def test_knowledge_update_docstring_points_to_knowledge_read() -> None:
    tools = {t.name: t for t in create_knowledge_tools()}
    desc = tools["knowledge_update"].description
    assert "knowledge_read(doc=...)" in desc
    assert "latest_version_no" in desc
    # 不再指引模型去 data/<doc>.json 里取版本（那里没有版本号）。
    assert "Read data/<doc>.json first" not in desc


def test_version_conflict_remedy_points_to_knowledge_read() -> None:
    src = SERVICE_PY.read_text(encoding="utf-8")
    assert "Re-read data/" not in src
    remedies = re.findall(
        r'but latest version in DB is v\{(?:latest_no|curr_no)\}\. (.*?)base_version\."',
        src,
        re.S,
    )
    assert len(remedies) == 2
    for remedy in remedies:
        assert "Call knowledge_read(doc=" in remedy
        assert "latest_version_no" in remedy


def test_skill_md_takes_base_version_from_knowledge_read() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "取返回的 `latest_version_no` 作为 `base_version`" in text
    assert "调用前先读 `data/<doc>.json`" not in text
