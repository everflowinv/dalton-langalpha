"""Verify step ordering in ``dalton-stock-move/SKILL.md``.

P-0 second batch contract: Step 0 quantifies the move, reads thesis/debate and
freezes 2–3 competing explanations; Step 1 locks the event window (history
questions never search today) and checks primary filings before any news;
Step 2 covers news, market regime/peers and sell-side research; social media
comes last in Step 3.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_PATH = REPO_ROOT / "plugins/dalton/skills/dalton-stock-move/SKILL.md"


def _step_text(md_text: str, step: int) -> str:
    pattern = rf"### 第 {step} 步：(.*?)(?=### 第 {step + 1} 步：|### 收尾门禁)"
    match = re.search(pattern, md_text, flags=re.DOTALL)
    assert match is not None, f"Step {step} heading not found in SKILL.md"
    return match.group(1)


def test_stock_move_skill_quant_freeze_first_filings_before_news_social_last() -> None:
    """Step 0 quant decomposition + frozen competing explanations come first;
    Step 1 locks the event window (no searching today for history questions)
    and checks primary filings; social media trails news and sell-side research.
    """
    md_text = SKILL_PATH.read_text(encoding="utf-8")

    # Step 0: quant decomposition, thesis/debate read, frozen explanations.
    step0 = _step_text(md_text, 0)
    assert "move_quant.py analyze" in step0
    assert "冻结 2–3 个竞争解释" in step0
    assert "可证伪" in step0
    assert "data_ok" in step0

    # Step 1: window lock before retrieval; history questions never search today;
    # primary filings checked in this step.
    step1 = _step_text(md_text, 1)
    assert "先锁窗口再检索" in step1
    assert "禁止先搜今天" in step1
    assert "dalton_sec.list_filings(" in step1

    # Step 2: news bounded to the event window, then market regime / peers and
    # sell-side research.
    step2 = _step_text(md_text, 2)
    assert "web_search(" in step2
    assert "start_date=NEWS_START" in step2
    assert "end_date=NEWS_END" in step2
    assert 'route="alphaengine"' in step2

    # Step 3: social media last, flagged as unverified.
    step3 = _step_text(md_text, 3)
    assert "x_search(" in step3
    assert "from_date=NEWS_START" in step3
    assert "to_date=NEWS_END" in step3
    assert "未经证实" in step3

    # Ordering across the workflow section: quant + window lock before filings;
    # filings before news; news before market-regime/peers before sell-side;
    # social media after all of them.
    wf_start = md_text.index("## 执行流程")
    wf_text = md_text[wf_start:]

    pos_quant = wf_text.index("move_quant.py analyze")
    pos_lock = wf_text.index("禁止先搜今天")
    pos_filings = wf_text.index("dalton_sec.list_filings(")
    pos_news = wf_text.index("start_date=NEWS_START")
    pos_regime = wf_text.index('macro(route="market-regime"')
    pos_sellside = wf_text.index('route="alphaengine"')
    pos_x = wf_text.index("x_search(")
    pos_xueqiu = wf_text.index("xueqiu")

    assert pos_quant < pos_lock < pos_filings
    assert pos_filings < pos_news
    assert pos_news < pos_regime < pos_sellside
    assert pos_sellside < pos_x
    assert pos_x < pos_xueqiu
