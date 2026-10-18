#!/usr/bin/env python3
"""
predictions.py — Structured earnings predictions CLI for Dalton earnings preview (PS.8).

Manages `predictions.json` (used by `dalton-earnings-preview` at T-3, checked in
`dalton-earnings-flash` at T0, and reconciled by `dalton-earnings-deep/scripts/reconcile.py` at T+1).

Required fields per prediction item (PS.8 line 1190):
  - id              : Unique identifier (e.g. "P1", "rev-fy26q3")
  - metric          : Metric name (e.g. "Total Revenue", "Non-GAAP Diluted EPS", "FY26Q4 Revenue Guidance")
  - period          : Fiscal period label (e.g. "FY26Q3", "FY26Q4E", "FY26")
  - basis           : Accounting/operational basis & unit (e.g. "GAAP, USD Millions", "Non-GAAP, USD/share")
  - our_expectation : Our expected value, range, or directional view (e.g. "40,500 - 41,200", "> 5.35")
  - street          : Street consensus value + snapshot vintage (e.g. "40,180 (consensus 2026-10-23)")
  - bogey           : Buyside bogey if sourced, or explicit "未找到" / null
  - why             : Brief mechanism/driver reasoning behind our expectation
  - verify_how      : Exact line item / exhibit / call section to verify when earnings release drops
  - linked_debate   : Linked debate ID or title from debate.md / preview Section 3
  - linked_pillar   : Linked thesis pillar ID or name from thesis.md

Subcommands:
  write    : Validate and atomically write predictions.json from --input, --stdin, or --item.
  read     : Read predictions.json and output JSON or Markdown checklist table.
  validate : Validate required fields, uniqueness, and optional minimum category coverage.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys


REQUIRED_FIELDS = (
    "id",
    "metric",
    "period",
    "basis",
    "our_expectation",
    "street",
    "bogey",
    "why",
    "verify_how",
    "linked_debate",
    "linked_pillar",
)

NON_EMPTY_FIELDS = (
    "id",
    "metric",
    "period",
    "basis",
    "our_expectation",
    "street",
    "why",
    "verify_how",
    "linked_debate",
    "linked_pillar",
)


def _normalize_payload(raw: dict | list, args: argparse.Namespace | None = None) -> dict:
    """Normalize input into a standard top-level envelope containing `predictions: [...]`."""
    now_iso = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    if isinstance(raw, list):
        envelope = {
            "schema_version": 1,
            "ticker": getattr(args, "ticker", None),
            "quarter": getattr(args, "quarter", None),
            "as_of": getattr(args, "as_of", None) or now_iso,
            "updated_at": now_iso,
            "predictions": raw,
        }
    elif isinstance(raw, dict):
        preds = raw.get("predictions")
        if preds is None and all(k in raw for k in ("id", "metric")):
            preds = [raw]
        elif preds is None:
            preds = []
        envelope = {
            "schema_version": raw.get("schema_version", 1),
            "ticker": getattr(args, "ticker", None) or raw.get("ticker"),
            "quarter": getattr(args, "quarter", None) or raw.get("quarter"),
            "as_of": getattr(args, "as_of", None) or raw.get("as_of") or now_iso,
            "updated_at": now_iso,
            "predictions": preds,
        }
    else:
        raise ValueError("Input JSON must be an object with 'predictions' or a list of prediction items.")
    return envelope


def validate_predictions(envelope: dict, strict: bool = False) -> dict:
    """Validate prediction items and return {ok, errors, warnings, count, coverage}."""
    errors: list[str] = []
    warnings: list[str] = []
    preds = envelope.get("predictions")
    if not isinstance(preds, list) or len(preds) == 0:
        return {
            "ok": False,
            "count": 0,
            "errors": ["'predictions' must be a non-empty list of prediction items."],
            "warnings": [],
            "coverage": {},
        }

    seen_ids: set[str] = set()
    for idx, item in enumerate(preds):
        prefix = f"predictions[{idx}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}: item must be a JSON object")
            continue

        missing_keys = [k for k in REQUIRED_FIELDS if k not in item]
        if missing_keys:
            errors.append(f"{prefix} (id={item.get('id', '?')}): missing required keys: {', '.join(missing_keys)}")

        for k in NON_EMPTY_FIELDS:
            val = item.get(k)
            if val is None or (isinstance(val, str) and not val.strip()):
                errors.append(f"{prefix} (id={item.get('id', '?')}): field '{k}' must be non-empty")

        pid = str(item.get("id") or "").strip()
        if pid:
            if pid in seen_ids:
                errors.append(f"{prefix}: duplicate prediction id '{pid}'")
            seen_ids.add(pid)

    # Check PS.8 minimum coverage heuristics (revenue, EPS/profit, guidance, KPIs)
    metrics_lower = " | ".join(
        f"{p.get('metric', '')} {p.get('basis', '')} {p.get('id', '')}".lower()
        for p in preds
        if isinstance(p, dict)
    )
    has_revenue = any(w in metrics_lower for w in ("revenue", "营收", "收入", "sales", "top line"))
    has_eps_or_profit = any(
        w in metrics_lower
        for w in ("eps", "利润", "ebit", "ebitda", "net income", "operating income", "margin", "毛利")
    )
    has_guidance = any(w in metrics_lower for w in ("guidance", "guide", "指引", "展望", "outlook"))
    coverage = {
        "total_items": len(preds),
        "has_revenue": has_revenue,
        "has_eps_or_profit": has_eps_or_profit,
        "has_guidance": has_guidance,
        "min_5_items": len(preds) >= 5,
    }

    if not has_revenue:
        msg = "Coverage check: no obvious Revenue (营收/收入) prediction found."
        (errors if strict else warnings).append(msg)
    if not has_eps_or_profit:
        msg = "Coverage check: no obvious EPS or core profit metric found."
        (errors if strict else warnings).append(msg)
    if not has_guidance:
        msg = "Coverage check: no obvious Guidance (指引/展望) prediction found."
        (errors if strict else warnings).append(msg)
    if len(preds) < 5:
        msg = f"Coverage check: PS.8 requires at least Revenue, EPS, Guidance, 2-4 KPIs, and debates (got {len(preds)} items, recommended >= 5)."
        (errors if strict else warnings).append(msg)

    return {
        "ok": len(errors) == 0,
        "count": len(preds),
        "errors": errors,
        "warnings": warnings,
        "coverage": coverage,
    }


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _format_expectation(val) -> str:
    if isinstance(val, dict):
        if "value" in val:
            return str(val["value"])
        if "low" in val and "high" in val:
            return f"{val['low']} - {val['high']}"
        return json.dumps(val, ensure_ascii=False)
    if val is None:
        return "未找到"
    return str(val)


def render_markdown_checklist(envelope: dict) -> str:
    preds = envelope.get("predictions") or []
    lines = [
        "| ID | 指标（期间｜口径） | 我们的预期 | 卖方一致预期 | 买方门槛（Bogey） | 在哪看（Verify How） | 关联 Debate / 支柱 |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in preds:
        pid = p.get("id", "")
        metric_col = f"{p.get('metric', '')}（{p.get('period', '')}｜{p.get('basis', '')}）"
        our_exp = _format_expectation(p.get("our_expectation"))
        street = _format_expectation(p.get("street"))
        bogey = _format_expectation(p.get("bogey")) if p.get("bogey") not in (None, "") else "未找到"
        verify = str(p.get("verify_how", "")).replace("|", "｜")
        links = f"{p.get('linked_debate', '-')} / {p.get('linked_pillar', '-')}"
        lines.append(f"| {pid} | {metric_col} | {our_exp} | {street} | {bogey} | {verify} | {links} |")
    return "\n".join(lines)


def cmd_write(args: argparse.Namespace) -> int:
    target = Path(args.file)
    raw_data = None

    if args.input:
        raw_data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    elif args.stdin:
        raw_data = json.loads(sys.stdin.read())
    elif args.item:
        items = [json.loads(x) for x in args.item]
        if args.append and target.exists():
            existing = json.loads(target.read_text(encoding="utf-8"))
            existing_preds = existing.get("predictions", existing if isinstance(existing, list) else [])
            existing_by_id = {p.get("id"): i for i, p in enumerate(existing_preds) if isinstance(p, dict)}
            for it in items:
                if it.get("id") in existing_by_id:
                    existing_preds[existing_by_id[it["id"]]] = it
                else:
                    existing_preds.append(it)
            raw_data = existing
            if isinstance(raw_data, dict):
                raw_data["predictions"] = existing_preds
        else:
            raw_data = items
    else:
        raise ValueError("Provide prediction data via --input <file>, --stdin, or one or more --item '<json>'.")

    envelope = _normalize_payload(raw_data, args)
    val = validate_predictions(envelope, strict=args.strict)
    if not val["ok"]:
        out = {"ok": False, "file": str(target), **val}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    _atomic_write_json(target, envelope)
    out = {
        "ok": True,
        "file": str(target),
        "count": val["count"],
        "warnings": val["warnings"],
        "coverage": val["coverage"],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    target = Path(args.file)
    if not target.exists():
        print(json.dumps({"ok": False, "error": f"File not found: {target}"}, ensure_ascii=False, indent=2))
        return 1
    raw = json.loads(target.read_text(encoding="utf-8"))
    envelope = _normalize_payload(raw)
    if args.markdown:
        print(render_markdown_checklist(envelope))
        return 0
    print(json.dumps({"ok": True, "file": str(target), **envelope}, ensure_ascii=False, indent=2))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    target = Path(args.file)
    if not target.exists():
        print(json.dumps({"ok": False, "error": f"File not found: {target}"}, ensure_ascii=False, indent=2))
        return 1
    raw = json.loads(target.read_text(encoding="utf-8"))
    envelope = _normalize_payload(raw)
    val = validate_predictions(envelope, strict=args.strict)
    out = {"ok": val["ok"], "file": str(target), **val}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if val["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Dalton earnings preview predictions.json CLI (PS.8)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_w = sub.add_parser("write", help="Validate and write predictions.json")
    sp_w.add_argument("--file", "-f", required=True, help="Target predictions.json path")
    sp_w.add_argument("--input", "-i", default=None, help="Input JSON file containing predictions")
    sp_w.add_argument("--stdin", action="store_true", help="Read JSON payload from stdin")
    sp_w.add_argument("--item", action="append", default=None, help="Inline JSON prediction object (repeatable)")
    sp_w.add_argument("--append", action="store_true", help="Merge/upsert --item entries into existing file")
    sp_w.add_argument("--ticker", default=None, help="Company ticker")
    sp_w.add_argument("--quarter", default=None, help="Fiscal quarter label (e.g. FY26Q3)")
    sp_w.add_argument("--as-of", dest="as_of", default=None, help="Freeze clock as-of timestamp")
    sp_w.add_argument("--strict", action="store_true", help="Enforce minimum category coverage (revenue, EPS, guidance, >=5 items)")
    sp_w.set_defaults(fn=cmd_write)

    sp_r = sub.add_parser("read", help="Read predictions.json and output JSON or Markdown checklist")
    sp_r.add_argument("--file", "-f", required=True, help="Path to predictions.json")
    sp_r.add_argument("--markdown", "--md", action="store_true", help="Render Markdown table for Section 五 of preview.md")
    sp_r.set_defaults(fn=cmd_read)

    sp_v = sub.add_parser("validate", help="Validate schema and required fields of predictions.json")
    sp_v.add_argument("--file", "-f", required=True, help="Path to predictions.json")
    sp_v.add_argument("--strict", action="store_true", help="Enforce minimum category coverage")
    sp_v.set_defaults(fn=cmd_validate)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
