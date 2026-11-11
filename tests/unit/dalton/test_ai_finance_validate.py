"""Offline validator checks for the original brief shape and signal sets."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.dalton.ai_finance.ai_finance_daily_validate import (
    validate_github_tracking,
    validate_markdown,
    validate_records,
)

HEAD = (
    "### 今日要闻\n\n"
    "### GitHub AI 项目动量\n"
    "{github}\n"
    "### Earnings Calls：AI 的直接业务影响\n\n"
    "### 来源说明\n"
)


def _github_item(index: int, repo: str, stars: str, gain: str) -> str:
    return (
        f"{index}. **[{repo}](https://github.com/{repo})** 总 stars {stars}；日增 +{gain}\n"
        "官方完成日增星。\n"
        "仅作观察。\n"
    )


def _write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _bundle(
    tmp_path: Path,
    candidates: list[dict[str, object]],
    dispositions: list[dict[str, object]],
    signals: list[dict[str, object]],
) -> tuple[Path, Path, Path]:
    scan = tmp_path / "scan.json"
    disposition_path = tmp_path / "dispositions.json"
    signal_path = tmp_path / "signals.json"
    _write(scan, {"coverage": {"ai_candidates": len(dispositions)}, "candidates": candidates})
    _write(disposition_path, {"records": dispositions})
    _write(signal_path, {"records": signals})
    return scan, disposition_path, signal_path


def test_github_section_rejects_a_ninth_item(tmp_path: Path) -> None:
    body = "\n".join(_github_item(index, f"acme/repo{index}", "20,000", "1,000") for index in range(1, 10))
    path = tmp_path / "brief.md"
    path.write_text(HEAD.format(github=body), encoding="utf-8")
    violations: list[str] = []
    validate_markdown(path, violations)
    assert any("exceeds 8" in item for item in violations)


def test_displayed_stars_must_match_verified_counts(tmp_path: Path) -> None:
    markdown = tmp_path / "brief.md"
    candidates = tmp_path / "github.json"
    markdown.write_text(
        HEAD.format(github=_github_item(1, "acme/widget", "1,000", "30")),
        encoding="utf-8",
    )
    _write(
        candidates,
        {
            "version": 3,
            "continuing_hotspots": [],
            "candidates": [
                {
                    "full_name": "acme/widget",
                    "history_verified": True,
                    "report_eligible": True,
                    "previously_reported": False,
                    "significance": {"breakout_score": 1.0},
                    "momentum": {"daily_gain": 1000, "daily_velocity_3d": 1000.0, "gain_7d": 7000},
                    "github": {"stars": 20000},
                }
            ],
        },
    )
    violations: list[str] = []
    validate_github_tracking(markdown, candidates, violations)
    assert any("displayed stars disagree" in item and "acme/widget" in item for item in violations)


def test_candidate_and_disposition_sets_must_match(tmp_path: Path) -> None:
    scan, dispositions, signals = _bundle(
        tmp_path,
        [{"doc_id": "a"}, {"doc_id": "b"}],
        [{"doc_id": "a", "disposition": "signal"}, {"doc_id": "c", "disposition": "watchlist"}],
        [],
    )
    violations: list[str] = []
    validate_records(scan, dispositions, signals, violations)
    assert "candidate doc_ids and dispositions are not a one-to-one set" in violations

    duplicate = tmp_path / "duplicate"
    duplicate.mkdir()
    scan, dispositions, signals = _bundle(
        duplicate,
        [{"doc_id": "a"}, {"doc_id": "a"}],
        [{"doc_id": "a", "disposition": "signal"}, {"doc_id": "a", "disposition": "watchlist"}],
        [],
    )
    violations = []
    validate_records(scan, dispositions, signals, violations)
    assert "duplicate candidate doc_id" in violations

    missing = tmp_path / "missing"
    missing.mkdir()
    scan, dispositions, signals = _bundle(
        missing,
        [{"doc_id": "a"}, {"doc_id": "b"}],
        [{"doc_id": "a", "disposition": "signal"}],
        [{"source_type": "earnings", "external_id": "a", "record_key": "earnings:a"}],
    )
    violations = []
    validate_records(scan, dispositions, signals, violations)
    assert "candidate doc_ids and dispositions are not a one-to-one set" in violations


def test_signal_dispositions_are_exactly_the_earnings_records(tmp_path: Path) -> None:
    scan, dispositions, signals = _bundle(
        tmp_path,
        [{"doc_id": "a"}],
        [{"doc_id": "a", "disposition": "signal"}],
        [],
    )
    violations: list[str] = []
    validate_records(scan, dispositions, signals, violations)
    assert "earnings signal records do not match the signal disposition set" in violations

    watched = tmp_path / "watched"
    watched.mkdir()
    scan, dispositions, signals = _bundle(
        watched,
        [{"doc_id": "a"}, {"doc_id": "b"}],
        [
            {"doc_id": "a", "disposition": "signal"},
            {"doc_id": "b", "disposition": "watchlist"},
        ],
        [
            {"source_type": "earnings", "external_id": "a", "record_key": "earnings:a"},
            {"source_type": "earnings", "external_id": "b", "record_key": "earnings:b"},
        ],
    )
    violations = []
    validate_records(scan, dispositions, signals, violations)
    assert "watchlist or reject entered signal records" in violations

    illegal = tmp_path / "illegal"
    illegal.mkdir()
    scan, dispositions, signals = _bundle(
        illegal,
        [{"doc_id": "a"}],
        [{"doc_id": "a", "disposition": "archive"}],
        [],
    )
    violations = []
    validate_records(scan, dispositions, signals, violations)
    assert any("is invalid" in item for item in violations)


def test_github_signal_requires_one_include_decision(tmp_path: Path) -> None:
    def prepare(name: str, decision: str | None, displayed: bool = False) -> tuple[Path, Path, Path, Path | None]:
        root = tmp_path / name
        root.mkdir()
        scan, dispositions, signals = _bundle(root, [], [], [
            {"source_type": "github", "external_id": "acme/widget", "record_key": "github:acme/widget"}
        ])
        decisions = None
        if decision is not None:
            decisions = root / "decisions.json"
            _write(decisions, {"records": [{
                "repo": "Acme/Widget",
                "decision": decision,
                "displayed": displayed,
                "reason": "read the repository",
                "readme_url": "https://example.invalid/readme",
            }]})
        return scan, dispositions, signals, decisions

    scan, dispositions, signals, decisions = prepare("exclude", "exclude")
    violations: list[str] = []
    validate_records(scan, dispositions, signals, violations, decisions)
    assert any("lacks an include decision" in item for item in violations)

    scan, dispositions, signals, decisions = prepare("include", "include", displayed=False)
    violations = []
    validate_records(scan, dispositions, signals, violations, decisions)
    assert violations == []

    scan, dispositions, signals, _decisions = prepare("none", None)
    violations = []
    validate_records(scan, dispositions, signals, violations, None)
    assert "GitHub signal records require decisions" in violations

    root = tmp_path / "absent"
    root.mkdir()
    scan, dispositions, signals = _bundle(root, [], [], [
        {"source_type": "github", "external_id": "acme/widget", "record_key": "github:acme/widget"}
    ])
    violations = []
    validate_records(scan, dispositions, signals, violations, root / "missing.json")
    assert any("missing file" in item for item in violations)
    assert not any("records list" in item for item in violations)
