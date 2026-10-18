#!/usr/bin/env python3
"""Validate a Dalton topic research markdown report.

Checks:
1. Per-claim confidence tag citation counts:
   - Lines tagged with 【高】 must contain >= 3 unique [S#] citations (else INFLATED_HIGH).
   - Lines tagged with 【中】 must contain >= 2 unique [S#] citations (else INFLATED_MEDIUM).
2. Low-confidence ratio check:
   - Among all lines tagged with 【高】 / 【中】 / 【低...】, if the share of 【低...】
     is < 15%, emit LOW_RATIO_WARNING ("可能存在置信度注水").
3. Citation closure:
   - Every [S#] referenced in the body must be defined in the `## Sources` section.
   - Any [S#] defined in `## Sources` but never referenced in the body is flagged as UNUSED_SOURCE.
4. Blind-spot gate check:
   - Checks whether "盲区检查完成" appears in the document.
5. Required sections check:
   - 执行摘要, 对覆盖公司的含义, 情景推演, 风险与触发条件, 信息缺口与局限, 数字核验表, Sources
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

CITATION_RE = re.compile(r"\[S(\d+)\]")
SOURCE_DEF_RE = re.compile(r"^\s*(?:[-*]\s+|\d+\.\s+)?\[S(\d+)\]\s+(.+)$")

HIGH_TAG_RE = re.compile(r"【高(?:[：:][^】]*)?】")
MED_TAG_RE = re.compile(r"【中(?:[：:][^】]*)?】")
LOW_TAG_RE = re.compile(r"【低(?:[：:][^】]*)?】")
CONFLICT_TAG_RE = re.compile(r"【冲突(?:[：:][^】]*)?】")
CORRECTION_TAG_RE = re.compile(r"【更正(?:[：:][^】]*)?】")

REQUIRED_SECTIONS = [
    "执行摘要",
    "对覆盖公司的含义",
    "情景推演",
    "风险与触发条件",
    "信息缺口与局限",
    "数字核验表",
    "Sources",
]


def split_body_and_sources(text: str) -> Tuple[str, str, int]:
    """Split markdown into (body_before_sources, sources_section, sources_start_line)."""
    lines = text.splitlines()
    sources_idx = None
    for idx, line in enumerate(lines):
        if re.match(r"^#{1,3}\s+(?:Sources|参考来源|信息来源)\b", line.strip(), re.IGNORECASE):
            sources_idx = idx
            break
    if sources_idx is None:
        return text, "", len(lines) + 1
    body = "\n".join(lines[:sources_idx])
    sources = "\n".join(lines[sources_idx + 1 :])
    return body, sources, sources_idx + 2


def validate_markdown(text: str) -> Dict[str, Any]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    body, sources_section, sources_start_line = split_body_and_sources(text)
    body_lines = body.splitlines()

    high_count = 0
    med_count = 0
    low_count = 0
    conflict_count = 0
    correction_count = 0

    used_citations: Set[str] = set()

    for line_no, raw_line in enumerate(body_lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("```"):
            continue

        cites_in_line = set(CITATION_RE.findall(line))
        used_citations.update(cites_in_line)

        has_high = bool(HIGH_TAG_RE.search(line))
        has_med = bool(MED_TAG_RE.search(line))
        has_low = bool(LOW_TAG_RE.search(line))
        has_conflict = bool(CONFLICT_TAG_RE.search(line))
        has_correction = bool(CORRECTION_TAG_RE.search(line))

        if has_high:
            high_count += 1
            if len(cites_in_line) < 3:
                errors.append(
                    {
                        "code": "INFLATED_HIGH",
                        "line": line_no,
                        "citations_found": sorted(f"S{c}" for c in cites_in_line),
                        "message": f"第 {line_no} 行标注【高】但仅包含 {len(cites_in_line)} 个独立引用（需 >=3 个 [S#]）。",
                        "excerpt": line[:120],
                    }
                )

        if has_med:
            med_count += 1
            if len(cites_in_line) < 2:
                errors.append(
                    {
                        "code": "INFLATED_MEDIUM",
                        "line": line_no,
                        "citations_found": sorted(f"S{c}" for c in cites_in_line),
                        "message": f"第 {line_no} 行标注【中】但仅包含 {len(cites_in_line)} 个引用（需 >=2 个 [S#]）。",
                        "excerpt": line[:120],
                    }
                )

        if has_low:
            low_count += 1
            if len(cites_in_line) < 1:
                warnings.append(
                    {
                        "code": "LOW_WITHOUT_CITATION",
                        "line": line_no,
                        "message": f"第 {line_no} 行标注【低】但未附任何 [S#] 来源编号。",
                        "excerpt": line[:120],
                    }
                )

        if has_conflict:
            conflict_count += 1
            if len(cites_in_line) < 2:
                errors.append(
                    {
                        "code": "CONFLICT_MISSING_SIDES",
                        "line": line_no,
                        "citations_found": sorted(f"S{c}" for c in cites_in_line),
                        "message": f"第 {line_no} 行标注【冲突】但引用少于 2 个（必须分别列出冲突双方来源）。",
                        "excerpt": line[:120],
                    }
                )

        if has_correction:
            correction_count += 1

    total_hm_low = high_count + med_count + low_count
    low_ratio = (low_count / total_hm_low) if total_hm_low > 0 else 0.0

    if total_hm_low == 0:
        errors.append(
            {
                "code": "NO_CONFIDENCE_TAGS",
                "line": 1,
                "message": "正文未检测到任何【高】/【中】/【低】置信度标签。",
            }
        )
    elif low_ratio < 0.15:
        warnings.append(
            {
                "code": "LOW_RATIO_WARNING",
                "low_ratio": round(low_ratio, 4),
                "message": (
                    f"低置信度【低】条目占比为 {low_ratio:.1%}（低于 15% 阈值），"
                    "可能存在置信度注水，请审稿人（dalton-reviewer）重点复核。"
                ),
            }
        )

    defined_citations: Dict[str, int] = {}
    if sources_section:
        for offset, raw_line in enumerate(sources_section.splitlines()):
            line_no = sources_start_line + offset
            m = SOURCE_DEF_RE.match(raw_line)
            if m:
                cid = m.group(1)
                if cid in defined_citations:
                    errors.append(
                        {
                            "code": "DUPLICATE_SOURCE_DEF",
                            "line": line_no,
                            "source_id": f"S{cid}",
                            "message": f"Sources 中重复定义了 [S{cid}]（第 {defined_citations[cid]} 行与第 {line_no} 行）。",
                        }
                    )
                defined_citations[cid] = line_no

    missing_defs = sorted((int(c) for c in used_citations if c not in defined_citations))
    for cid in missing_defs:
        errors.append(
            {
                "code": "UNDEFINED_CITATION",
                "source_id": f"S{cid}",
                "message": f"正文引用了 [S{cid}]，但在 ## Sources 中未找到定义。",
            }
        )

    unused_defs = sorted((int(c) for c in defined_citations if c not in used_citations))
    for cid in unused_defs:
        warnings.append(
            {
                "code": "UNUSED_SOURCE",
                "line": defined_citations[str(cid)],
                "source_id": f"S{cid}",
                "message": f"## Sources 定义了 [S{cid}]，但正文中从未引用。",
            }
        )

    missing_sections: List[str] = []
    for sec in REQUIRED_SECTIONS:
        pattern = re.compile(rf"^#{{1,3}}\s+.*{re.escape(sec)}", re.MULTILINE | re.IGNORECASE)
        if not pattern.search(text):
            missing_sections.append(sec)
            errors.append(
                {
                    "code": "MISSING_SECTION",
                    "section": sec,
                    "message": f"缺少必填章节标题：{sec}",
                }
            )

    blind_spot_checked = "盲区检查完成" in text
    if not blind_spot_checked:
        warnings.append(
            {
                "code": "MISSING_BLIND_SPOT_CONFIRMATION",
                "message": "报告中未包含“盲区检查完成”确认标记（Phase 3.5 门禁要求）。",
            }
        )

    passed = len(errors) == 0
    return {
        "passed": passed,
        "counts": {
            "high": high_count,
            "medium": med_count,
            "low": low_count,
            "conflict": conflict_count,
            "correction": correction_count,
            "total_tagged": total_hm_low,
            "low_ratio": round(low_ratio, 4),
            "citations_used": len(used_citations),
            "citations_defined": len(defined_citations),
        },
        "blind_spot_checked": blind_spot_checked,
        "missing_sections": missing_sections,
        "errors": errors,
        "warnings": warnings,
    }


def run_self_test() -> int:
    sample_valid = """# 专题研究：CoWoS 先进封装供需格局（时间范围：2026Q1–2026Q4）

## 执行摘要
- 2026Q4 台积电 CoWoS 月产能将达 7.5 万片【高】[S1][S2][S3]。
- B200 封装良率已在 2026Q2 爬升至 92% 以上【中】[S1][S2]。
- 二供日月光 2026 下半年外溢订单占比预计约 15%【低：单一供应链访谈推断】[S4]。
- 【冲突】来源 A 称某云厂 2027 ASIC 订单为“数万颗”[S2]，来源 B 称“十万颗以上”[S3]。

## 对覆盖公司的含义
- 对 NVDA 与 AVGO 的出货节奏分析【中】[S1][S3]。

## 情景推演
- 基准、乐观与悲观三档产能情景【中】[S1][S2]。

## 风险与触发条件
- 基板供应瓶颈与良率波动【中】[S2][S3]。

## 信息缺口与局限
- 目前缺乏数据：非台系二线封测厂 2027 年具体扩产 CapEx。
- Phase 3.5 状态：盲区检查完成。

## 数字核验表
| 指标 | 报告数值 | 原始来源 | 核验状态 |
|---|---|---|---|
| 2026Q4 CoWoS 月产能 | 7.5 万片 | 台积电 2026Q2 法说会 [S1] | PASS |

## Sources
- [S1] TSMC 2026Q2 Earnings Call Transcript, 2026-07-18
- [S2] Morgan Stanley Asia Semiconductor Deep Dive, 2026-08-05
- [S3] Goldman Sachs AI Supply Chain Tracker, 2026-08-12
- [S4] Supply Chain Channel Check Note, 2026-08-15
"""
    res_valid = validate_markdown(sample_valid)
    assert res_valid["passed"] is True, f"Expected sample_valid to pass, got: {res_valid}"
    assert res_valid["counts"]["low_ratio"] == 0.1667, f"Unexpected low_ratio: {res_valid['counts']}"

    sample_invalid = """# 专题草稿
## 执行摘要
- 只有两个引用却标高【高】[S1][S2]。
- 只有一个引用却标中【中】[S1]。
- 引用了未定义的来源【中】[S1][S99]。

## Sources
- [S1] Source 1
- [S2] Source 2
"""
    res_invalid = validate_markdown(sample_invalid)
    assert res_invalid["passed"] is False, "Expected sample_invalid to fail"
    codes = {e["code"] for e in res_invalid["errors"]}
    assert "INFLATED_HIGH" in codes, f"Missing INFLATED_HIGH in {codes}"
    assert "INFLATED_MEDIUM" in codes, f"Missing INFLATED_MEDIUM in {codes}"
    assert "UNDEFINED_CITATION" in codes, f"Missing UNDEFINED_CITATION in {codes}"
    print("validate_topic.py self-test PASSED.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate confidence tags, citation closure, and required sections in a topic research report."
    )
    parser.add_argument("cmd", nargs="?", choices=["self-test"], help="Optional 'self-test' subcommand.")
    parser.add_argument("--input", "-i", type=str, help="Path to the markdown topic report.")
    parser.add_argument("--json", action="store_true", help="Output validation result as JSON.")
    parser.add_argument("--self-test", action="store_true", help="Run built-in unit self-tests.")
    args = parser.parse_args()

    if args.self_test or args.cmd == "self-test":
        return run_self_test()

    if not args.input:
        parser.error("Either --input <report.md> or --self-test is required.")

    path = Path(args.input)
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        return 2

    text = path.read_text(encoding="utf-8")
    result = validate_markdown(text)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        status = "PASS" if result["passed"] else "FAIL"
        c = result["counts"]
        print(
            f"[{status}] {path.name} | 高:{c['high']} 中:{c['medium']} 低:{c['low']} "
            f"(低占比:{c['low_ratio']:.1%}) 冲突:{c['conflict']} 更正:{c['correction']}"
        )
        for err in result["errors"]:
            print(f"  [ERROR] {err['code']}: {err['message']}")
        for warn in result["warnings"]:
            print(f"  [WARN]  {warn['code']}: {warn['message']}")

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
