#!/usr/bin/env python3
"""Dalton Full Coverage — FACTS-LEDGER upsert, query, and completeness checker."""
import argparse
import datetime as dt
import json
import pathlib
import re
import sys

FACTS_OPEN = "<!-- FACTS-LEDGER"
FACTS_CLOSE = "-->"

CORE_SCALAR_KEYS = (
    "price",
    "market_cap_usd",
    "shares_diluted",
    "net_cash_basic",
    "net_cash_strict",
    "hidden_asset_net",
    "return_r1",
    "return_r2",
    "return_r3",
    "return_score",
    "qoe_sloan",
    "qoe_cfo_ni",
    "qoe_ccc",
)

QUARTER_PREFIXES = ("rev", "gp", "op", "eps")
ANNUAL_PREFIXES = ("fcf", "sbc")


def render_ledger_comment(rows: list) -> str:
    payload = {"ledger": rows}
    return f"{FACTS_OPEN}\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n{FACTS_CLOSE}"


def render_markdown_table(rows: list) -> str:
    lines = [
        "| key | 指标 | 数值 | 期间 | 来源 |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r.get('key', '')} | {r.get('label', '')} | {r.get('value', '')} | {r.get('period', '')} | {r.get('source', '')} |"
        )
    return "\n".join(lines)


def parse_ledger(text: str) -> list:
    m = re.search(re.escape(FACTS_OPEN) + r"(.*?)" + re.escape(FACTS_CLOSE), text, flags=re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1).strip())
        return data.get("ledger", []) if isinstance(data, dict) else []
    except Exception:
        return []


def build_default_facts_doc(rows: list, title: str = "目标公司数字底表（FACTS-LEDGER）") -> str:
    now_str = dt.datetime.now().isoformat(timespec="minutes")
    return "\n".join([
        f"# {title}",
        "",
        f"更新时间：{now_str}",
        "",
        "> 硬规则：完整覆盖后续所有阶段（问题清单、A/B/C/D 取证、Excel 建模、三段式备忘录、审稿与发布核验）引用目标公司数字只能来自本底表。缺项必须写“底表缺 X”并回到阶段 0 用 `facts.py set` 补齐。唯一例外是同业可比公司数字（由 A 支线现取并注明期间与来源）。",
        "",
        "## 一、机器可读账本",
        "",
        render_ledger_comment(rows),
        "",
        render_markdown_table(rows),
        "",
    ])


def update_facts_file(path: pathlib.Path, new_rows: list, title: str = None) -> list:
    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
    old_rows = parse_ledger(existing_text)
    by_key = {r["key"]: r for r in old_rows if isinstance(r, dict) and "key" in r}
    for r in new_rows:
        by_key[r["key"]] = r
    rows = list(by_key.values())

    if not existing_text.strip():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(build_default_facts_doc(rows, title=title or "目标公司数字底表（FACTS-LEDGER）"), encoding="utf-8")
        return rows

    new_block = render_ledger_comment(rows) + "\n\n" + render_markdown_table(rows)
    pattern = re.compile(
        re.escape(FACTS_OPEN) + r".*?" + re.escape(FACTS_CLOSE) + r"(?:\s*\|[^\n]*\n)+",
        flags=re.S,
    )
    if pattern.search(existing_text):
        updated = pattern.sub(new_block + "\n", existing_text, count=1)
    elif FACTS_OPEN in existing_text:
        pattern_comment = re.compile(re.escape(FACTS_OPEN) + r".*?" + re.escape(FACTS_CLOSE), flags=re.S)
        updated = pattern_comment.sub(new_block, existing_text, count=1)
    else:
        updated = existing_text.rstrip() + "\n\n## 一、机器可读账本\n\n" + new_block + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    return rows


def check_completeness(rows: list) -> dict:
    by_key = {r.get("key"): r for r in rows if isinstance(r, dict) and r.get("key")}
    missing_scalars = [k for k in CORE_SCALAR_KEYS if k not in by_key or not str(by_key[k].get("value", "")).strip()]

    quarter_counts = {}
    for prefix in QUARTER_PREFIXES:
        matched = [
            k for k, v in by_key.items()
            if (k.startswith(f"{prefix}_q") or k.startswith(f"{prefix}_Q") or re.match(rf"^{prefix}_\d{{4}}Q[1-4]$", k, re.I))
            and str(v.get("value", "")).strip()
        ]
        quarter_counts[prefix] = len(matched)

    annual_counts = {}
    for prefix in ANNUAL_PREFIXES:
        matched = [
            k for k, v in by_key.items()
            if (k.startswith(f"{prefix}_fy") or k.startswith(f"{prefix}_FY") or re.match(rf"^{prefix}_(?:FY)?\d{{4}}$", k, re.I))
            and str(v.get("value", "")).strip()
        ]
        annual_counts[prefix] = len(matched)

    missing_series = []
    for prefix, cnt in quarter_counts.items():
        if cnt < 8:
            missing_series.append(f"{prefix}_q1..q8 (当前 {cnt}/8 季)")
    for prefix, cnt in annual_counts.items():
        if cnt < 3:
            missing_series.append(f"{prefix}_fy1..fy3 (当前 {cnt}/3 年)")

    missing_meta = [
        k for k, v in by_key.items()
        if not str(v.get("period", "")).strip() or not str(v.get("source", "")).strip()
    ]

    ok = not missing_scalars and not missing_series and not missing_meta
    return {
        "ok": ok,
        "total_keys": len(by_key),
        "missing_scalars": missing_scalars,
        "quarter_counts": quarter_counts,
        "annual_counts": annual_counts,
        "missing_series": missing_series,
        "missing_period_or_source": missing_meta,
    }


def resolve_file_arg(args) -> pathlib.Path:
    f = getattr(args, "file", None) or getattr(args, "facts", None) or getattr(args, "file_pos", None) or "facts.md"
    return pathlib.Path(f)


def cmd_set(args):
    path = resolve_file_arg(args)
    row = {
        "key": args.key,
        "label": args.label or args.key,
        "value": str(args.value),
        "period": args.period or "",
        "source": args.source or "",
    }
    rows = update_facts_file(path, [row], title=args.title)
    payload = {"ok": True, "path": str(path), "updated": row, "total_keys": len(rows)}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"OK: [{row['key']}] = {row['value']} ({row['period']} | {row['source']}) -> {path} (total {len(rows)} keys)")


def cmd_get(args):
    path = resolve_file_arg(args)
    if not path.exists():
        msg = f"底表文件不存在: {path}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg, "path": str(path)}, ensure_ascii=False, indent=2))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        sys.exit(1)

    rows = parse_ledger(path.read_text(encoding="utf-8"))
    if args.key:
        matched = [r for r in rows if r.get("key") == args.key]
        if not matched:
            msg = f"底表缺 {args.key}"
            if args.json:
                print(json.dumps({"ok": False, "error": msg, "key": args.key, "path": str(path)}, ensure_ascii=False, indent=2))
            else:
                print(f"ERROR: {msg}", file=sys.stderr)
            sys.exit(1)
        row = matched[0]
        if args.json:
            print(json.dumps({"ok": True, "path": str(path), "row": row}, ensure_ascii=False, indent=2))
        else:
            print(f"{row.get('key')}\t{row.get('label')}\t{row.get('value')}\t{row.get('period')}\t{row.get('source')}")
        return

    if args.json:
        print(json.dumps({"ok": True, "path": str(path), "ledger": rows, "count": len(rows)}, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            print(f"{r.get('key')}\t{r.get('label')}\t{r.get('value')}\t{r.get('period')}\t{r.get('source')}")


def cmd_check(args):
    path = resolve_file_arg(args)
    if not path.exists():
        msg = f"底表文件不存在: {path}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg, "path": str(path)}, ensure_ascii=False, indent=2))
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
        sys.exit(1)

    rows = parse_ledger(path.read_text(encoding="utf-8"))
    res = check_completeness(rows)
    res["path"] = str(path)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"FACTS-LEDGER check: {'PASS' if res['ok'] else 'FAIL'} ({res['total_keys']} keys in {path})")
        if res["missing_scalars"]:
            print("  缺核心标量键:", ", ".join(res["missing_scalars"]))
        if res["missing_series"]:
            print("  缺序列键:", "；".join(res["missing_series"]))
        if res["missing_period_or_source"]:
            print("  缺 period 或 source 的键:", ", ".join(res["missing_period_or_source"]))
    sys.exit(0 if res["ok"] else 1)


def main():
    parser = argparse.ArgumentParser(description="Dalton FACTS-LEDGER 底表读写与完整性检查工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_set = sub.add_parser("set", help="写入或更新单条底表记录（upsert by key）")
    p_set.add_argument("file_pos", nargs="?", default=None, help="facts.md 路径（可选位置参数）")
    p_set.add_argument("--file", "--facts", dest="file", default=None, help="facts.md 路径（默认 ./facts.md）")
    p_set.add_argument("--key", required=True, help="标准键名，如 price / net_cash_strict / rev_q1")
    p_set.add_argument("--label", default=None, help="中文指标名称")
    p_set.add_argument("--value", required=True, help="指标数值（含单位或标准格式）")
    p_set.add_argument("--period", default="", help="会计期间或时点，如 FY2025 / 2025Q4 / 2026-04-17")
    p_set.add_argument("--source", default="", help="可追溯一手来源，如 FY2025 10-K Item 8 p.62")
    p_set.add_argument("--title", default=None, help="新建文件时的标题")
    p_set.add_argument("--json", action="store_true", help="输出 JSON")
    p_set.set_defaults(func=cmd_set)

    p_get = sub.add_parser("get", help="读取底表全部记录或按 key 查询单条记录")
    p_get.add_argument("file_pos", nargs="?", default=None, help="facts.md 路径（可选位置参数）")
    p_get.add_argument("--file", "--facts", dest="file", default=None, help="facts.md 路径（默认 ./facts.md）")
    p_get.add_argument("--key", default=None, help="按 key 精确查询")
    p_get.add_argument("--json", action="store_true", help="输出 JSON")
    p_get.set_defaults(func=cmd_get)

    p_chk = sub.add_parser("check", help="检查必填键（标量键 + 8季利润表 + 3年FCF/SBC）是否齐全")
    p_chk.add_argument("file_pos", nargs="?", default=None, help="facts.md 路径（可选位置参数）")
    p_chk.add_argument("--file", "--facts", dest="file", default=None, help="facts.md 路径（默认 ./facts.md）")
    p_chk.add_argument("--json", action="store_true", help="输出 JSON")
    p_chk.set_defaults(func=cmd_check)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
