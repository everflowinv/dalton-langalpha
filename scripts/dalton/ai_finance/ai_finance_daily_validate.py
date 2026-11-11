#!/usr/bin/env python3
"""Validate the AI + finance daily brief without poisoning cron status.

Default/report mode always exits 0 and prints violations for iterative edits.
Only --final exits non-zero when violations remain.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

try:
    from .ai_finance_github_momentum import REPORT_LIMIT, report_rank, resolve_explicit_path
except ImportError:
    from ai_finance_github_momentum import REPORT_LIMIT, report_rank, resolve_explicit_path


def _temp(name: str) -> Path:
    return Path(__file__).resolve().parents[3] / "temp" / name


DEFAULT_MARKDOWN = _temp("ai_finance_daily.md")
DEFAULT_SCAN = _temp("ai_finance_earnings_scan.json")
DEFAULT_DISPOSITIONS = _temp("ai_finance_earnings_dispositions.json")
DEFAULT_SIGNALS = _temp("ai_finance_signal_records.json")
DEFAULT_GITHUB = _temp("ai_finance_github_candidates.json")
DEFAULT_GITHUB_DECISIONS = _temp("ai_finance_github_decisions.json")
STAR_DISPLAY = re.compile(r"总\s*stars\s+(\d{1,3}(?:,\d{3})+|\d+)\s*[；;]\s*日增\s*\+(\d{1,3}(?:,\d{3})+|\d+)(?![\d,.])", re.I)

HEADINGS = (
    "### 今日要闻",
    "### GitHub AI 项目动量",
    "### Earnings Calls：AI 的直接业务影响",
    "### 来源说明",
)
VALID_DISPOSITIONS = {"signal", "watchlist", "reject"}


def load_json(path: Path, violations: list[str]) -> dict[str, Any] | None:
    try:
        path = resolve_explicit_path(path)
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        violations.append(f"missing file: {path}")
        return None
    except json.JSONDecodeError as exc:
        violations.append(f"invalid JSON {path}: {type(exc).__name__}: {exc}")
        return None
    except ValueError as exc:
        violations.append(str(exc))
        return None
    except OSError as exc:
        violations.append(f"invalid JSON {path}: {type(exc).__name__}: {exc}")
        return None
    if not isinstance(value, dict):
        violations.append(f"JSON root must be an object: {path}")
        return None
    return value


def section(text: str, start: str, end: str | None) -> str:
    tail = text.split(start, 1)[1]
    return tail if end is None else tail.split(end, 1)[0]


def visible_chars_without_urls(text: str) -> int:
    text = re.sub(r"\]\(https?://[^)]+\)", "]()", text)
    return len(text)


def validate_markdown(path: Path, violations: list[str]) -> dict[str, int]:
    try:
        path = resolve_explicit_path(path)
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        violations.append(f"missing file: {path}")
        return {}
    except ValueError as exc:
        violations.append(str(exc))
        return {}
    except OSError as exc:
        violations.append(f"cannot read {path}: {exc}")
        return {}

    for heading in HEADINGS:
        count = text.count(heading)
        if count != 1:
            violations.append(f"heading count {heading!r}: expected 1, got {count}")
    positions = [text.find(heading) for heading in HEADINGS]
    if all(pos >= 0 for pos in positions) and positions != sorted(positions):
        violations.append("required headings are out of order")
    if any(line.lstrip().startswith("|") for line in text.splitlines()):
        violations.append("Markdown tables are not allowed")
    if violations and not all(text.count(heading) == 1 for heading in HEADINGS):
        return {"markdown_bytes": len(text.encode("utf-8"))}

    news = section(text, HEADINGS[0], HEADINGS[1])
    github = section(text, HEADINGS[1], HEADINGS[2])
    earnings = section(text, HEADINGS[2], HEADINGS[3])
    news_items = len(re.findall(r"(?m)^\d+\. \*\*", news))
    github_main = github.split("#### 持续跟踪", 1)[0]
    github_items = len(re.findall(r"(?m)^\d+\. ", github_main))
    followup_text = github.split("#### 持续跟踪", 1)[1] if "#### 持续跟踪" in github else ""
    followup_items = len(re.findall(r"(?m)^\s*-\s+(?:\*\*)?\[", followup_text))
    has_followup = "#### 持续跟踪" in github and "https://github.com/" in github.split("#### 持续跟踪", 1)[1]
    earnings_items = len(re.findall(r"(?m)^\d+\. \*\*", earnings))

    if news_items > 8:
        violations.append(f"news item count exceeds 8: {news_items}")
    if github_items == 0 and not has_followup:
        if not any(message in github for message in ("未发现新的高置信度 AI breakout repo", "今日没有达到阈值的新项目或持续热点", "GitHub 官方增星历史核验失败")):
            violations.append("GitHub section has no numbered items and no approved empty-state text")
    if github_items + followup_items > REPORT_LIMIT:
        violations.append(f"GitHub total item count exceeds {REPORT_LIMIT}: {github_items + followup_items}")
    if "####" in github or re.search(r"(?m)^\s*[-*+]\s+(?:\*\*)?\[", github):
        violations.append("GitHub projects must share one numbered list; no follow-up or reserve lists")
    if re.search(r"[%％]|(?:3|三)\s*日均速|(?:7|七)\s*日\s*(?:新增|增量|\+)|周增|增速|斜率|breakout_score|velocity|(?:动量|爆发)?评分", github, re.I):
        violations.append("GitHub display must show total stars and completed-day gain only; hide percentages, multi-day gains and scores")
    if len(github) > 3000:
        violations.append(f"GitHub section exceeds 3000 characters: {len(github)}")

    blocks = re.split(r"(?m)^\d+\. ", github_main)[1:]
    for index, block in enumerate(blocks, 1):
        lines = [line for line in block.strip().splitlines() if line.strip()]
        if len(lines) != 3:
            violations.append(f"GitHub item {index} must have 3 nonblank lines: {len(lines)}")
        if not lines or not re.match(r"^\*\*\[[\w.-]+/[\w.-]+\]\(https://github\.com/", lines[0]):
            violations.append(f"GitHub item {index} must start with a linked repository name")
        if not lines or not STAR_DISPLAY.search(lines[0]):
            violations.append(f"GitHub item {index} must show total stars and completed-day gain")
        chars = visible_chars_without_urls("".join(lines))
        if chars > 220:
            violations.append(f"GitHub item {index} exceeds 220 characters excluding URLs: {chars}")

    return {
        "markdown_bytes": len(text.encode("utf-8")),
        "news_items": news_items,
        "github_items": github_items,
        "github_total_items": github_items + followup_items,
        "earnings_items": earnings_items,
        "github_section_chars": len(github),
    }


def validate_github_tracking(markdown_path: Path, candidates_path: Path, violations: list[str], decisions_path: Path | None = None) -> dict[str, int]:
    data = load_json(candidates_path, violations)
    try:
        markdown_path = resolve_explicit_path(markdown_path)
    except ValueError as exc:
        violations.append(str(exc))
        return {}
    if data is None or not markdown_path.exists():
        return {}
    if data.get("version") != 3:
        violations.append("GitHub candidates must come from size-adjusted tracker v3, not old cached daily data")
        return {}
    text = markdown_path.read_text(encoding="utf-8")
    if not all(heading in text for heading in HEADINGS):
        return {}
    github = section(text, HEADINGS[1], HEADINGS[2])
    detailed = re.findall(r"(?m)^\d+\. \*\*\[([\w.-]+/[\w.-]+)\]", github)
    followup = github.split("#### 持续跟踪", 1)[1] if "#### 持续跟踪" in github else ""
    tracked = re.findall(r"(?m)^\s*-\s+(?:\*\*)?\[([\w.-]+/[\w.-]+)\]\(https://github\.com/", followup)
    # A competitor/source link elsewhere in the section is not a follow-up.
    linked = {name.lower() for name in detailed + tracked}
    required = data.get("continuing_hotspots") or []
    candidates = {item["full_name"].lower(): item for item in data.get("candidates", [])}
    reviewed = None
    if decisions_path is not None:
        decisions = load_json(decisions_path, violations)
        records = decisions.get("records") if decisions else None
        reviewed = {}
        if not isinstance(records, list):
            violations.append("GitHub decisions must contain a records list")
        else:
            for record in records:
                if not isinstance(record, dict) or not isinstance(record.get("repo"), str):
                    violations.append("Invalid GitHub review record")
                    continue
                name = record["repo"].lower()
                if name in reviewed or name not in candidates:
                    violations.append(f"Duplicate or unknown GitHub review: {name}")
                reviewed[name] = record
                if record.get("decision") not in ("include", "exclude") or not record.get("reason") or not record.get("readme_url"):
                    violations.append(f"GitHub review requires decision, reason and README source: {name}")
                if type(record.get("displayed")) is not bool or record["displayed"] != (name in linked):
                    violations.append(f"GitHub displayed decision disagrees with brief: {name}")
            unreviewed = set(candidates) - set(reviewed)
            if unreviewed:
                violations.append("GitHub candidates missing relevance review: " + ", ".join(sorted(unreviewed)))
        required = [name for name in required if reviewed.get(name.lower(), {}).get("decision") != "exclude"]
    if len(detailed + tracked) > REPORT_LIMIT:
        violations.append(f"GitHub total item count exceeds {REPORT_LIMIT}")
    if len(linked) != len(detailed + tracked):
        violations.append("GitHub projects must not be reported twice")
    for name in detailed + tracked:
        item = candidates.get(name.lower())
        if not item or not item.get("history_verified") or not item.get("report_eligible"):
            violations.append(f"GitHub reported without verified active momentum: {name}")
        if reviewed is not None and reviewed.get(name.lower(), {}).get("decision") != "include":
            violations.append(f"GitHub reported without relevance approval: {name}")
    for line in github.splitlines():
        repo = re.match(r"^\d+\. \*\*\[([\w.-]+/[\w.-]+)\]", line)
        if not repo:
            continue
        item = candidates.get(repo[1].lower())
        numbers = STAR_DISPLAY.search(line)
        if item and numbers:
            shown = tuple(int(value.replace(",", "")) for value in numbers.groups())
            actual = ((item.get("github") or {}).get("stars"), (item.get("momentum") or {}).get("daily_gain"))
            if shown != actual:
                violations.append(f"GitHub displayed stars disagree with verified data: {repo[1]}")
    # Lower-ranked continuing projects may sit out only when all eight slots
    # are occupied by equally strong or stronger eligible projects. Monitoring
    # state remains untouched; this is a display budget, not a cooling signal.
    selected = [candidates[name] for name in linked if name in candidates]
    full_valid = len(linked) == REPORT_LIMIT and len(selected) == REPORT_LIMIT and all(
        item.get("history_verified") and item.get("report_eligible") and item.get("significance") for item in selected)
    weakest = min((report_rank(item) for item in selected), default=()) if full_valid else ()
    missing, deferred = [], []
    for name in required:
        if name.lower() in linked:
            continue
        item = candidates.get(name.lower())
        if full_valid and item and item.get("significance") and report_rank(item) <= weakest:
            deferred.append(name)
        else:
            missing.append(name)
    if missing:
        violations.append("Higher-priority active published hotspots omitted: " + ", ".join(missing))
    if reviewed is not None:
        approved = [item for name, item in candidates.items() if reviewed.get(name, {}).get("decision") == "include"]
        expected_count = min(REPORT_LIMIT, len(approved))
        if len(linked) != expected_count:
            violations.append(f"GitHub must display the reviewed top {expected_count}, got {len(linked)}")
        if selected and all(item.get("significance") and item.get("momentum") for item in approved):
            ranks = [report_rank(candidates[name.lower()]) for name in detailed if name.lower() in candidates]
            if ranks != sorted(ranks, reverse=True):
                violations.append("GitHub projects must be ordered by combined breakout rank")
            lowest = min(report_rank(item) for item in selected)
            if any(item["full_name"].lower() not in linked and report_rank(item) > lowest for item in approved):
                violations.append("Higher-priority reviewed GitHub project omitted from combined top 8")
    return {"github_continuing_eligible": len(required), "github_continuing_deferred_by_cap": len(deferred), "github_continuing_missing": len(missing)}


def validate_records(
    scan_path: Path,
    dispositions_path: Path,
    signals_path: Path,
    violations: list[str],
    decisions_path: Path | None = None,
) -> dict[str, int]:
    scan = load_json(scan_path, violations)
    dispositions_obj = load_json(dispositions_path, violations)
    signals_obj = load_json(signals_path, violations)
    if scan is None or dispositions_obj is None or signals_obj is None:
        return {}

    coverage = scan.get("coverage")
    records = dispositions_obj.get("records")
    signals = signals_obj.get("records")
    if not isinstance(coverage, dict):
        violations.append(f"missing object coverage in {scan_path}")
        return {}
    if not isinstance(records, list):
        violations.append(f"missing list records in {dispositions_path}")
        return {}
    if not isinstance(signals, list):
        violations.append(f"missing list records in {signals_path}")
        return {}

    expected = coverage.get("ai_candidates")
    if not isinstance(expected, int):
        violations.append("coverage.ai_candidates must be an integer")
        expected = -1
    if len(records) != expected:
        violations.append(f"disposition count mismatch: expected {expected}, got {len(records)}")

    counts = {name: 0 for name in VALID_DISPOSITIONS}
    for index, record in enumerate(records, 1):
        disposition = record.get("disposition") if isinstance(record, dict) else None
        if disposition not in VALID_DISPOSITIONS:
            violations.append(f"disposition {index} is invalid: {disposition!r}")
            continue
        counts[disposition] += 1
    if sum(counts.values()) != len(records):
        violations.append("valid disposition counts do not sum to all records")

    earnings_signals = sum(
        1 for record in signals if isinstance(record, dict) and record.get("source_type") == "earnings"
    )
    if earnings_signals != counts["signal"]:
        violations.append(
            f"earnings signal records mismatch: dispositions={counts['signal']}, signals={earnings_signals}"
        )
    _validate_signal_sets(scan, records, signals, violations, decisions_path)
    return {
        "ai_candidates": expected,
        "dispositions": len(records),
        "signals": counts["signal"],
        "watchlist": counts["watchlist"],
        "reject": counts["reject"],
        "earnings_signal_records": earnings_signals,
    }


def _validate_signal_sets(
    scan: dict[str, Any],
    records: list[Any],
    signals: list[Any],
    violations: list[str],
    decisions_path: Path | None,
) -> None:
    """One candidate doc_id matches one disposition, and only signal rows may be stored."""
    candidates = scan.get("candidates")
    candidate_ids: list[str] = []
    if not isinstance(candidates, list):
        violations.append("scan candidates must be a list")
    else:
        for candidate in candidates:
            doc_id = candidate.get("doc_id") if isinstance(candidate, dict) else None
            if not isinstance(doc_id, str) or not doc_id:
                violations.append("candidate missing doc_id")
                continue
            candidate_ids.append(doc_id)
        if len(candidate_ids) != len(set(candidate_ids)):
            violations.append("duplicate candidate doc_id")

    disposition_ids: list[str] = []
    signal_disposition_ids: list[str] = []
    blocked_ids: list[str] = []
    for index, record in enumerate(records, 1):
        doc_id = record.get("doc_id") if isinstance(record, dict) else None
        disposition = record.get("disposition") if isinstance(record, dict) else None
        if not isinstance(doc_id, str) or not doc_id:
            violations.append(f"disposition {index} missing doc_id")
            continue
        disposition_ids.append(doc_id)
        if disposition == "signal":
            signal_disposition_ids.append(doc_id)
        elif disposition in {"watchlist", "reject"}:
            blocked_ids.append(doc_id)
    if len(disposition_ids) != len(set(disposition_ids)):
        violations.append("duplicate disposition doc_id")
    if set(candidate_ids) != set(disposition_ids) or len(candidate_ids) != len(disposition_ids):
        violations.append("candidate doc_ids and dispositions are not a one-to-one set")

    earnings_ids: list[str] = []
    github_ids: list[str] = []
    record_keys: list[str] = []
    for record in signals:
        if not isinstance(record, dict):
            violations.append("signal record must be an object")
            continue
        source_type = record.get("source_type")
        external_id = record.get("external_id")
        record_key = record.get("record_key")
        if isinstance(record_key, str):
            record_keys.append(record_key)
        if source_type == "earnings":
            if not isinstance(external_id, str) or not external_id:
                violations.append("earnings signal missing external_id")
            else:
                earnings_ids.append(external_id)
        elif source_type == "github":
            if not isinstance(external_id, str) or not external_id:
                violations.append("github signal missing external_id")
            else:
                github_ids.append(external_id)
        else:
            violations.append(f"signal source_type must be github or earnings: {source_type!r}")
    if len(record_keys) != len(set(record_keys)):
        violations.append("duplicate signal record_key")
    if len(earnings_ids) != len(set(earnings_ids)) or set(earnings_ids) != set(signal_disposition_ids):
        violations.append("earnings signal records do not match the signal disposition set")
    if set(blocked_ids) & set(earnings_ids):
        violations.append("watchlist or reject entered signal records")
    if len(github_ids) != len(set(github_ids)):
        violations.append("duplicate github signal")
    if not github_ids:
        return
    if decisions_path is None:
        violations.append("GitHub signal records require decisions")
        return
    decisions_obj = load_json(decisions_path, violations)
    decision_records = decisions_obj.get("records") if decisions_obj else None
    if decisions_obj is None:
        return
    if not isinstance(decision_records, list):
        violations.append("GitHub decisions must contain a records list")
        return
    reviews: dict[str, dict[str, Any]] = {}
    for review in decision_records:
        repo = review.get("repo") if isinstance(review, dict) else None
        if not isinstance(repo, str) or not repo:
            violations.append("Invalid GitHub review record")
            continue
        key = repo.lower()
        if key in reviews:
            violations.append(f"Duplicate GitHub review: {key}")
        reviews[key] = review
    for repo in github_ids:
        review = reviews.get(repo.lower())
        if not review or review.get("decision") != "include":
            violations.append(f"GitHub signal lacks an include decision: {repo}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--scan", type=Path, default=DEFAULT_SCAN)
    parser.add_argument("--dispositions", type=Path, default=DEFAULT_DISPOSITIONS)
    parser.add_argument("--signals", type=Path, default=DEFAULT_SIGNALS)
    parser.add_argument("--github-candidates", type=Path, default=DEFAULT_GITHUB)
    parser.add_argument("--github-decisions", type=Path, default=DEFAULT_GITHUB_DECISIONS)
    parser.add_argument(
        "--final",
        action="store_true",
        help="exit non-zero if violations remain; report mode always exits zero",
    )
    args = parser.parse_args()

    violations: list[str] = []
    metrics = validate_markdown(args.markdown, violations)
    metrics.update(validate_github_tracking(args.markdown, args.github_candidates, violations, args.github_decisions))
    metrics.update(validate_records(
        args.scan, args.dispositions, args.signals, violations, args.github_decisions
    ))
    payload = {
        "ok": not violations,
        "mode": "final" if args.final else "report",
        "violations": violations,
        "metrics": metrics,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if args.final and violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
