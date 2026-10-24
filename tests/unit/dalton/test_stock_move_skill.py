"""Verify step ordering in ``dalton-stock-move/SKILL.md``."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_PATH = REPO_ROOT / "plugins/dalton/skills/dalton-stock-move/SKILL.md"


def test_stock_move_skill_searches_news_and_x_first_with_today_and_yesterday() -> None:
    """Step 1 of ``dalton-stock-move/SKILL.md`` must search latest news (``web_search``)
    and X (``x_search``) bounded to today and yesterday before fundamentals, sell-side,
    and peer comparison.
    """
    md_text = SKILL_PATH.read_text(encoding="utf-8")

    step1_match = re.search(
        r"### 第 1 步：(.*?)(?=### 第 2 步：)", md_text, flags=re.DOTALL
    )
    assert step1_match is not None, "Step 1 heading not found in SKILL.md"
    step1_text = step1_match.group(1)

    assert "今天发生了什么" in step1_text
    assert "web_search(" in step1_text
    assert "start_date=YESTERDAY" in step1_text
    assert "end_date=TODAY" in step1_text
    assert "x_search(" in step1_text
    assert "from_date=YESTERDAY" in step1_text
    assert "to_date=TODAY" in step1_text

    # Ordering across the workflow: web_search & x_search in Step 1 must appear before
    # fundamentals (dalton_sec.list_filings), sell-side (alphaengine), and peer quant (move_quant.py).
    workflow_start = md_text.index("## 九步执行流程")
    wf_text = md_text[workflow_start:]

    pos_web = wf_text.index("web_search(")
    pos_x = wf_text.index("x_search(")
    pos_filings = wf_text.index("dalton_sec.list_filings(")
    pos_sellside = wf_text.index('route="alphaengine"')
    pos_peers = wf_text.index("move_quant.py analyze")

    assert pos_web < pos_filings < pos_sellside < pos_peers
    assert pos_x < pos_filings < pos_sellside < pos_peers
