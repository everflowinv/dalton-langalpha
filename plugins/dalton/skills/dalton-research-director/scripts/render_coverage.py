#!/usr/bin/env python3
"""Deterministic coverage.html renderer for Dalton Research Director (PK.19 / PK-UI.5).

Uses only Python stdlib (`html`, `json`, `sys`, `pathlib`) to render the JSON output of
`dalton_coverage_board` into a Quiet Workspace HTML file (`Home/dalton/coverage.html`).

Usage:
    python render_coverage.py board.json > dalton/coverage.html
    python render_coverage.py --in board.json --out dalton/coverage.html
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any


STATUS_DOT = {
    "intact": ("#16a34a", "稳固"),
    "confirm": ("#16a34a", "强化"),
    "THESIS_STRENGTHENED": ("#16a34a", "强化"),
    "NO_CHANGE": ("#64748b", "维持"),
    "warn": ("#d97706", "存疑"),
    "THESIS_WEAKENED": ("#d97706", "弱化"),
    "break": ("#dc2626", "证伪"),
    "THESIS_BROKEN": ("#dc2626", "证伪"),
    "NEW_THESIS": ("#2563eb", "新立论"),
}


def _esc(val: Any) -> str:
    return html.escape(str(val if val is not None else ""))


def render_coverage_html(board: dict[str, Any]) -> str:
    b_date = _esc(board.get("date") or "")
    tz = _esc(board.get("tz") or "Asia/Shanghai")
    budget = board.get("budget") or {}
    cap = float(budget.get("cap_usd") or budget.get("daily_cap_usd") or 0.0)
    spent = float(budget.get("spent_usd") or budget.get("spent_today_usd") or 0.0)
    rem = float(budget.get("remaining_usd") or 0.0)
    companies = board.get("companies") or []
    pending_approvals = board.get("pending_approvals") or []
    pm_handoffs = board.get("pm_handoffs") or []

    rows_html: list[str] = []
    for c in companies:
        ticker = _esc(c.get("ticker") or "")
        tier = _esc(str(c.get("tier") or "core").upper())
        hv = c.get("house_view") or {}
        stance = _esc(hv.get("stance") or c.get("stance") or "-")
        conviction = _esc(hv.get("conviction") or c.get("conviction") or "-")
        one_liner = _esc(c.get("one_liner") or "")
        lv = c.get("last_verdict") or {}
        v_word = str(lv.get("word") or lv.get("verdict") or c.get("aggregate_status") or "intact")
        dot_color, dot_label = STATUS_DOT.get(v_word, ("#64748b", _esc(v_word)))
        v_date = _esc(lv.get("event_date") or lv.get("at") or "-")

        next_ev = (c.get("next_events") or [None])[0] or c.get("next_catalyst") or {}
        if isinstance(next_ev, dict) and (next_ev.get("title") or next_ev.get("type")):
            ev_title = _esc(next_ev.get("title") or next_ev.get("type"))
            ev_date = _esc(next_ev.get("date") or next_ev.get("expected_date") or "")
            ev_status = _esc(next_ev.get("date_status") or "")
            cat_str = f"{ev_title} ({ev_date})" + (f" [{ev_status}]" if ev_status == "待确认" else "")
        else:
            cat_str = "-"

        lp = c.get("last_publication") or {}
        pub_str = _esc(lp.get("title") or "-") if isinstance(lp, dict) else "-"
        rel_link = f"../{ticker}/thesis.md"

        rows_html.append(
            f"""<tr>
  <td><strong><a href="{rel_link}">{ticker}</a></strong> <span class="badge">{tier}</span></td>
  <td><span class="dot" style="background:{dot_color}"></span>{dot_label} · {stance}/{conviction}</td>
  <td>{one_liner}</td>
  <td><code>{_esc(v_word)}</code> <span class="muted">({v_date})</span></td>
  <td>{cat_str}</td>
  <td>{pub_str}</td>
</tr>"""
        )

    pm_items_html: list[str] = []
    for req in pending_approvals:
        t = _esc(req.get("ticker") or "")
        title = _esc(req.get("title") or "")
        thread_id = _esc(req.get("thread_id") or "")
        pm_items_html.append(
            f"<li><strong>[{t} 待批发布]</strong> {title} "
            f'(线程: <a href="/chat/t/{thread_id}"><code>{thread_id[:8]}</code></a>)</li>'
        )
    for ho in pm_handoffs:
        t = _esc(ho.get("ticker") or "")
        what = _esc(ho.get("what") or "")
        pm_items_html.append(f"<li><strong>[{t} 待决事项]</strong> {what}</li>")

    pm_block = (
        "<ul>\n" + "\n".join(pm_items_html) + "\n</ul>"
        if pm_items_html
        else '<p class="muted">当前无待批发布或等待 PM 裁决的事项。</p>'
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>Dalton 覆盖总览 ({b_date})</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #fafafa;
    --card: #ffffff;
    --text: #18181b;
    --muted: #71717a;
    --border: #e4e4e7;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #121214;
      --card: #18181b;
      --text: #f4f4f5;
      --muted: #a1a1aa;
      --border: #27272a;
    }}
  }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Inter", "PingFang SC", sans-serif;
    background: var(--bg);
    color: var(--text);
    margin: 0;
    padding: 24px 32px;
    line-height: 1.5;
  }}
  header {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    border-bottom: 1px solid var(--border);
    padding-bottom: 12px;
    margin-bottom: 20px;
  }}
  h1 {{ font-size: 1.25rem; margin: 0; font-weight: 600; }}
  h2 {{ font-size: 0.95rem; margin: 20px 0 8px; font-weight: 600; }}
  .summary-bar {{
    display: flex;
    gap: 24px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 16px;
    font-size: 0.875rem;
  }}
  .muted {{ color: var(--muted); font-size: 0.8125rem; }}
  .badge {{
    font-size: 0.7rem;
    padding: 1px 6px;
    border-radius: 4px;
    border: 1px solid var(--border);
    color: var(--muted);
  }}
  .dot {{
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 6px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    font-size: 0.875rem;
  }}
  th, td {{
    text-align: left;
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
  }}
  th {{
    font-weight: 500;
    color: var(--muted);
    font-size: 0.78rem;
  }}
  a {{ color: inherit; text-decoration: underline; text-underline-offset: 2px; }}
</style>
</head>
<body>
  <header>
    <h1>Dalton 覆盖总览</h1>
    <span class="muted">日期：{b_date} ({tz}) · 覆盖公司：{len(companies)} 家</span>
  </header>
  <div class="summary-bar">
    <div><strong>今日花费 / 日预算：</strong> ${spent:.2f} / ${cap:.2f}</div>
    <div><strong>剩余可用额度：</strong> ${rem:.2f}</div>
    <div><strong>待 PM 审批发布：</strong> {len(pending_approvals)} 篇</div>
    <div><strong>待 PM 裁决事项：</strong> {len(pm_handoffs)} 项</div>
  </div>

  <h2>等你处理（PM Attention）</h2>
  {pm_block}

  <h2>覆盖公司状态一览</h2>
  <table>
    <thead>
      <tr>
        <th>公司</th>
        <th>状态 / 观点</th>
        <th>一句话判断（House View）</th>
        <th>最近结论</th>
        <th>下一个催化剂</th>
        <th>最近正式发布</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows_html) if rows_html else '<tr><td colspan="6" class="muted">暂无覆盖公司数据</td></tr>'}
    </tbody>
  </table>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Render Dalton coverage board JSON to coverage.html.")
    parser.add_argument("input_pos", nargs="?", default=None, help="Optional positional input JSON file")
    parser.add_argument("--in", dest="in_file", default=None, help="Input board JSON file (defaults to stdin)")
    parser.add_argument("--out", dest="out_file", default=None, help="Output HTML file (defaults to stdout)")
    args = parser.parse_args()

    src_path = args.in_file or args.input_pos
    if src_path:
        raw = Path(src_path).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()

    board = json.loads(raw) if raw.strip() else {}
    html_out = render_coverage_html(board)

    if args.out_file:
        out_p = Path(args.out_file)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(html_out, encoding="utf-8")
    else:
        sys.stdout.write(html_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
