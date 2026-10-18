#!/usr/bin/env python3
"""Dalton Bull/Bear Discussion (v4.2) 机械格式检查与支线合并工具。

移植自 OpenClaw bullbear-discussion/scripts/render.py check()，剥离飞书分块、落盘标记与 $/~ 字符禁令。

检查项：
1. 正文引用编号集合 [引用N] == 引用清单集合 [引用N]
2. 股东回报引用编号集合 [回报引用N]（第一节 + 附录正文）== 附录清单集合 [回报引用N]
3. 股东回报第一节非空非注释行 <= 48 行（目标 <= 40 行），且无 > 120 字的长自然段
4. §2 市场共识：三个父 bullet（看多的人默认成立 / 看空的人默认成立 / 两边都不争的），父行冒号后无文字，子 bullet 数分别为 2–4 / 2–4 / 1–3
5. §3 Top debates：每个 ### Debate 含 5 个一级 bullet 块（分歧 / Bull 怎么看 / Bear 怎么看 / 双方最强反驳 / 我的判断）
6. §4 Earnings revision map：含“→”的行必须写“→ Debate N”
7. §6 财务体检：非空非注释行 <= 12 行；全文不出现“占净利润”（应写“相当于净利润的”）
8. §8 一句话：不出现“Debate N 和/与/或/同时/成立/兑现”这类裸 debate 编号写法
9. 引用清单（## 十、引用清单 或顺延章节）之后不能再有正文章节；若有 <!-- INSERT:return_appendix --> 须在引用清单之前
10. 全文不出现“你的判断”（应为“我的判断”）

用法：
  python bb_check.py path/to/main.md
  python bb_check.py path/to/drafts/2026-10-18-bullbear.md
  python bb_check.py path/to/main.md --return path/to/return.md --appendix path/to/return_appendix.md --merge-out path/to/merged.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys

REF_HEAD_RE = re.compile(r"^##\s+(?:十|十一|十二|十三)、引用清单\b", re.M)
RET_SEC_RE = re.compile(r"^##\s+一、股东回报\b", re.M)
APP_SEC_RE = re.compile(r"^##\s+(?:九|十|十一|十二)、股东回报附录\b", re.M)
DEBATE_BLOCKS = ["分歧", "Bull 怎么看", "Bear 怎么看", "双方最强反驳", "我的判断"]


def section(body: str, start: str, end: str | None) -> str:
    if start not in body:
        return ""
    seg = body.split(start, 1)[1]
    if end and end in seg:
        seg = seg.split(end, 1)[0]
    return seg


def extract_embedded_return(body: str) -> tuple[str | None, str | None]:
    """若文档已合并，从正文中切出第一节股东回报与第九节股东回报附录。"""
    ret = None
    app = None
    m_ret = RET_SEC_RE.search(body)
    if m_ret and "## 二、" in body:
        end_ret = body.find("## 二、", m_ret.start())
        if end_ret != -1:
            ret = body[m_ret.start():end_ret]
    m_app = APP_SEC_RE.search(body)
    ref_m = REF_HEAD_RE.search(body)
    if m_app:
        end_app = ref_m.start() if (ref_m and ref_m.start() > m_app.start()) else len(body)
        app = body[m_app.start():end_app]
    return ret, app


def check(
    slug: str,
    body: str,
    ret: str | None = None,
    app: str | None = None,
    require_return_markers: bool = False,
) -> list[str]:
    problems: list[str] = []

    # 若未显式传 ret/app，尝试从已合并正文中提取
    if ret is None and app is None:
        emb_ret, emb_app = extract_embedded_return(body)
        ret = emb_ret
        app = emb_app

    ref_m = REF_HEAD_RE.search(body)
    if not ref_m:
        problems.append(f"{slug}: 主稿缺 `## 十、引用清单`（或顺延的十一/十二/十三节引用清单）标题")

    before_ref = body[:ref_m.start()] if ref_m else body
    if ref_m:
        after_ref = body[ref_m.end():]
        extra_sec = re.findall(r"^##\s+([一二三四五六七八九十]+、.*)", after_ref, flags=re.M)
        if extra_sec:
            problems.append(
                f"{slug}: 引用清单之后不应再有其他章节 {extra_sec}，附加专题须放在第八节后、股东回报附录与引用清单之前"
            )
        if "<!-- INSERT:return_appendix -->" in body:
            app_pos = body.find("<!-- INSERT:return_appendix -->")
            if app_pos > ref_m.start():
                problems.append(
                    f"{slug}: <!-- INSERT:return_appendix --> 必须位于引用清单之前，确保附录与引用清单在文末收尾"
                )

    # 1. 正文引用闭环（支持行首 [引用N] 或 - [引用N]）
    used = sorted({int(x) for x in re.findall(r"\[引用(\d+)\]", before_ref)})
    after_ref_body = body[ref_m.start():] if ref_m else body
    listed = sorted({int(x) for x in re.findall(r"^\s*(?:[-*]\s*)?\[引用(\d+)\]", after_ref_body, flags=re.M)})
    if used != listed:
        problems.append(f"{slug}: 正文引用 {used} 与清单 {listed} 不一致")

    # 2. 股东回报节与附录检查
    if ret is not None:
        app_text = app or ""
        if "**回报引用清单**" in app_text:
            app_before_list, app_list_part = app_text.split("**回报引用清单**", 1)
        else:
            app_before_list, app_list_part = app_text, app_text
        rtxt = ret + "\n" + app_before_list
        rused = sorted({int(x) for x in re.findall(r"\[回报引用(\d+)\]", rtxt)})
        rlisted = sorted(
            {int(x) for x in re.findall(r"^\s*(?:[-*]\s*)?\[回报引用(\d+)\]", app_list_part, flags=re.M)}
        )
        if rused != rlisted:
            problems.append(f"{slug}: 回报正文引用 {rused} 与附录清单 {rlisted} 不一致")
        if require_return_markers:
            if "<!-- 第一节完成 -->" not in ret:
                problems.append(f"{slug}: 股东回报第一节没有完成标记 `<!-- 第一节完成 -->`")
            if app is None or "<!-- 完成 -->" not in app:
                problems.append(f"{slug}: 股东回报附录缺失或没有完成标记 `<!-- 完成 -->`")
        ret_lines = [l for l in ret.splitlines() if l.strip() and not l.strip().startswith("<!--")]
        if len(ret_lines) > 48:
            problems.append(f"{slug}: 第一节股东回报 {len(ret_lines)} 行，超过 48 行上限（目标 ≤40 行，含表格）")
        long_paras = [
            l for l in ret_lines if not l.lstrip().startswith(("|", "-", "*", "#")) and len(l.strip()) > 120
        ]
        if long_paras:
            problems.append(f"{slug}: 第一节股东回报有 {len(long_paras)} 段长段落，应为 bullet/表格")

    # 3. 禁用“你的判断”与句中 v3 引用
    if "你的判断" in body:
        problems.append(f"{slug}: 主稿出现「你的判断」{body.count('你的判断')} 处，应为「我的判断」")
    v3 = re.findall(r"\[引用\d+\]\s*[0-9]", before_ref)
    if v3:
        problems.append(f"{slug}: 疑似句中引用 {len(v3)} 处，例如 {v3[:3]}（引用应放句末）")

    # 4. 第二节：三个父 bullet，子 bullet 数量 2–4 / 2–4 / 1–3
    sec2 = section(body, "## 二、", "## 三、")
    if not sec2:
        problems.append(f"{slug}: 缺 `## 二、市场共识` 章节")
    else:
        for label, lo, hi in (("看多的人默认成立", 2, 4), ("看空的人默认成立", 2, 4), ("两边都不争的", 1, 3)):
            parents = re.findall(r"(?m)^[-*]\s*\*{0,2}" + re.escape(label) + r"\*{0,2}[：:][ \t]*(\S.*)?$", sec2)
            if len(parents) != 1:
                problems.append(f"{slug}: 第二节「{label}」应是一条父 bullet，现有 {len(parents)} 条")
                continue
            if parents[0]:
                problems.append(f"{slug}: 第二节「{label}」父 bullet 后面不该跟内容，条目要放到子 bullet")
            m = re.search(
                r"(?m)^[-*]\s*\*{0,2}" + re.escape(label) + r"\*{0,2}[：:].*$((?:\n[ \t]+[-*].*)*)", sec2
            )
            children = [l for l in (m.group(1) if m else "").splitlines() if l.strip()]
            if not lo <= len(children) <= hi:
                problems.append(f"{slug}: 第二节「{label}」有 {len(children)} 条子 bullet，应为 {lo} 至 {hi} 条")

    # 5. 第三节：每条 debate 五块齐全且均为 bullet
    sec3 = section(body, "## 三、", "## 四、")
    debates = re.split(r"(?m)^### Debate ", sec3)[1:] if sec3 else []
    if not debates:
        problems.append(f"{slug}: 第三节没有找到 `### Debate ` 小节")
    for k, deb in enumerate(debates, 1):
        for blk in DEBATE_BLOCKS:
            m = re.search(r"(?m)^(\s*[-*]\s*)?\*\*" + re.escape(blk), deb)
            if not m:
                problems.append(f"{slug}: Debate {k} 缺「{blk}」块")
            elif not m.group(1):
                problems.append(f"{slug}: Debate {k} 的「{blk}」不是 bullet")

    # 6. 第四节：回指必须写「→ Debate N 短名」
    sec4 = section(body, "## 四、", "## 五、")
    bad_ref = [
        l.strip()[:40] for l in sec4.splitlines() if "→" in l and not re.search(r"→\s*Debate\s*\d+", l)
    ]
    if bad_ref:
        problems.append(f"{slug}: 第四节有 {len(bad_ref)} 行回指没写「→ Debate N 短名」，例如 {bad_ref[:2]}")

    # 7. 第六节：行数 <= 12 行，且不出现“占净利润”
    sec6 = [
        l for l in section(body, "## 六、", "## 七、").splitlines() if l.strip() and not l.strip().startswith("<!--")
    ]
    if len(sec6) > 12:
        problems.append(f"{slug}: 第六节财务体检 {len(sec6)} 行，超过 12 行上限")
    if "占净利润" in body:
        problems.append(f"{slug}: 出现「占净利润」{body.count('占净利润')} 处，应写「相当于净利润的」")

    # 8. 第八节：一句话里不出现裸 Debate N
    one = section(body, "## 八、一句话", "## ")
    bare = re.findall(r"[Dd]ebate\s*\d+\s*(?:和|与|或|同时|成立|兑现)", one)
    if bare:
        problems.append(f"{slug}: 一句话里有裸 debate 编号写法 {bare}")

    return problems


def merge_files(
    main_text: str,
    ret_text: str | None,
    app_text: str | None,
    slug: str = "bullbear",
) -> tuple[str, list[str]]:
    """将 return.md 与 return_appendix.md 插入主稿的 INSERT 占位符，并执行全量格式检查。"""
    problems: list[str] = []
    merged = main_text
    for tag, txt, fallback in (
        ("<!-- INSERT:return -->", ret_text, "## 一、股东回报\n\n（支线未完成）\n"),
        ("<!-- INSERT:return_appendix -->", app_text, "## 九、股东回报附录\n\n（支线未完成）\n"),
    ):
        if tag not in merged:
            if tag == "<!-- INSERT:return -->" and RET_SEC_RE.search(merged):
                continue
            if tag == "<!-- INSERT:return_appendix -->" and APP_SEC_RE.search(merged):
                continue
            problems.append(f"{slug}: 主稿缺 {tag} 标记")
            continue
        if txt is None:
            problems.append(f"{slug}: 没有对应支线文件，{tag} 未能替换")
            merged = merged.replace(tag, fallback)
        else:
            clean_txt = re.sub(r"<!--.*?-->\n?", "", txt).strip() + "\n"
            merged = merged.replace(tag, clean_txt)
    problems.extend(check(slug, merged, ret_text, app_text))
    return merged, problems


def main() -> int:
    ap = argparse.ArgumentParser(description="Dalton Bull/Bear v4.2 格式检查与合并脚本")
    ap.add_argument("target", help="要检查的 Markdown 文件路径（如 main.md 或 drafts/YYYY-MM-DD-bullbear.md）")
    ap.add_argument("--return-file", "--return", dest="return_file", default=None, help="股东回报第一节文件路径（默认同目录 return.md）")
    ap.add_argument("--appendix-file", "--appendix", dest="appendix_file", default=None, help="股东回报附录文件路径（默认同目录 return_appendix.md）")
    ap.add_argument("--merge-out", default=None, help="可选：把主稿与回报支线合并后写到该路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON 格式结果")
    args = ap.parse_args()

    target_path = pathlib.Path(args.target)
    if not target_path.exists():
        msg = f"文件不存在: {target_path}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 1

    body = target_path.read_text(encoding="utf-8")
    slug = target_path.stem
    parent = target_path.parent

    ret_path = pathlib.Path(args.return_file) if args.return_file else (parent / "return.md")
    app_path = pathlib.Path(args.appendix_file) if args.appendix_file else (parent / "return_appendix.md")
    ret_text = ret_path.read_text(encoding="utf-8") if ret_path.exists() else None
    app_text = app_path.read_text(encoding="utf-8") if app_path.exists() else None

    if args.merge_out:
        merged, problems = merge_files(body, ret_text, app_text, slug=slug)
        out_path = pathlib.Path(args.merge_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(merged, encoding="utf-8")
        sha256 = hashlib.sha256(merged.encode("utf-8")).hexdigest()
    else:
        problems = check(slug, body, ret_text, app_text)
        out_path = target_path
        sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()

    ok = len(problems) == 0
    if args.json:
        print(
            json.dumps(
                {
                    "ok": ok,
                    "path": str(out_path),
                    "sha256": sha256,
                    "warn_count": len(problems),
                    "warnings": problems,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for p in problems:
            print("WARN", p)
        if ok:
            print(f"OK {out_path} (0 WARN, sha256={sha256[:12]})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
