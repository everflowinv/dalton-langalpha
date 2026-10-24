#!/usr/bin/env python3
"""12-question report structure and company-specific research agenda."""
import argparse
import json
import pathlib
import re
import sys

QUESTIONS = (
    ("这家公司靠什么赚钱？", "A、C", (1, 2)),
    ("关键驱动变量是什么，趋势如何？", "A、C、D", (1, 3, 8)),
    ("市场预测里藏着哪些隐含假设？", "B、C", (4, 8, 9)),
    ("管理层和企业文化值得信任吗，股东如何受益？", "B、C、D", (1, 2, 7)),
    ("股价为什么在当前位置？", "B、C", (8,)),
    ("哪些高频数据能帮助我们检验判断？", "A、D", (10,)),
    ("市场共识、多方和空方分别怎么看？", "B、D", (5, 8)),
    ("做多、做空还是观察，我们的差异化观点是什么？", "主笔综合 A/B/C/D", (2, 4, 6, 9)),
    ("催化剂如何使判断兑现？", "B、C", (9, 10)),
    ("投资判断需要多长时间兑现？", "主笔综合 B/C", (6, 9, 10)),
    ("需要对冲吗，应该对冲什么风险？", "主笔综合 A/D", (5, 6)),
    ("我们可能错在哪里，遗漏了什么风险？", "主笔综合 A/B/C/D", (5, 10)),
)
CHAPTERS = (
    "公司与业务", "业务质量", "行业与竞争", "核心投资逻辑", "风险与反方证据",
    "风险收益", "管理层与股权", "市场预期", "估值", "后续跟踪",
)
CN_NUM = ("零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十")
CITE = re.compile(r"\[(?:引用\d+|[ABCD]-\d+)\]")
Q_TITLE = re.compile(r"Q(\d+)[.．、]\s*(.+)$")
SECTION = re.compile(r"([零一二三四五六七八九十]+)[、.．]\s*(.+)$")


def headings(text):
    """Ignore fenced examples and comments; record real ATX headings."""
    text = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group().count("\n"), text, flags=re.S)
    result, fence = [], None
    for line_no, line in enumerate(text.splitlines()):
        marker = re.match(r"^\s*(\x60{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = (token[0], len(token))
            elif token[0] == fence[0] and len(token) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue
        m = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if m:
            result.append((line_no, len(m.group(1)), m.group(2).strip()))
    return result


def question_blocks(text):
    lines, hs = text.splitlines(), headings(text)
    blocks = []
    for pos, (line_no, level, title) in enumerate(hs):
        m = Q_TITLE.fullmatch(title)
        if not m:
            continue
        end = next((h[0] for h in hs[pos + 1:] if h[1] <= level), len(lines))
        blocks.append({
            "id": int(m.group(1)), "line": line_no, "level": level,
            "title": m.group(2), "body": "\n".join(lines[line_no + 1:end]),
        })
    return blocks


def content_field(body, label):
    m = re.search(rf"^[ \t]*(?:[-*][ \t]*)?{re.escape(label)}[：:][ \t]*(.*)$", body, flags=re.M)
    return m.group(1).strip() if m else ""


def clean_body(body):
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    body = re.sub(r"(?ms)^\s*(\x60{3,}|~{3,}).*?^\s*\1\s*$", "", body)
    return body


def validate_agenda(text):
    errors = []
    blocks = question_blocks(text)
    ids = [b["id"] for b in blocks]
    if ids != list(range(1, 13)):
        errors.append("研究问题清单需按顺序包含唯一的 Q1–Q12")
    if "待填写" in text:
        errors.append("研究问题清单仍有“待填写”：先读一手材料并填写公司专属问题")
    for name in ("已读一手材料", "关键驱动", "关键矛盾"):
        if not content_field(clean_body(text), name):
            errors.append(f"研究问题清单缺“{name}”")
    for b in blocks:
        for name in ("公司问题", "关键假设", "取证安排", "论证章节"):
            if not content_field(clean_body(b["body"]), name):
                errors.append(f"研究问题清单 Q{b['id']} 缺“{name}”")
    return errors


def make_agenda(name, ticker):
    lines = [
        f"# {name}（{ticker}）12问研究提纲", "",
        "先初读一手材料，再填写以下内容。取证返回后更新证据与缺口，终稿答案写入报告第零节。",
        "", "已读一手材料：待填写（报告期、原文位置、已读部分）",
        "关键驱动：待填写（收入、利润与现金流传导）",
        "关键矛盾：待填写（最影响当前判断的公司专属争议）", "",
    ]
    for i, (title, branches, chapters) in enumerate(QUESTIONS, 1):
        lines += [
            f"## Q{i}. {title}", "",
            "公司问题：待填写", "关键假设：待填写",
            f"取证安排：{branches}；待填写具体证据需求",
            "论证章节：" + "、".join(f"第{n}章" for n in chapters),
            "证据进展：待填写（已有支持、最强反方、尚待补证）", "",
        ]
    return "\n".join(lines)


def validate_report_structure(text, max_chapter=10):
    errors = []
    hs = headings(text)
    blocks = question_blocks(text)
    ids = [b["id"] for b in blocks]
    if ids != list(range(1, 13)):
        errors.append("报告需按顺序包含唯一的 Q1–Q12；重复、漏题或额外题号须修正")
    section0, chapters = [], {}
    summaries = [h[0] for h in hs if h[1] == 2 and h[2] == "核心判断"]
    for line, level, title in hs:
        m = SECTION.fullmatch(title)
        if level != 2 or not m or m.group(1) not in CN_NUM:
            continue
        num = CN_NUM.index(m.group(1))
        if num == 0:
            section0.append(line)
        else:
            chapters.setdefault(num, []).append((line, m.group(2)))
    if len(section0) != 1:
        errors.append("需有唯一的二级标题“零、12 个核心问题与投资判断”")
    else:
        line0 = section0[0]
        end0 = next((line for line, level, title in hs if line > line0 and level <= 2), len(text.splitlines()))
        if not summaries or summaries[-1] >= line0:
            errors.append("第零节12问须位于核心判断之后")
        if not chapters.get(1) or line0 >= chapters[1][0][0]:
            errors.append("第零节12问须位于第一章之前，不能放在附录")
        if any(b["level"] != 3 or not line0 < b["line"] < end0 for b in blocks):
            errors.append("Q1–Q12 应全部使用三级标题并位于第零节")
    positions = []
    for num, title in enumerate(CHAPTERS[:max_chapter], 1):
        entries = chapters.get(num, [])
        if len(entries) != 1 or entries[0][1] != title:
            errors.append(f"第{num}章应有唯一的二级标题“{CN_NUM[num]}、{title}”")
        else:
            positions.append(entries[0][0])
    if positions != sorted(positions):
        errors.append("十章论证须按第一章至第十章排列")
    for b in blocks:
        body = clean_body(b["body"])
        source = content_field(body, "来源")
        if not source:
            errors.append(f"Q{b['id']} 缺来源行")
        elif not CITE.search(source) and not ("缺口" in source and len(source.replace("缺口", "").strip(" ——：:")) >= 8):
            errors.append(f"Q{b['id']} 来源行需有具体引用编号，或明确缺口及所需材料")
        detail = content_field(body, "详见")
        targets = re.findall(r"第(\d{1,2}|[一二三四五六七八九十]+)章", detail)
        nums = [int(t) if t.isdigit() else CN_NUM.index(t) if t in CN_NUM else -1 for t in targets]
        if not nums:
            errors.append(f"Q{b['id']} 缺有效论证章节定位，例如“详见：第1章、第3章”")
        elif max_chapter == 10 and any(n not in chapters or len(chapters[n]) != 1 for n in nums):
            errors.append(f"Q{b['id']} 指向不存在或不唯一的论证章节")
        elif max_chapter < 10 and any(not (1 <= n <= 10) for n in nums):
            errors.append(f"Q{b['id']} 指向不存在或不唯一的论证章节")
        answer = "\n".join(
            line for line in body.splitlines()
            if not re.match(r"^\s*(?:[-*]\s*)?(来源|详见)[：:]", line)
        )
        answer = CITE.sub("", answer).strip(" \n-*")
        if not answer or re.fullmatch(r"[（(]?(待填写|待补充|见正文|略|TODO)[）)]?[。.]?", answer, flags=re.I):
            errors.append(f"Q{b['id']} 缺实质回答，不能只有来源与章节指向")
    return errors


def main():
    parser = argparse.ArgumentParser(description="Dalton 12-question research structure validator & scaffold generator")
    sub = parser.add_subparsers(dest="command", required=True)

    p_va = sub.add_parser("validate-agenda", help="校验 12 问研究问题清单 (questions.md)")
    p_va.add_argument("path", help="questions.md 文件路径")
    p_va.add_argument("--json", action="store_true", help="输出 JSON")

    p_vr = sub.add_parser("validate-report", help="校验 12 问投资备忘录结构 (memo.md)")
    p_vr.add_argument("path", help="memo.md 文件路径")
    p_vr.add_argument("--max-chapter", type=int, default=10, help="要求已存在的最高章节号（分段写作时可用 2 或 6）")
    p_vr.add_argument("--json", action="store_true", help="输出 JSON")

    p_ma = sub.add_parser("make-agenda", help="生成 12 问研究问题清单初始骨架")
    p_ma.add_argument("--name", required=True, help="公司名称")
    p_ma.add_argument("--ticker", required=True, help="股票代码")
    p_ma.add_argument("--out", default=None, help="输出文件路径（不指定则打印到 stdout）")

    args = parser.parse_args()
    if args.command == "make-agenda":
        content = make_agenda(args.name, args.ticker)
        if args.out:
            out_path = pathlib.Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content, encoding="utf-8")
            print(f"Wrote agenda scaffold to {out_path}")
        else:
            print(content)
        return

    target = pathlib.Path(args.path)
    if not target.exists():
        msg = f"文件不存在: {target}"
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "path": str(target), "errors": [msg]}, ensure_ascii=False, indent=2))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        sys.exit(1)

    text = target.read_text(encoding="utf-8")
    if args.command == "validate-agenda":
        errors = validate_agenda(text)
    else:
        errors = validate_report_structure(text, max_chapter=args.max_chapter)

    ok = len(errors) == 0
    if getattr(args, "json", False):
        print(json.dumps({"ok": ok, "path": str(target), "errors": errors}, ensure_ascii=False, indent=2))
    else:
        if ok:
            print(f"PASS: {target}")
        else:
            for err in errors:
                print(f"FAIL: {err}")
            print(f"结果：FAIL（共 {len(errors)} 项错误）")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
