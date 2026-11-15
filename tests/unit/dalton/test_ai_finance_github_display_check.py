"""The GitHub metric rule reads each item's stats parenthesis, not the project description.

Regression: the 2026-11-15 daily was rejected because a repo description said
"Playwright 评分器" (a grader tool), which the old section-wide match took for a score.
"""
from pathlib import Path

from scripts.dalton.ai_finance import ai_finance_daily_validate as validate

RULE = "GitHub display must show total stars and completed-day gain only; hide percentages, multi-day gains and scores"
REPORT = Path(__file__).parent / "fixtures" / "ai_finance_report_20261115.md"


def _violations(tmp_path: Path, text: str) -> list[str]:
    path = tmp_path / "report.md"
    path.write_text(text, encoding="utf-8")
    found: list[str] = []
    validate.validate_markdown(path, found)
    return found


def test_description_words_do_not_trip_the_metric_rule(tmp_path: Path) -> None:
    text = REPORT.read_text(encoding="utf-8")
    assert "评分器" in text
    assert RULE not in _violations(tmp_path, text)


def test_metrics_inside_the_stats_parenthesis_are_rejected(tmp_path: Path) -> None:
    text = REPORT.read_text(encoding="utf-8").replace(
        "（总 stars 16,539；日增 +4,147）", "（总 stars 16,539；日增 +4,147；增速 25%）", 1
    )
    assert RULE in _violations(tmp_path, text)


def test_momentum_score_wording_is_rejected_anywhere(tmp_path: Path) -> None:
    text = REPORT.read_text(encoding="utf-8").replace("Playwright 评分器", "Playwright 评分器，动量评分 9.1", 1)
    assert RULE in _violations(tmp_path, text)


def test_metrics_outside_the_parenthesis_before_the_description_are_rejected(tmp_path: Path) -> None:
    text = REPORT.read_text(encoding="utf-8").replace(
        "（总 stars 16,539；日增 +4,147）", "（总 stars 16,539；日增 +4,147）7 日新增 +700", 1
    )
    assert RULE in _violations(tmp_path, text)


def test_numeric_multi_day_gain_in_the_description_is_rejected(tmp_path: Path) -> None:
    text = REPORT.read_text(encoding="utf-8").replace(
        "Playwright 评分器", "Playwright 评分器，过去 7 日累计新增 12,345", 1
    )
    assert RULE in _violations(tmp_path, text)


def test_a_date_and_version_in_the_description_are_not_a_multi_day_gain(tmp_path: Path) -> None:
    text = REPORT.read_text(encoding="utf-8").replace(
        "Playwright 评分器", "11 月 7 日发布的 Playwright 评分器 v2", 1
    )
    assert RULE not in _violations(tmp_path, text)
    text = REPORT.read_text(encoding="utf-8").replace(
        "Playwright 评分器", "Playwright 评分器，11 月 7 日新增 3 项检查，支持 GPT-5", 1
    )
    assert RULE not in _violations(tmp_path, text)


def test_labelled_slope_and_score_values_in_the_description_are_rejected(tmp_path: Path) -> None:
    for extra in ("增星斜率：1.2", "评分：9.1", "评分 8", "综合评分 9.1", "7 日 +700 stars", "增星增速：1.5 倍"):
        text = REPORT.read_text(encoding="utf-8").replace("Playwright 评分器", f"Playwright 评分器；{extra}", 1)
        assert RULE in _violations(tmp_path, text), extra
