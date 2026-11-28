#!/usr/bin/env python3
"""FDE 招聘周报 → Discord 摘要。

先让 fde-hiring-tracker 出一份完整 markdown 报告落盘，再用同一份快照的 JSON
拼一段适合 Discord 的纯文本摘要（bullet 列表，不用表格）打到 stdout，
由 cron 的 announce 投递。stderr 交给调用方重定向，避免污染投递正文。
"""
import argparse
import json
import os
import subprocess
import sys

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(WS, "skills", "fde-hiring-tracker", "scripts", "run.sh")


def run_cli(args):
    p = subprocess.run(["bash", RUN] + args, cwd=WS, capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        raise SystemExit(f"run.sh {' '.join(args)} 失败，退出码 {p.returncode}")
    sys.stderr.write(p.stderr)
    return p.stdout


def pct(x):
    return "-" if x is None else f"{x * 100:.1f}%"


def delta_str(d):
    if d is None:
        return ""
    if d == 0:
        return "（持平）"
    return f"（{d:+d}）"


def build(payload, md_path, digest_top):
    t = payload["totals"]
    date = payload["date"]
    base = payload.get("compare_to")
    L = []
    head = f"📈 **FDE 招聘跟踪 · {date}**"
    head += f"（对比 {base}）" if base else "（首次快照，无基准）"
    L.append(head)
    L.append("")
    L.append(
        f"{t['companies_polled']} 家在跟，{t['companies_hiring_fde']} 家在招，"
        f"合计 {t['fde_reqs']} 个 req / {t['fde_postings']} 条 posting。"
    )

    movers = payload.get("movers") or []
    if base:
        ups = [m for m in movers if m["delta"] > 0][:6]
        downs = [m for m in movers if m["delta"] < 0][:6]
        L.append("")
        L.append("**本周增减**")
        if not ups and not downs:
            L.append("• 无变化")
        for m in ups + downs:
            L.append(f"• {m['name']} {m['delta']:+d} → {m['reqs']} req")

        ent = payload.get("new_entrants") or []
        newly = payload.get("newly_tracked") or []
        ex = payload.get("exits") or []
        if ent:
            L.append("")
            L.append("**新开 FDE 岗（此前为 0）**")
            for r in ent[:8]:
                L.append(f"• {r['name']} {r['reqs']} req")
        if newly:
            L.append("")
            L.append("**新纳入跟踪**：" + "、".join(f"{r['name']} {r['reqs']} req" for r in newly[:10]))
        if ex:
            L.append("")
            L.append("**清零退出**：" + "、".join(r["name"] for r in ex[:10]))

    hiring = payload.get("hiring") or []
    L.append("")
    L.append(f"**req 数 Top {digest_top}**")
    for r in hiring[:digest_top]:
        L.append(f"• {r['name']} {r['reqs']} req / {r['postings']} posting{delta_str(r.get('delta'))}")

    share = payload.get("share") or []
    L.append("")
    L.append(f"**FDE 占在招岗位比例 Top {digest_top}**（posting 口径）")
    for r in share[:digest_top]:
        basis = "" if r.get("total_basis") == "exact" else f"，{r.get('total_basis')} 口径为下限"
        L.append(f"• {r['name']} {pct(r['share'])}（{r['postings']}/{r['total_postings']}{basis}）")

    fails = payload.get("failures") or []
    trunc = [r for r in hiring if r.get("truncated")]
    pend = payload.get("pending_tier1b") or []
    notes = []
    if fails:
        notes.append(f"{len(fails)} 个源抓取失败：" + "、".join(f.get("name", "?") for f in fails[:5]))
    if trunc:
        notes.append("翻页未收敛：" + "、".join(r["name"] for r in trunc[:5]))
    if pend:
        notes.append(
            f"{sum(r['pending_1b'] for r in pend)} 条歧义标题待裁定（`run.sh review`）"
        )
    if notes:
        L.append("")
        L.append("**口径提醒**")
        for n in notes:
            L.append(f"• {n}")

    L.append("")
    L.append(f"报告全文：`{md_path}`")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="markdown 报告落盘路径")
    ap.add_argument("--lookback", type=int, default=7)
    ap.add_argument("--top", type=int, default=60, help="markdown 报告的榜单长度")
    ap.add_argument("--digest-top", type=int, default=10, help="Discord 摘要的榜单长度")
    a = ap.parse_args()

    os.makedirs(os.path.dirname(os.path.join(WS, a.out)) or ".", exist_ok=True)
    run_cli(["report", "--lookback", str(a.lookback), "--top", str(a.top), "--out", a.out])
    raw = run_cli(["report", "--lookback", str(a.lookback), "--format", "json"])
    payload = json.loads(raw)
    print(build(payload, a.out, a.digest_top))


if __name__ == "__main__":
    main()
