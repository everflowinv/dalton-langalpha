"""Contract tests for the full-coverage FACTS-LEDGER script and its prompt reference.

提示词第 1 批 #1：facts-ledger.md 记载的季度序列键名必须与 facts.py 的完整性
检查一致（营业利润是 op_q1..q8），旧别名 opinc_q* 兼容读取且同一季度不重复计数。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
FACTS_SCRIPT = (
    REPO_ROOT
    / "plugins"
    / "dalton"
    / "skills"
    / "dalton-full-coverage"
    / "scripts"
    / "facts.py"
)
FACTS_LEDGER_MD = (
    REPO_ROOT
    / "plugins"
    / "dalton"
    / "skills"
    / "dalton-full-coverage"
    / "references"
    / "facts-ledger.md"
)

sys.path.insert(0, str(FACTS_SCRIPT.parent))
import facts  # noqa: E402


def _quarter_rows(prefix: str, quarters: list[str]) -> list[dict]:
    return [
        {
            "key": f"{prefix}_{q}",
            "label": f"{prefix} {q}",
            "value": "1",
            "period": q,
            "source": "10-Q",
        }
        for q in quarters
    ]


def _scalar_rows() -> list[dict]:
    return [
        {"key": k, "label": k, "value": "1", "period": "FY2025", "source": "10-K"}
        for k in facts.CORE_SCALAR_KEYS
    ]


def _annual_rows() -> list[dict]:
    return _quarter_rows("fcf", ["fy1", "fy2", "fy3"]) + _quarter_rows(
        "sbc", ["fy1", "fy2", "fy3"]
    )


class TestOpQuarterAlias:
    def test_canonical_op_q_series_counts_eight(self) -> None:
        rows = _scalar_rows() + _annual_rows() + _quarter_rows(
            "op", [f"q{i}" for i in range(1, 9)]
        )
        res = facts.check_completeness(rows)
        assert res["quarter_counts"]["op"] == 8
        assert not [m for m in res["missing_series"] if m.startswith("op_")]

    def test_legacy_opinc_q_alias_still_counts(self) -> None:
        rows = _quarter_rows("opinc", [f"q{i}" for i in range(1, 9)])
        res = facts.check_completeness(rows)
        assert res["quarter_counts"]["op"] == 8
        assert not [m for m in res["missing_series"] if m.startswith("op_")]

    def test_mixed_op_and_opinc_fills_missing_quarters(self) -> None:
        rows = _quarter_rows("op", ["q1", "q2", "q3", "q4"]) + _quarter_rows(
            "opinc", ["q5", "q6", "q7", "q8"]
        )
        res = facts.check_completeness(rows)
        assert res["quarter_counts"]["op"] == 8

    def test_same_quarter_op_and_opinc_counts_once(self) -> None:
        rows = _quarter_rows("op", [f"q{i}" for i in range(1, 9)]) + _quarter_rows(
            "opinc", [f"q{i}" for i in range(1, 9)]
        )
        res = facts.check_completeness(rows)
        assert res["quarter_counts"]["op"] == 8

    def test_alias_does_not_mask_missing_quarters(self) -> None:
        rows = _quarter_rows("opinc", ["q1", "q2", "q3"])
        res = facts.check_completeness(rows)
        assert res["quarter_counts"]["op"] == 3
        assert any(
            m.startswith("op_q1..q8 (当前 3/8 季)") for m in res["missing_series"]
        )

    def test_alias_only_applies_to_op_prefix(self) -> None:
        # revinc_q1 之类不存在别名：仍按 0 计，避免前缀误匹配。
        rows = _quarter_rows("revinc", [f"q{i}" for i in range(1, 9)])
        res = facts.check_completeness(rows)
        assert res["quarter_counts"]["rev"] == 0


class TestLedgerDocMatchesChecker:
    def test_documented_quarter_prefixes_equal_checker_prefixes(self) -> None:
        """facts-ledger.md 第 4 节记载的序列键名就是检查器认可的键名。"""
        text = FACTS_LEDGER_MD.read_text(encoding="utf-8")
        documented = set(re.findall(r"`(\w+)_q1` 至 `\1_q8`", text))
        assert documented == set(facts.QUARTER_PREFIXES), (
            f"facts-ledger.md 与 facts.py 的季度序列键名不一致: {documented}"
        )

    def test_doc_no_longer_documents_opinc(self) -> None:
        text = FACTS_LEDGER_MD.read_text(encoding="utf-8")
        assert "opinc_q" not in text
