#!/usr/bin/env python3
"""Dalton Full Coverage — investment memo validator.

New single-track memos: section structure (per ``research_structure.SECTIONS``),
citation closure against the trailing ``## 引用`` list, evidence-branch markers.
No citation-rate floor, no facts full-coverage sweep and no minimum length —
those pushed every number and every ledger row into the prose (the DASH memo
failure mode). Readability rules live in ``references/memo-writing.md``.

Legacy dual-track memos (第零节 12 问 + 十章) — drafts from runs still in
flight when the structure switched — keep validating under the OLD rules
(基本信息, 12 问结构, Thesis/Risk-Matrix citation rate, facts coverage,
period sampling, ``--part 1|2|3``), so an in-flight run can finish without
being rewritten. Detection rides ``research_structure.is_legacy_report``.
"""
import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from facts import parse_ledger
from research_structure import headings, is_legacy_report, validate_agenda, validate_report_structure

CITE = re.compile(r"\[(引用\d+|[ABCD]-\d+)\]")
KINDS = ("a", "b", "c", "d")
PERIOD_HINT = re.compile(
    r"(FY\d{2,4}|\d{4}\s*年|[1-4]Q\d{2}|Q[1-4]\s*\d{2,4}|\d{4}Q[1-4]|1H\d{2}|2H\d{2}|"
    r"\d{4}-\d{2}|截至|过去 ?12 ?个月|TTM|最近一个季度)"
)


def _cite_sort_key(cid: str) -> tuple[int, int | str]:
    if cid.startswith("引用") and cid[2:].isdigit():
        return (0, int(cid[2:]))
    return (1, cid)


def check_thesis_and_risk_citations(text: str) -> list:
    """Legacy rule: ≥70% of Thesis / Risk Matrix rows carry citations."""
    fails = []
    for label, pat in (("Thesis", r"^#{2,6}\s*.*Thesis.*$"), ("Risk Matrix", r"^#{2,6}\s*.*Risk Matrix.*$")):
        mm = re.search(pat, text, flags=re.M | re.I)
        if not mm:
            fails.append(f"缺 {label} 章节")
            continue
        rest = text[mm.end():]
        nxt = re.search(r"^#{2,3}\s", rest, flags=re.M)
        block = rest[:nxt.start()] if nxt else rest
        body, seen_table_header = [], False
        for line in block.splitlines():
            s = line.strip()
            if not s.startswith(("-", "*", "|")) or "---" in s:
                continue
            if s.startswith("|") and not seen_table_header:
                seen_table_header = True
                continue
            body.append(line)
        bad = [l.strip()[:50] for l in body if not CITE.search(l)]
        if not body:
            fails.append(f"{label} 章节无实质列表或表格数据行")
        elif len(bad) > len(body) * 0.3:
            fails.append(f"{label} 有 {len(bad)}/{len(body)} 行（>30%）没有 [引用N] 引用标记，例：{bad[0]}")
    return fails


def check_citation_closure(text: str) -> tuple[set, list, list]:
    fails, warns = [], []
    listed = set()
    mlist = re.search(r"^#{2,6}\s*(?:引用|.*引用清单.*)\s*$", text, flags=re.M)
    used = set(CITE.findall(text[:mlist.start()] if mlist else text))
    if not mlist:
        fails.append("没有 `## 引用` 章节")
    else:
        tail = text[mlist.end():]
        for cid in CITE.findall(tail):
            listed.add(cid)
        for line in tail.splitlines():
            m2 = re.match(r"^\s*[-*]?\s*\[?(引用\d+|[ABCD]-\d+)\]?[.．、:：\s]", line)
            if m2:
                listed.add(m2.group(1))
        orphan = sorted(used - listed, key=_cite_sort_key)
        unused = sorted(listed - used, key=_cite_sort_key)
        if orphan:
            fails.append(f"正文引用在 `## 引用` 里找不到：{', '.join(orphan[:12])}")
        if unused:
            warns.append(f"`## 引用` 里有未被正文引用的条目：{', '.join(unused[:12])}")
    if not used:
        fails.append("正文没有任何 [引用N] 引用标记")
    return used, fails, warns


def _check_research_dir(research_dir, warns):
    if research_dir is not None and research_dir.exists():
        for k in KINDS:
            p = research_dir / f"e{k}.md"
            if not p.exists():
                warns.append(f"取证支线 {k.upper()} 无产物 {p.name}")
            elif not p.read_text(encoding="utf-8").strip().endswith("<!-- 完成 -->"):
                warns.append(f"{p.name} 末行没有 `<!-- 完成 -->`，可能未完整落盘")


def validate_memo_text(
    text: str,
    part: int | None = None,
    questions_path: pathlib.Path | None = None,
    facts_path: pathlib.Path | None = None,
    research_dir: pathlib.Path | None = None,
) -> tuple[list, list, dict]:
    fails, warns = [], []
    lines = text.splitlines()
    h2_titles = [title for _, level, title in headings(text) if level == 2]

    legacy = is_legacy_report(text)
    effective_part = part if part in (1, 2, 3) else 3
    max_chapter = 2 if effective_part == 1 else (6 if effective_part == 2 else 10)

    if legacy:
        # ---- 旧结构：按切换前的旧规则校验（在途旧稿收尾） ----
        if "基本信息" not in h2_titles:
            fails.append("缺二级标题“## 基本信息”")
        fails.extend(validate_report_structure(text, max_chapter=max_chapter))
        if questions_path is not None:
            if not questions_path.exists():
                fails.append(f"缺公司问题清单：{questions_path}")
            else:
                fails.extend(validate_agenda(questions_path.read_text(encoding="utf-8"), warnings=warns))
        if effective_part >= 2:
            fails.extend(check_thesis_and_risk_citations(text))
        used_cites = set(CITE.findall(text))
        ledger_count = 0
        if effective_part == 3:
            if not any(t.startswith("附录") for t in h2_titles):
                fails.append("缺二级标题“## 附录”")
            used_cites, c_fails, c_warns = check_citation_closure(text)
            fails.extend(c_fails)
            warns.extend(c_warns)
        else:
            if not used_cites:
                fails.append("当前段落没有任何 [引用N] 引用标记")
        if facts_path is not None and facts_path.exists():
            ledger = parse_ledger(facts_path.read_text(encoding="utf-8"))
            ledger_count = len(ledger)
            if effective_part == 3 and ledger:
                miss = [r for r in ledger if str(r.get("value", "")).strip() and str(r["value"]).strip() not in text]
                if miss:
                    warns.append(
                        "底表以下指标未在正文原样出现："
                        + "，".join(f"{r.get('label')}({r.get('value')})" for r in miss[:10])
                    )
        _check_research_dir(research_dir, warns)
        naked = []
        for line in lines:
            s = line.strip()
            if s.startswith(("|", ">", "#")):
                continue
            if re.search(r"\d+(\.\d+)?\s*%", line) and not PERIOD_HINT.search(line):
                naked.append(s[:60])
        if naked:
            warns.append(f"{len(naked)} 行百分比缺少明显期间标注，例：{naked[0]}")
        stats = {
            "lines": len(lines),
            "structure": "legacy",
            "part": effective_part,
            "max_chapter": max_chapter,
            "citations": len(used_cites),
            "ledger_rows": ledger_count,
        }
        return fails, warns, stats

    # ---- 新结构：章节 + 引用闭环 + 支线完成标记 ----
    if part is not None:
        warns.append(f"--part 仅对旧版十二章结构生效，本文按新结构整体校验（忽略 --part {part}）")
    fails.extend(validate_report_structure(text))

    if questions_path is not None:
        if not questions_path.exists():
            fails.append(f"缺公司问题清单：{questions_path}")
        else:
            fails.extend(validate_agenda(questions_path.read_text(encoding="utf-8"), warnings=warns))

    used_cites, c_fails, c_warns = check_citation_closure(text)
    fails.extend(c_fails)
    warns.extend(c_warns)

    _check_research_dir(research_dir, warns)
    if facts_path is not None and not facts_path.exists():
        warns.append(f"数字底表不存在：{facts_path}")

    stats = {
        "lines": len(lines),
        "structure": "single-track",
        "citations": len(used_cites),
    }
    return fails, warns, stats


def main():
    parser = argparse.ArgumentParser(description="Dalton 投资备忘录校验：新结构查章节与引用闭环；旧结构（第零节 12 问 + 十章）按旧规则校验")
    parser.add_argument("memo_pos", nargs="?", default=None, help="备忘录文件路径（可选位置参数）")
    parser.add_argument("--memo", default=None, help="备忘录文件路径")
    parser.add_argument("--mode", default="full", choices=["full", "screen"], help="校验模式（默认 full）")
    parser.add_argument("--part", type=int, default=None, choices=[1, 2, 3],
                        help="旧版三段式分段校验（仅旧结构生效）：1(至第二章) / 2(至第六章) / 3(全文)")
    parser.add_argument("--questions", default=None, help="可选：同步校验 questions.md 问题清单")
    parser.add_argument("--facts", default=None, help="可选：旧结构核对底表覆盖；新结构只核对存在")
    parser.add_argument("--research-dir", default=None, help="可选：包含 questions.md / facts.md / ea..ed.md 的研究目录")
    parser.add_argument("--strict", action="store_true", help="将 WARN 也视为失败")
    parser.add_argument("--json", action="store_true", help="输出 JSON")

    args = parser.parse_args()
    memo_arg = args.memo or args.memo_pos
    if not memo_arg:
        parser.error("必须提供备忘录路径（位置参数或 --memo）")

    memo_path = pathlib.Path(memo_arg)
    if not memo_path.exists():
        msg = f"备忘录文件不存在: {memo_path}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg, "memo": str(memo_path)}, ensure_ascii=False, indent=2))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        sys.exit(1)

    rdir = pathlib.Path(args.research_dir) if args.research_dir else None
    q_path = pathlib.Path(args.questions) if args.questions else (rdir / "questions.md" if rdir and (rdir / "questions.md").exists() else None)
    f_path = pathlib.Path(args.facts) if args.facts else (rdir / "facts.md" if rdir and (rdir / "facts.md").exists() else None)

    text = memo_path.read_text(encoding="utf-8")
    fails, warns, stats = validate_memo_text(
        text,
        part=args.part,
        questions_path=q_path,
        facts_path=f_path,
        research_dir=rdir,
    )

    ok = not fails and (not warns or not args.strict)
    payload = {
        "ok": ok,
        "memo": str(memo_path),
        "mode": args.mode,
        **stats,
        "fails": fails,
        "warns": warns,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        part_label = f"Part {stats['part']}" if legacy_part_label(stats, args) else f"Mode {args.mode}"
        print(f"validate_memo {memo_path} [{part_label}]  结构 {stats['structure']}  行数 {stats['lines']}  引用 {stats['citations']} 条")
        for f in fails:
            print("FAIL:", f)
        for w in warns:
            print("WARN:", w)
        print("结果：", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def legacy_part_label(stats: dict, args: argparse.Namespace) -> bool:
    return stats.get("structure") == "legacy" and args.part is not None


if __name__ == "__main__":
    main()
