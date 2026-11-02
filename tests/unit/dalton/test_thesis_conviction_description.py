"""Lock the conviction semantics in thesis.schema.json to FINAL_SPEC §2.1–2.2.

提示词第 1 批 #5：conviction 表示判断的稳健程度；none 只用于 stance=no_view，
不再鼓励"预期差不足写 none"这类保守结论。
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "dalton"
    / "knowledge"
    / "schemas"
    / "thesis.schema.json"
)


def _conviction() -> dict:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return schema["$defs"]["houseView"]["properties"]["conviction"]


def test_conviction_enum_unchanged() -> None:
    assert _conviction()["enum"] == ["high", "medium", "low", "none"]


def test_description_states_robustness_semantics() -> None:
    desc = _conviction()["description"]
    assert "判断的稳健程度" in desc
    for level, meaning in (
        ("high", "扰动下判断仍成立"),
        ("medium", "依赖一个可识别、可监控的关键条件"),
        ("low", "依赖窄假设"),
    ):
        assert level in desc and meaning in desc
    # none 只属于 no_view；"预期差不够"不再是写 none 的理由。
    assert "只用于 stance=no_view" in desc
    assert "不得用来表达" in desc
    assert "没有足够预期差时写 none" not in desc
