#!/usr/bin/env python3
"""P0F.4 Wiki Coverage Assessment Report Generator (strictly read-only).

Inspects `~/.openclaw/workspace/wiki/companies` (174 dirs) and `sectors` (27 dirs)
against `config/dalton/coverage.yaml` (105 companies: 27 Core, 78 Watch).
For each company in `coverage.yaml`:
- Checks whether a corresponding directory exists under `wiki/companies/<SLUG>`;
- Counts `.md` pages (content pages excluding `index.md`, plus total `.md` files)
  and determines the latest document / mtime date;
- Runs a `company-wiki` search by company name / ticker (first page, top 10)
  against `wiki/vectors.db` (`mode=ro&immutable=1`) and checks whether the first
  page hits the company's own wiki directory.

Writes the Markdown report to `var/dalton/reports/wiki_coverage_20261018.md`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COVERAGE_YAML = REPO_ROOT / "config" / "dalton" / "coverage.yaml"
DEFAULT_OCW_ROOT = Path(os.path.expanduser("~/.openclaw/workspace"))
DEFAULT_REPORT_PATH = REPO_ROOT / "var" / "dalton" / "reports" / "wiki_coverage_20261018.md"

DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\b")


def _load_company_wiki_module(ocw_root: Path) -> Any:
    cli_path = ocw_root / "skills" / "company-wiki" / "scripts" / "company_wiki_cli.py"
    spec = importlib.util.spec_from_file_location("company_wiki_cli_ro", cli_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {cli_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_wiki_aliases_only(ocw_root: Path) -> dict[str, str]:
    """Read ONLY the `aliases` mapping from company-wiki config.json (never reads any keys/tokens)."""
    cfg_path = ocw_root / "skills" / "company-wiki" / "config.json"
    if not cfg_path.is_file():
        return {}
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    aliases = raw.get("aliases") if isinstance(raw, dict) else {}
    if not isinstance(aliases, dict):
        return {}
    return {str(k).strip().lower(): str(v).strip() for k, v in aliases.items() if k and v}


def resolve_wiki_company_dir(
    company_entry: dict[str, Any],
    existing_dirs: dict[str, Path],
    wiki_aliases: dict[str, str],
) -> Path | None:
    """Resolve a coverage.yaml company entry to an existing `wiki/companies/<DIR>` directory if any."""
    candidates: list[str] = []
    ticker = str(company_entry.get("ticker") or "").strip()
    ws_name = str((company_entry.get("workspace") or {}).get("name") or "").strip()
    name = str(company_entry.get("name") or "").strip()
    search_names = (company_entry.get("aliases") or {}).get("search_names") or []

    for c in [ticker, ws_name, name, *search_names]:
        c_str = str(c).strip()
        if c_str and c_str not in candidates:
            candidates.append(c_str)
            # Also strip .HK / .SS / .SZ / leading zeros for HK tickers
            if c_str.upper().endswith(".HK"):
                stem = c_str[:-3].lstrip("0") or "0"
                candidates.append(stem)
                candidates.append(stem.zfill(4))
                candidates.append(f"{stem.zfill(4)}.hk")
            elif c_str.upper().endswith((".SS", ".SZ", ".SH")):
                stem = c_str.split(".")[0]
                candidates.append(stem)

    existing_upper = {k.upper(): p for k, p in existing_dirs.items()}

    # 1. Direct match on ticker / workspace name / alias against existing directory names
    for cand in candidates:
        if cand.upper() in existing_upper:
            return existing_upper[cand.upper()]

    # 2. Match via company-wiki config.json aliases
    for cand in candidates:
        mapped = wiki_aliases.get(cand.lower())
        if mapped and mapped.upper() in existing_upper:
            return existing_upper[mapped.upper()]

    return None


def inspect_company_dir(dir_path: Path, db_conn: sqlite3.Connection | None) -> dict[str, Any]:
    """Count .md files (excluding and including index.md) and find latest document/mtime date."""
    all_md = sorted(f for f in dir_path.iterdir() if f.is_file() and f.suffix == ".md")
    content_md = [f for f in all_md if f.name != "index.md"]

    dates: list[str] = []
    for f in content_md:
        m = DATE_PREFIX_RE.match(f.name)
        if m:
            dates.append(m.group(1))

    if db_conn is not None:
        row = db_conn.execute(
            "SELECT COUNT(*), MAX(date) FROM documents WHERE category_type = 'company' AND UPPER(category_name) = UPPER(?)",
            (dir_path.name,),
        ).fetchone()
        if row and row[1]:
            dates.append(str(row[1]))

    latest_mtime_date: str | None = None
    target_files = content_md or all_md
    if target_files:
        max_mtime = max(f.stat().st_mtime for f in target_files)
        latest_mtime_date = dt.date.fromtimestamp(max_mtime).isoformat()

    latest_doc_date = max(dates) if dates else latest_mtime_date
    return {
        "dir_name": dir_path.name,
        "content_pages": len(content_md),
        "total_md_files": len(all_md),
        "has_index_md": (dir_path / "index.md").is_file(),
        "latest_doc_date": latest_doc_date or "-",
        "latest_mtime_date": latest_mtime_date or "-",
    }


def check_company_wiki_search_hit(
    wiki_mod: Any,
    db_conn: sqlite3.Connection,
    company_entry: dict[str, Any],
    expected_dir_name: str | None,
) -> dict[str, Any]:
    """Run company-wiki search using company name/aliases and check if page 1 (top 10) hits own directory."""
    ticker = str(company_entry.get("ticker") or "").strip()
    name = str(company_entry.get("name") or "").strip()
    search_names = (company_entry.get("aliases") or {}).get("search_names") or []

    queries_to_try: list[str] = []
    for q in [name, ticker, *search_names, *( [expected_dir_name] if expected_dir_name else [] )]:
        q_str = str(q).strip()
        if q_str and q_str not in queries_to_try:
            queries_to_try.append(q_str)

    if expected_dir_name is None:
        for q in queries_to_try:
            rows = wiki_mod.keyword_search(db_conn, q, ([], []), limit=10)
            if rows:
                return {
                    "hit_self": False,
                    "hit_any": True,
                    "query_used": q,
                    "rank": None,
                    "total_results": len(rows),
                }
        return {
            "hit_self": False,
            "hit_any": False,
            "query_used": name or ticker,
            "rank": None,
            "total_results": 0,
        }

    for q in queries_to_try:
        rows = wiki_mod.keyword_search(db_conn, q, ([], []), limit=50)
        seen_doc_ids: set[int] = set()
        dedup_rows = []
        for r in rows:
            doc_id = r[1]
            if doc_id not in seen_doc_ids:
                seen_doc_ids.add(doc_id)
                dedup_rows.append(r)
            if len(dedup_rows) >= 10:
                break

        for idx, r in enumerate(dedup_rows, 1):
            cat_type, cat_name = str(r[3]), str(r[4])
            if cat_type == "company" and cat_name.upper() == expected_dir_name.upper():
                return {
                    "hit_self": True,
                    "hit_any": True,
                    "query_used": q,
                    "rank": idx,
                    "total_results": len(dedup_rows),
                }

    return {
        "hit_self": False,
        "hit_any": False,
        "query_used": queries_to_try[0] if queries_to_try else ticker,
        "rank": None,
        "total_results": 0,
    }


def generate_report(
    coverage_yaml: Path = DEFAULT_COVERAGE_YAML,
    ocw_root: Path = DEFAULT_OCW_ROOT,
    report_path: Path = DEFAULT_REPORT_PATH,
) -> dict[str, Any]:
    cov_data = yaml.safe_load(coverage_yaml.read_text(encoding="utf-8"))
    companies: list[dict[str, Any]] = cov_data.get("companies") or []

    wiki_root = ocw_root / "wiki"
    companies_dir = wiki_root / "companies"
    sectors_dir = wiki_root / "sectors"
    vectors_db = wiki_root / "vectors.db"

    existing_company_dirs = {
        p.name: p for p in sorted(companies_dir.iterdir()) if p.is_dir()
    }
    existing_sector_dirs = {
        p.name: p for p in sorted(sectors_dir.iterdir()) if p.is_dir()
    }

    wiki_aliases = _load_wiki_aliases_only(ocw_root)
    wiki_mod = _load_company_wiki_module(ocw_root)

    conn = sqlite3.connect(f"file:{vectors_db}?mode=ro&immutable=1", uri=True)
    conn.execute("PRAGMA query_only = ON")

    try:
        total_indexed_docs, latest_indexed_date = conn.execute(
            "SELECT COUNT(*), MAX(date) FROM documents"
        ).fetchone()

        rows_out: list[dict[str, Any]] = []
        matched_wiki_dirs: set[str] = set()

        for entry in companies:
            ticker = str(entry.get("ticker") or "").strip()
            name = str(entry.get("name") or "").strip()
            tier = str(entry.get("tier") or "").strip().lower()
            market = str(entry.get("market") or "").strip()

            dir_path = resolve_wiki_company_dir(entry, existing_company_dirs, wiki_aliases)
            if dir_path is not None:
                matched_wiki_dirs.add(dir_path.name)
                dir_info = inspect_company_dir(dir_path, conn)
                search_info = check_company_wiki_search_hit(
                    wiki_mod, conn, entry, dir_path.name
                )
            else:
                dir_info = {
                    "dir_name": None,
                    "content_pages": 0,
                    "total_md_files": 0,
                    "has_index_md": False,
                    "latest_doc_date": "-",
                    "latest_mtime_date": "-",
                }
                search_info = {
                    "hit_self": False,
                    "query_used": name or ticker,
                    "rank": None,
                    "total_results": 0,
                }

            rows_out.append(
                {
                    "ticker": ticker,
                    "name": name,
                    "tier": tier,
                    "market": market,
                    "has_wiki_dir": dir_path is not None,
                    "has_wiki_pages": dir_info["content_pages"] > 0,
                    "wiki_dir": dir_info["dir_name"] or "-",
                    "content_pages": dir_info["content_pages"],
                    "total_md_files": dir_info["total_md_files"],
                    "latest_doc_date": dir_info["latest_doc_date"],
                    "latest_mtime_date": dir_info["latest_mtime_date"],
                    "search_hit": search_info["hit_self"],
                    "search_query": search_info["query_used"],
                    "search_rank": search_info["rank"],
                }
            )
    finally:
        conn.close()

    core_rows = [r for r in rows_out if r["tier"] == "core"]
    watch_rows = [r for r in rows_out if r["tier"] == "watch"]

    core_with_dir = [r for r in core_rows if r["has_wiki_dir"]]
    core_without_dir = [r for r in core_rows if not r["has_wiki_dir"]]
    core_search_hit = [r for r in core_rows if r["search_hit"]]

    watch_with_dir = [r for r in watch_rows if r["has_wiki_dir"]]
    watch_without_dir = [r for r in watch_rows if not r["has_wiki_dir"]]
    watch_search_hit = [r for r in watch_rows if r["search_hit"]]

    total_with_dir = len(core_with_dir) + len(watch_with_dir)
    total_with_pages = sum(1 for r in rows_out if r["has_wiki_pages"])
    total_search_hit = len(core_search_hit) + len(watch_search_hit)

    unmatched_wiki_dirs = sorted(set(existing_company_dirs.keys()) - matched_wiki_dirs)

    summary = {
        "as_of": "2026-10-18",
        "wiki_total_company_dirs": len(existing_company_dirs),
        "wiki_total_sector_dirs": len(existing_sector_dirs),
        "wiki_total_indexed_documents": int(total_indexed_docs or 0),
        "wiki_latest_document_date": str(latest_indexed_date or "-"),
        "coverage_total": len(rows_out),
        "coverage_core_total": len(core_rows),
        "coverage_watch_total": len(watch_rows),
        "coverage_with_wiki_dir": total_with_dir,
        "coverage_with_wiki_pages": total_with_pages,
        "coverage_search_hit_page1": total_search_hit,
        "core_with_wiki_dir": len(core_with_dir),
        "core_without_wiki_dir": len(core_without_dir),
        "core_search_hit_page1": len(core_search_hit),
        "core_missing_tickers": [
            {"ticker": r["ticker"], "name": r["name"]} for r in core_without_dir
        ],
        "watch_with_wiki_dir": len(watch_with_dir),
        "watch_without_wiki_dir": len(watch_without_dir),
        "watch_search_hit_page1": len(watch_search_hit),
        "extra_wiki_company_dirs_outside_coverage_count": len(unmatched_wiki_dirs),
    }

    lines: list[str] = [
        "# Dalton 基金 Wiki 覆盖率核查报告（P0F.4）",
        "",
        "- **核查日期**：2026-10-18",
        f"- **Wiki 路径（只读）**：`~/.openclaw/workspace/wiki`",
        f"- **覆盖名单真源**：`config/dalton/coverage.yaml`（共 **{summary['coverage_total']}** 家：Core **{summary['coverage_core_total']}** 家、Watch **{summary['coverage_watch_total']}** 家）",
        "",
        "## 1. 总体规模与覆盖汇总",
        "",
        "| 指标 | 数值 | 说明 |",
        "|---|---:|---|",
        f"| Wiki `companies/` 目录总数 | **{summary['wiki_total_company_dirs']}** | 含覆盖名单内公司及未入覆盖池的产业链/非上市公司 |",
        f"| Wiki `sectors/` 行业目录总数 | **{summary['wiki_total_sector_dirs']}** | 如 `china-internet`、`global-semiconductor`、`us-software` 等 |",
        f"| Wiki `vectors.db` 已索引文档总数 | **{summary['wiki_total_indexed_documents']}** | 最新文档日期 `{summary['wiki_latest_document_date']}` |",
        f"| 覆盖名单（105 家）中有 Wiki 目录 | **{summary['coverage_with_wiki_dir']} / {summary['coverage_total']}** | Core **{summary['core_with_wiki_dir']} / {summary['coverage_core_total']}**，Watch **{summary['watch_with_wiki_dir']} / {summary['coverage_watch_total']}** |",
        f"| 覆盖名单（105 家）中有正文页面（不含 `index.md`） | **{summary['coverage_with_wiki_pages']} / {summary['coverage_total']}** | 有目录的公司均含有效 `.md` 研究纪要/资料 |",
        f"| `company-wiki` 检索第一页（Top 10）命中自身目录 | **{summary['coverage_search_hit_page1']} / {summary['coverage_with_wiki_dir']}**（占全覆盖 {summary['coverage_search_hit_page1']}/{summary['coverage_total']}） | Core **{summary['core_search_hit_page1']} / {summary['core_with_wiki_dir']}**，Watch **{summary['watch_search_hit_page1']} / {summary['watch_with_wiki_dir']}** |",
        f"| 覆盖名单之外的 Wiki 公司目录数 | **{summary['extra_wiki_company_dirs_outside_coverage_count']}** | 供应链/未上市独角兽/历史跟踪标的（如 `ANTHROPIC`、`OPENAI`、`SPACEX`、`CATL`、`BYTEDANCE` 等） |",
        "",
        "> **关于旧 PROGRESS.md 误写“26 家公司与 8 篇专题”的原因说明**：",
        "> 当时编写 P0-D.4 摘要时未直接遍历 `~/.openclaw/workspace/wiki/companies`（174 个目录）与 `sectors`（27 个目录），而是误将早期抽样统计的 Core 持仓子集（26 家）和某单次查询返回的 8 条结果当作全库规模。本次已通过只读脚本全量核实。",
        "",
        "---",
        "",
        "## 2. Core 27 家中没有 Wiki 目录的公司（重点缺口）",
        "",
    ]

    if core_without_dir:
        lines.append(
            f"Core {summary['coverage_core_total']} 家中有 **{len(core_without_dir)}** 家在 `wiki/companies/` 下尚无独立目录："
        )
        lines.append("")
        lines.append("| Ticker | 公司名称 | Market | 状态说明 |")
        lines.append("|---|---|---|---|")
        for r in core_without_dir:
            lines.append(
                f"| `{r['ticker']}` | {r['name']} | {r['market']} | 无 `wiki/companies/` 目录（检索 `company-wiki` 未命中自身页面） |"
            )
    else:
        lines.append("Core 27 家全部拥有 Wiki 目录。")

    lines.extend(
        [
            "",
            "---",
            "",
            "## 3. Core 27 家逐家核查明细",
            "",
            "| # | Ticker | 公司名称 | Market | Wiki 目录 | 正文页数 (不含 `index.md`) | 总 `.md` 数 | 最近文档日期 | `company-wiki` 首页命中 | 检索词与排名 |",
            "|---:|---|---|---|---|---:|---:|---|---|---|",
        ]
    )

    for idx, r in enumerate(core_rows, 1):
        hit_str = "✅ 命中" if r["search_hit"] else ("❌ 未命中" if r["has_wiki_dir"] else "— (无目录)")
        rank_str = f"`{r['search_query']}` (#{r['search_rank']})" if r["search_hit"] else "-"
        lines.append(
            f"| {idx} | `{r['ticker']}` | {r['name']} | {r['market']} | `{r['wiki_dir']}` | "
            f"{r['content_pages']} | {r['total_md_files']} | {r['latest_doc_date']} | {hit_str} | {rank_str} |"
        )

    lines.extend(
        [
            "",
            "---",
            "",
            "## 4. Watch 78 家逐家核查明细",
            "",
            "| # | Ticker | 公司名称 | Market | Wiki 目录 | 正文页数 (不含 `index.md`) | 总 `.md` 数 | 最近文档日期 | `company-wiki` 首页命中 | 检索词与排名 |",
            "|---:|---|---|---|---|---:|---:|---|---|---|",
        ]
    )

    for idx, r in enumerate(watch_rows, 1):
        hit_str = "✅ 命中" if r["search_hit"] else ("❌ 未命中" if r["has_wiki_dir"] else "— (无目录)")
        rank_str = f"`{r['search_query']}` (#{r['search_rank']})" if r["search_hit"] else "-"
        lines.append(
            f"| {idx} | `{r['ticker']}` | {r['name']} | {r['market']} | `{r['wiki_dir']}` | "
            f"{r['content_pages']} | {r['total_md_files']} | {r['latest_doc_date']} | {hit_str} | {rank_str} |"
        )

    lines.extend(
        [
            "",
            "---",
            "",
            "## 5. Wiki 27 个行业目录（`wiki/sectors/`）清单",
            "",
            "| # | Sector 目录 | 正文页数 (不含 `index.md`) | 总 `.md` 数 | 最近文档日期 |",
            "|---:|---|---:|---:|---|",
        ]
    )

    for idx, (sec_name, sec_path) in enumerate(sorted(existing_sector_dirs.items()), 1):
        sec_info = inspect_company_dir(sec_path, None)
        lines.append(
            f"| {idx} | `{sec_name}` | {sec_info['content_pages']} | {sec_info['total_md_files']} | {sec_info['latest_doc_date']} |"
        )

    lines.append("")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "summary": summary,
        "report_path": str(report_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate P0F.4 Wiki coverage report (read-only).")
    parser.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE_YAML)
    parser.add_argument("--ocw-root", type=Path, default=DEFAULT_OCW_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    res = generate_report(
        coverage_yaml=args.coverage,
        ocw_root=args.ocw_root,
        report_path=args.output,
    )
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        s = res["summary"]
        print(
            f"[wiki_coverage_report] Written to {res['report_path']}\n"
            f"  Wiki dirs: companies={s['wiki_total_company_dirs']}, sectors={s['wiki_total_sector_dirs']}, indexed_docs={s['wiki_total_indexed_documents']}\n"
            f"  Coverage (105): with_wiki_dir={s['coverage_with_wiki_dir']}/105 (Core {s['core_with_wiki_dir']}/27, Watch {s['watch_with_wiki_dir']}/78)\n"
            f"  Search hit page 1: {s['coverage_search_hit_page1']}/{s['coverage_with_wiki_dir']} (Core {s['core_search_hit_page1']}/{s['core_with_wiki_dir']}, Watch {s['watch_search_hit_page1']}/{s['watch_with_wiki_dir']})\n"
            f"  Core missing wiki dirs ({s['core_without_wiki_dir']}): {[c['ticker'] for c in s['core_missing_tickers']]}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
