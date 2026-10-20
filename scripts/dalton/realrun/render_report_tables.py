#!/usr/bin/env python3
"""Render deterministic evidence summary tables from raw JSON files under `var/dalton/` (P0J §1.6, L42).

Usage:
  1. Render generated block to stdout:
     `uv run python scripts/dalton/realrun/render_report_tables.py [--evidence-dir DIR]`
  2. Update `<!-- generated:evidence-table --> ... <!-- /generated -->` in a Markdown file:
     `uv run python scripts/dalton/realrun/render_report_tables.py --update docs/dalton/dev-guide/P0J-round4-dev-report.md`
  3. Reproduce the R4 5-row discrepancy on the legacy P0I report:
     `uv run python scripts/dalton/realrun/render_report_tables.py --compare-legacy-report docs/dalton/dev-guide/P0I-round3-dev-report.md`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]

GENERATED_START_MARKER = "<!-- generated:evidence-table -->"
GENERATED_END_MARKER = "<!-- /generated -->"

FAMILY_SPECS: list[tuple[str, str, str]] = [
    ("G1 预算", "scripts/dalton/realrun/g1_budget.py", "g1_budget.json"),
    ("G2 接口与探测", "scripts/dalton/realrun/g2_providers.py", "g2_providers.json"),
    ("G3 发布核验", "scripts/dalton/realrun/g3_publish.py", "g3_publish.json"),
    ("G4 自主议程", "scripts/dalton/realrun/g4_autonomy.py", "g4_autonomy.json"),
    ("G5.1 Broker", "scripts/dalton/realrun/g5_broker_and_antigravity.py", "g5_1_broker.json"),
    ("G5.2 Antigravity", "scripts/dalton/realrun/g5_broker_and_antigravity.py", "g5_2_antigravity.json"),
    ("G6 财务镜头", "scripts/dalton/realrun/g6_lenses.py", "g6_lenses.json"),
    ("G7 宿主机桥", "scripts/dalton/realrun/g7_bridge.py", "g7_bridge.json"),
]


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_default_evidence_dir(repo_root: Path = REPO_ROOT) -> Path:
    candidates = [
        repo_root / "var" / "dalton" / "realrun",
        repo_root / "var" / "dalton" / "trial-runs" / "realrun-20261019",
    ]
    for cand in candidates:
        if cand.is_dir() and any(cand.glob("*.json")):
            return cand
    return candidates[-1]


def _find_evidence_file(evidence_dir: Path, filename: str) -> Path | None:
    exact = evidence_dir / filename
    if exact.is_file():
        return exact
    if filename in ("g5_1_broker.json", "g5_2_antigravity.json"):
        legacy = evidence_dir / "g5_broker_and_antigravity.json"
        if legacy.is_file():
            return legacy
    stem = Path(filename).stem
    matches = sorted(evidence_dir.glob(f"{stem}*.json"))
    return matches[-1] if matches else None


def render_evidence_table_block(
    evidence_dir: str | Path | None = None,
    *,
    repo_root: Path = REPO_ROOT,
) -> str:
    """Render the deterministic `<!-- generated:evidence-table --> ... <!-- /generated -->` block."""
    ev_dir = Path(evidence_dir) if evidence_dir is not None else resolve_default_evidence_dir(repo_root)
    if not ev_dir.is_absolute():
        ev_dir = repo_root / ev_dir

    lines: list[str] = [
        GENERATED_START_MARKER,
        "| 族 | 入库脚本 | 原始证据路径 | 原始证据 `sha256` | `git_sha` | `test_user_id` | 新调用 / 回放 (`fresh` / `replayed`) | 对话轮次 (`turns`) | 合计现金花费 (`combined_cash_usd`) | 名义花费 (`notional_usd`) | `valid` |",
        "|---|---|---|---|---|---|---|---:|---:|---:|---|",
    ]

    g3_payload: dict[str, Any] | None = None

    for family_label, script_rel, json_name in FAMILY_SPECS:
        f_path = _find_evidence_file(ev_dir, json_name)
        if f_path is None or not f_path.is_file():
            continue
        sha = compute_sha256(f_path)
        data = json.loads(f_path.read_text(encoding="utf-8"))
        if json_name == "g3_publish.json":
            g3_payload = data
        try:
            rel_path = f_path.resolve().relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            rel_path = f_path.as_posix()

        totals = (data.get("journal") or {}).get("totals") or {}
        fresh = int(totals.get("fresh_calls") or 0)
        replayed = int(totals.get("replayed_calls") or 0)
        turns = int(totals.get("conversation_turns") or 0)
        cash = float(totals.get("combined_cash_usd") or 0.0)
        notional = float(totals.get("notional_usd") or 0.0)
        git_sha = str(data.get("git_sha") or "")
        test_user_id = str(data.get("test_user_id") or "")
        valid_flag = str(bool(data.get("valid", True))).lower()

        lines.append(
            f"| **{family_label}** | `{script_rel}` | `{rel_path}` | `{sha}` | `{git_sha}` | "
            f"`{test_user_id}` | `{fresh}` / `{replayed}` | `{turns}` | "
            f"`${cash:.6f}` | `${notional:.6f}` | `{valid_flag}` |"
        )

    if isinstance(g3_payload, dict) and isinstance(g3_payload.get("evaluation"), dict):
        ev = g3_payload["evaluation"]
        g3_totals = (g3_payload.get("journal") or {}).get("totals") or {}
        sourced_total = ev.get("gold_sourced_total", ev.get("sourced_total", 0))
        true_verified = ev.get("true_verified_in_gold_count", ev.get("true_verified_count", 0))
        false_verified = ev.get("false_verified_in_gold_count", ev.get("false_verified_count", 0))
        false_unverified = ev.get("false_unverified_in_gold_count", ev.get("false_unverified_count", 0))
        sourced_uncertain = ev.get("sourced_uncertain_in_gold_count", ev.get("sourced_uncertain_count", 0))
        lines.extend(
            [
                "",
                "| G3 关键指标 | 证据实测值 |",
                "|---|---|",
                f"| 金标准有来源数字总数 (`gold_sourced_total`) | `{sourced_total}` |",
                f"| 金标准真阳核实数 (`true_verified_in_gold_count`) | `{true_verified}` |",
                f"| 金标准错放数 (`false_verified_in_gold_count`) | `{false_verified}` |",
                f"| 金标准误判无来源/矛盾 (`false_unverified_in_gold_count`) | `{false_unverified}` |",
                f"| 金标准有来源判 `uncertain` (`sourced_uncertain_in_gold_count`) | `{sourced_uncertain}` |",
                f"| `dalton_llm_journal` 新调用 / 回放 (`fresh_calls` / `replayed_calls`) | `{g3_totals.get('fresh_calls', 0)}` / `{g3_totals.get('replayed_calls', 0)}` |",
                f"| `dalton_llm_journal` 名义花费 (`notional_usd`) | `${float(g3_totals.get('notional_usd', 0.0)):.6f}` |",
            ]
        )

    lines.append(GENERATED_END_MARKER)
    return "\n".join(lines)


def update_markdown_generated_block(
    md_path: str | Path,
    *,
    evidence_dir: str | Path | None = None,
    repo_root: Path = REPO_ROOT,
) -> bool:
    """Replace or insert the `<!-- generated:evidence-table --> ... <!-- /generated -->` block in `md_path`."""
    p = Path(md_path)
    if not p.is_absolute():
        p = repo_root / p
    text = p.read_text(encoding="utf-8")
    block = render_evidence_table_block(evidence_dir, repo_root=repo_root)
    pattern = re.compile(
        r"^[ \t]*"
        + re.escape(GENERATED_START_MARKER)
        + r"[ \t]*\r?\n.*?^[ \t]*"
        + re.escape(GENERATED_END_MARKER)
        + r"[ \t]*$",
        re.DOTALL | re.MULTILINE,
    )
    if pattern.search(text):
        new_text = pattern.sub(lambda _: block, text, count=1)
    else:
        new_text = text.rstrip() + "\n\n" + block + "\n"
    p.write_text(new_text, encoding="utf-8")
    return True


def verify_generated_evidence_block(
    markdown_text: str,
    *,
    evidence_dir: str | Path | None = None,
    repo_root: Path = REPO_ROOT,
) -> list[dict[str, Any]]:
    """Verify that any `<!-- generated:evidence-table --> ... <!-- /generated -->` block matches `render_evidence_table_block` verbatim."""
    m = re.search(
        r"^[ \t]*"
        + re.escape(GENERATED_START_MARKER)
        + r"[ \t]*\r?\n.*?^[ \t]*"
        + re.escape(GENERATED_END_MARKER)
        + r"[ \t]*$",
        markdown_text,
        re.DOTALL | re.MULTILINE,
    )
    if not m:
        return []

    found_block = m.group(0).strip()
    inferred_dir = evidence_dir
    if inferred_dir is None:
        path_m = re.search(r"`(var/dalton/[^`]+?)/[^/`]+\.json`", found_block)
        if path_m:
            inferred_dir = repo_root / path_m.group(1)

    expected_block = render_evidence_table_block(inferred_dir, repo_root=repo_root).strip()
    if found_block != expected_block:
        start_line = markdown_text[: m.start()].count("\n") + 1
        return [
            {
                "line": start_line,
                "reason": "generated_evidence_table_mismatch",
                "expected": expected_block,
                "actual": found_block,
            }
        ]
    return []


def compare_legacy_report(
    report_path: str | Path,
    *,
    evidence_dir: str | Path | None = None,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    """Compare legacy hand-written table & G3 claims in `P0I-round3-dev-report.md` against raw JSON files (P0J §1.6)."""
    p = Path(report_path)
    if not p.is_absolute():
        p = repo_root / p
    ev_dir = Path(evidence_dir) if evidence_dir is not None else (repo_root / "var" / "dalton" / "trial-runs" / "realrun-20261019")
    if not ev_dir.is_absolute():
        ev_dir = repo_root / ev_dir

    text = p.read_text(encoding="utf-8")
    row_diffs: list[dict[str, Any]] = []

    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip().startswith("| **G"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 7:
            continue
        family_cell = cells[0].replace("*", "").strip()
        raw_paths_cell = cells[3]
        sha_cell = cells[4].strip("` ")
        calls_cell = cells[5]
        cost_cell = cells[6]

        json_m = re.search(r"(g[1-7]_[a-z0-9_]+\.json)", raw_paths_cell)
        if not json_m:
            continue
        json_file = ev_dir / json_m.group(1)
        if not json_file.is_file():
            continue

        actual_sha = compute_sha256(json_file)
        payload = json.loads(json_file.read_text(encoding="utf-8"))
        totals = (payload.get("journal") or {}).get("totals") or {}
        actual_fresh = int(totals.get("fresh_calls") or 0)
        actual_replayed = int(totals.get("replayed_calls") or 0)
        actual_cash = float(totals.get("combined_cash_usd") or 0.0)
        actual_notional = float(totals.get("notional_usd") or 0.0)

        call_m = re.search(r"`(\d+)`\s*/\s*`(\d+)`", calls_cell)
        rep_fresh = int(call_m.group(1)) if call_m else -1
        rep_replayed = int(call_m.group(2)) if call_m else -1

        cash_m = re.search(r"`\$([0-9.]+)`", cost_cell)
        rep_cash = float(cash_m.group(1)) if cash_m else -1.0

        notional_m = re.search(r"名义\s*`\$([0-9.]+)`", cost_cell)
        rep_notional = float(notional_m.group(1)) if notional_m else None

        mismatches: list[str] = []
        if sha_cell != actual_sha:
            mismatches.append(f"sha256: report={sha_cell[:12]}... != actual={actual_sha[:12]}...")
        if (rep_fresh, rep_replayed) != (actual_fresh, actual_replayed):
            mismatches.append(
                f"fresh/replayed: report={rep_fresh}/{rep_replayed} != actual={actual_fresh}/{actual_replayed}"
            )
        if abs(rep_cash - actual_cash) > 1e-6:
            mismatches.append(f"combined_cash_usd: report=${rep_cash:.6f} != actual=${actual_cash:.6f}")
        if rep_notional is not None and abs(rep_notional - actual_notional) > 1e-6:
            mismatches.append(f"notional_usd: report=${rep_notional:.6f} != actual=${actual_notional:.6f}")

        if mismatches:
            row_diffs.append(
                {
                    "line": line_no,
                    "family": family_cell,
                    "json_file": json_file.name,
                    "mismatches": mismatches,
                }
            )

    g3_eval_diffs: list[str] = []
    g3_path = ev_dir / "g3_publish.json"
    if g3_path.is_file():
        g3_data = json.loads(g3_path.read_text(encoding="utf-8"))
        ev = g3_data.get("evaluation") or {}
        sourced_total = ev.get("gold_sourced_total", ev.get("sourced_total"))
        true_verified = ev.get("true_verified_in_gold_count", ev.get("true_verified_count"))
        false_unverified = ev.get("false_unverified_in_gold_count", ev.get("false_unverified_count"))
        sourced_uncertain = ev.get("sourced_uncertain_in_gold_count", ev.get("sourced_uncertain_count"))
        if "61 / 61 = 100.0%" in text and sourced_total != 61:
            g3_eval_diffs.append(
                f"G3 recall: report claims 61/61 (100.0%), actual evidence is "
                f"{true_verified}/{sourced_total} "
                f"(false_unverified={false_unverified}, sourced_uncertain={sourced_uncertain})"
            )

    return {
        "report": str(p.relative_to(repo_root) if p.is_relative_to(repo_root) else p),
        "mismatched_table_rows_count": len(row_diffs),
        "mismatched_table_rows": row_diffs,
        "g3_evaluation_mismatches": g3_eval_diffs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render or verify deterministic evidence tables from var/dalton/ JSON files (P0J §1.6, L42)."
    )
    parser.add_argument("--evidence-dir", default=None, help="Directory containing g*.json evidence files")
    parser.add_argument("--update", default=None, help="Markdown file path to update in-place")
    parser.add_argument(
        "--compare-legacy-report",
        default=None,
        help="Compare a legacy report (e.g. P0I-round3-dev-report.md) against raw JSON evidence",
    )
    args = parser.parse_args(argv)

    if args.compare_legacy_report:
        diff_res = compare_legacy_report(args.compare_legacy_report, evidence_dir=args.evidence_dir)
        sys.stdout.write(json.dumps(diff_res, ensure_ascii=False, indent=2) + "\n")
        return 0

    if args.update:
        update_markdown_generated_block(args.update, evidence_dir=args.evidence_dir)
        sys.stdout.write(f"Updated generated evidence table in {args.update}\n")
        return 0

    sys.stdout.write(render_evidence_table_block(args.evidence_dir) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
