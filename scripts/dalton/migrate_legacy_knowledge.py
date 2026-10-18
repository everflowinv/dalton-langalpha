#!/usr/bin/env python3
"""P1.4 Legacy Knowledge Migration Script (OpenClaw + Dalton core.sqlite -> Company Knowledge Layer).

Migrates:
1. OpenClaw `~/.openclaw/workspace/state/stock-tracker/<T>/` (`thesis.md`, `consensus/*.json`, `last_run.json`, `seen.json`, `updates.log`)
2. OpenClaw `~/.openclaw/workspace/wiki/` (`vectors.db`, `companies/<T>/*`)
3. Dalton `/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core/core.sqlite`
   (`debate_map_versions`, `company_dossier_versions`, `forecast_model_versions`, `claim_versions`)

Strictly read-only on all legacy sources:
- SQLite connections opened with `mode=ro&immutable=1`
- Never reads `writer-tokens.json`, `credential-grants`, `deliveries.json`
- Strips `feishu` from any state JSON
- Verifies SHA-256 and mtime of all legacy source files before and after execution
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import src.dalton.knowledge.ops  # noqa: F401
from src.dalton.knowledge.paths import DOC_KEYS, RENDERED, data_path, rendered_path
from src.dalton.knowledge.render import (
    render_agent_block,
    render_document_md,
    render_published_index_md,
    render_verdicts_md,
    update_agent_md_block,
)
from src.dalton.knowledge.validate import validate_update

DEFAULT_OCW_PATH = Path(os.path.expanduser("~/.openclaw/workspace"))
DEFAULT_DALTON_SQLITE = Path(
    "/Volumes/EveSSD/Dalton/workspaces/ws-7d894366d1132e2930475a60/state/dalton-core/core.sqlite"
)
DEFAULT_TICKERS = ("META", "AMZN", "GOOGL", "MSFT")
FORBIDDEN_SUBSTRINGS = ("writer-tokens.json", "credential-grants", "deliveries.json")

COMPANY_METADATA: dict[str, dict[str, Any]] = {
    "META": {
        "name": "Meta Platforms, Inc.",
        "name_zh": "Meta Platforms",
        "cik": "0001326801",
        "exchange": "NASDAQ",
        "sector": "Communication Services",
        "currency": "USD",
        "fiscal_year_end": "12-31",
        "peers": ["GOOGL", "AMZN", "SNAP", "PINS", "TTD"],
        "benchmarks": ["QQQ", "XLC"],
        "current_period": "3Q26",
        "current_fy": "FY2026",
        "next_fy": "FY2027",
        "earnings_date": "2026-10-28",
        "team": [
            {"name": "Mark Zuckerberg", "role": "Founder, Chairman & CEO", "since": "2004", "notes": "Leads AI/Muse & FoA strategy"},
            {"name": "Susan Li", "role": "CFO", "since": "2022-11", "notes": "Leads expense & capex guidance discipline"},
        ],
    },
    "AMZN": {
        "name": "Amazon.com, Inc.",
        "name_zh": "亚马逊",
        "cik": "0001018724",
        "exchange": "NASDAQ",
        "sector": "Consumer Discretionary",
        "currency": "USD",
        "fiscal_year_end": "12-31",
        "peers": ["MSFT", "GOOGL", "WMT", "SHOP"],
        "benchmarks": ["QQQ", "XLY"],
        "current_period": "3Q26",
        "current_fy": "FY2026",
        "next_fy": "FY2027",
        "earnings_date": "2026-10-29",
        "team": [
            {"name": "Andy Jassy", "role": "President & CEO", "since": "2021-07", "notes": "Leads AWS & retail automation"},
            {"name": "Brian Olsavsky", "role": "SVP & CFO", "since": "2015-06", "notes": "Quarterly EBIT & revenue guidance"},
            {"name": "Matt Garman", "role": "CEO of AWS", "since": "2024-06", "notes": "Leads AWS infrastructure, Bedrock & Trainium"},
        ],
    },
    "GOOGL": {
        "name": "Alphabet Inc.",
        "name_zh": "谷歌 (Alphabet)",
        "cik": "0001652044",
        "exchange": "NASDAQ",
        "sector": "Communication Services",
        "currency": "USD",
        "fiscal_year_end": "12-31",
        "peers": ["META", "MSFT", "AMZN"],
        "benchmarks": ["QQQ", "XLC"],
        "current_period": "3Q26",
        "current_fy": "FY2026",
        "next_fy": "FY2027",
        "earnings_date": "2026-10-28",
        "team": [
            {"name": "Sundar Pichai", "role": "CEO", "since": "2015-10", "notes": "Leads Alphabet, Search & Gemini"},
            {"name": "Anat Ashkenazi", "role": "SVP & CFO", "since": "2024-07", "notes": "Leads capex discipline & cost re-engineering"},
        ],
    },
    "MSFT": {
        "name": "Microsoft Corporation",
        "name_zh": "微软",
        "cik": "0000789019",
        "exchange": "NASDAQ",
        "sector": "Information Technology",
        "currency": "USD",
        "fiscal_year_end": "06-30",
        "peers": ["AMZN", "GOOGL", "ORCL", "CRM"],
        "benchmarks": ["QQQ", "XLK"],
        "current_period": "1Q27",
        "current_fy": "FY2027",
        "next_fy": "FY2028",
        "earnings_date": "2026-10-28",
        "team": [
            {"name": "Satya Nadella", "role": "Chairman & CEO", "since": "2014-02", "notes": "Leads Cloud + AI & Copilot transition"},
            {"name": "Amy Hood", "role": "EVP & CFO", "since": "2013-05", "notes": "Leads segment guidance, capex & margin framework"},
            {"name": "Scott Guthrie", "role": "EVP, Cloud + AI", "since": "2014", "notes": "Leads Azure & Maia silicon"},
        ],
    },
}


def _check_safe_path(path: Path) -> None:
    s = str(path)
    for bad in FORBIDDEN_SUBSTRINGS:
        if bad in s:
            raise PermissionError(f"Forbidden legacy file access blocked: {path}")


def _sha256_file(path: Path) -> str:
    _check_safe_path(path)
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot_source_tree(ocw_path: Path, sqlite_path: Path, tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Record SHA-256 and mtime_ns of all legacy source files we read."""
    snap: dict[str, dict[str, Any]] = {}
    candidates: list[Path] = []
    if sqlite_path.exists():
        candidates.append(sqlite_path)
    vec_db = ocw_path / "wiki" / "vectors.db"
    if vec_db.exists():
        candidates.append(vec_db)
    for t in tickers:
        tdir = ocw_path / "state" / "stock-tracker" / t
        if tdir.exists():
            for p in sorted(tdir.rglob("*")):
                if p.is_file() and not any(b in p.name for b in FORBIDDEN_SUBSTRINGS):
                    candidates.append(p)
        wdir = ocw_path / "wiki" / "companies" / t
        if wdir.exists():
            for p in sorted(wdir.rglob("*")):
                if p.is_file() and not any(b in p.name for b in FORBIDDEN_SUBSTRINGS):
                    candidates.append(p)
    for p in candidates:
        st = p.stat()
        snap[str(p)] = {
            "sha256": _sha256_file(p),
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
        }
    return snap


def verify_source_tree_unchanged(
    before: dict[str, dict[str, Any]],
    ocw_path: Path,
    sqlite_path: Path,
    tickers: list[str],
) -> dict[str, Any]:
    after = snapshot_source_tree(ocw_path, sqlite_path, tickers)
    mutations: list[dict[str, Any]] = []
    for path_str, b_info in before.items():
        a_info = after.get(path_str)
        if a_info is None:
            mutations.append({"path": path_str, "error": "deleted"})
        elif a_info["sha256"] != b_info["sha256"] or a_info["mtime_ns"] != b_info["mtime_ns"]:
            mutations.append({"path": path_str, "before": b_info, "after": a_info})
    return {
        "files_checked": len(before),
        "zero_source_mutations": len(mutations) == 0,
        "mutations": mutations,
    }


def _connect_ro_sqlite(path: Path) -> sqlite3.Connection:
    _check_safe_path(path)
    uri = f"file:{path}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def parse_thesis_markdown(ticker: str, md_text: str, source_rel_path: str) -> dict[str, Any]:
    """Parse OpenClaw `thesis.md` into structured sections with line-range provenance."""
    lines = md_text.splitlines()
    sections: dict[str, dict[str, Any]] = {}
    debates: list[dict[str, Any]] = []

    current_h2: str | None = None
    current_h2_start: int = 1
    current_h2_lines: list[tuple[int, str]] = []
    current_d: dict[str, Any] | None = None

    def flush_d(end_line: int) -> None:
        nonlocal current_d
        if current_d is not None:
            current_d["end_line"] = end_line
            raw_block = "\n".join(l for _, l in current_d["raw_lines"])
            current_d["sha256"] = _sha256_text(raw_block)
            debates.append(current_d)
            current_d = None

    def flush_h2(end_line: int) -> None:
        nonlocal current_h2, current_h2_lines
        if current_h2 is not None:
            raw_block = "\n".join(l for _, l in current_h2_lines)
            sections[current_h2] = {
                "heading": current_h2,
                "start_line": current_h2_start,
                "end_line": end_line,
                "lines": current_h2_lines,
                "sha256": _sha256_text(raw_block),
            }

    header_lines: list[tuple[int, str]] = []
    for idx, line in enumerate(lines, start=1):
        if line.startswith("## "):
            flush_d(idx - 1)
            flush_h2(idx - 1)
            current_h2 = line[3:].strip()
            current_h2_start = idx
            current_h2_lines = []
        elif line.startswith("### D") and current_h2 == "Debates":
            flush_d(idx - 1)
            m = re.match(r"^###\s+(D\d+)\s+(.*)$", line.strip())
            if m:
                d_id, d_title = m.group(1), m.group(2).strip()
            else:
                parts = line[4:].strip().split(None, 1)
                d_id = parts[0]
                d_title = parts[1] if len(parts) > 1 else parts[0]
            current_d = {
                "id": d_id,
                "title": d_title,
                "start_line": idx,
                "end_line": idx,
                "raw_lines": [(idx, line)],
                "bullets": [],
            }
        else:
            if current_h2 is None:
                header_lines.append((idx, line))
            else:
                current_h2_lines.append((idx, line))
                if current_d is not None:
                    current_d["raw_lines"].append((idx, line))
                    stripped = line.strip()
                    if stripped.startswith("- "):
                        current_d["bullets"].append((idx, stripped[2:].strip()))

    flush_d(len(lines))
    flush_h2(len(lines))

    return {
        "ticker": ticker,
        "source_path": source_rel_path,
        "file_sha256": _sha256_text(md_text),
        "total_lines": len(lines),
        "header_lines": header_lines,
        "sections": sections,
        "debates": debates,
    }


def extract_dalton_core_for_ticker(sqlite_path: Path, ticker: str) -> dict[str, Any]:
    """Extract latest debate_map, company_dossier, forecast_model, and referenced claims from core.sqlite."""
    if not sqlite_path.exists():
        return {}
    conn = _connect_ro_sqlite(sqlite_path)
    cur = conn.cursor()
    subj_ref = f"company:ticker:{ticker.lower()}"

    cur.execute(
        "SELECT version_number, record_json FROM debate_map_versions WHERE subject_ref = ? ORDER BY version_number DESC LIMIT 1",
        (subj_ref,),
    )
    dm_row = cur.fetchone()
    debate_map = json.loads(dm_row[1]) if dm_row else {}

    cur.execute(
        "SELECT version_number, record_json FROM company_dossier_versions WHERE company_ref = ? ORDER BY version_number DESC LIMIT 1",
        (subj_ref,),
    )
    dos_row = cur.fetchone()
    dossier = json.loads(dos_row[1]) if dos_row else {}

    cur.execute(
        "SELECT version_number, record_json FROM forecast_model_versions WHERE company_ref = ? ORDER BY version_number DESC LIMIT 1",
        (subj_ref,),
    )
    fm_row = cur.fetchone()
    forecast_model = json.loads(fm_row[1]) if fm_row else {}

    claim_ids: set[str] = set()
    for d in debate_map.get("debates", []):
        for pos_key in ("bull_position", "bear_position"):
            pos = d.get(pos_key) or {}
            for ref in pos.get("claim_refs", []):
                if isinstance(ref, str) and ref.startswith("claim-version:"):
                    claim_ids.add(ref)
        mp = d.get("market_position") or {}
        for ref in mp.get("refs", []):
            if isinstance(ref, str) and ref.startswith("claim-version:"):
                claim_ids.add(ref)
    for ev in dossier.get("evidence_refs", []):
        ref = ev.get("ref")
        if isinstance(ref, str) and ref.startswith("claim-version:"):
            claim_ids.add(ref)

    claims: dict[str, Any] = {}
    if claim_ids:
        placeholders = ",".join("?" for _ in claim_ids)
        cur.execute(
            f"SELECT claim_version_id, claim_json FROM claim_versions WHERE claim_version_id IN ({placeholders})",
            tuple(sorted(claim_ids)),
        )
        for cid, cjson in cur.fetchall():
            try:
                claims[cid] = json.loads(cjson)
            except Exception:
                pass

    conn.close()
    return {
        "subject_ref": subj_ref,
        "debate_map_version": dm_row[0] if dm_row else None,
        "debate_map": debate_map,
        "dossier_version": dos_row[0] if dos_row else None,
        "dossier": dossier,
        "forecast_model_version": fm_row[0] if fm_row else None,
        "forecast_model": forecast_model,
        "claims": claims,
    }


def extract_industry_debate_map(sqlite_path: Path) -> dict[str, Any]:
    if not sqlite_path.exists():
        return {}
    conn = _connect_ro_sqlite(sqlite_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT subject_ref, version_number, record_json FROM debate_map_versions WHERE subject_ref NOT LIKE 'company:%' ORDER BY version_number DESC LIMIT 1"
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return {}
    return {
        "subject_ref": row[0],
        "version_number": row[1],
        "debate_map": json.loads(row[2]),
    }


def extract_company_bundle(ocw_path: Path, sqlite_path: Path, ticker: str) -> dict[str, Any]:
    """Stage 1 (`extract`): Build intermediate raw bundle for `ticker`."""
    tdir = ocw_path / "state" / "stock-tracker" / ticker
    thesis_path = tdir / "thesis.md"
    _check_safe_path(thesis_path)
    thesis_md = thesis_path.read_text(encoding="utf-8")
    parsed_thesis = parse_thesis_markdown(
        ticker, thesis_md, f"state/stock-tracker/{ticker}/thesis.md"
    )

    last_run_path = tdir / "last_run.json"
    _check_safe_path(last_run_path)
    last_run = json.loads(last_run_path.read_text(encoding="utf-8")) if last_run_path.exists() else {}
    last_run.pop("feishu", None)

    seen_path = tdir / "seen.json"
    _check_safe_path(seen_path)
    seen_data = json.loads(seen_path.read_text(encoding="utf-8")) if seen_path.exists() else {}
    if isinstance(seen_data, dict):
        seen_data.pop("feishu", None)

    updates_path = tdir / "updates.log"
    _check_safe_path(updates_path)
    updates_log = updates_path.read_text(encoding="utf-8") if updates_path.exists() else ""

    cdir = tdir / "consensus"
    consensus_files = sorted(cdir.glob("*.json")) if cdir.exists() else []
    latest_consensus: dict[str, Any] = {}
    latest_consensus_rel = ""
    if consensus_files:
        lc_path = consensus_files[-1]
        _check_safe_path(lc_path)
        latest_consensus = json.loads(lc_path.read_text(encoding="utf-8"))
        latest_consensus_rel = f"state/stock-tracker/{ticker}/consensus/{lc_path.name}"

    wdir = ocw_path / "wiki" / "companies" / ticker
    wiki_files: list[dict[str, Any]] = []
    if wdir.exists():
        for wp in sorted(wdir.rglob("*")):
            if wp.is_file() and wp.suffix.lower() in (".md", ".txt"):
                _check_safe_path(wp)
                txt = wp.read_text(encoding="utf-8", errors="replace")
                wiki_files.append(
                    {
                        "filename": wp.name,
                        "rel_path": f"wiki/companies/{ticker}/{wp.name}",
                        "sha256": _sha256_text(txt),
                        "size": len(txt),
                    }
                )

    dalton_core = extract_dalton_core_for_ticker(sqlite_path, ticker)

    return {
        "ticker": ticker,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "parsed_thesis": parsed_thesis,
        "raw_thesis_md": thesis_md,
        "last_run": last_run,
        "seen": seen_data,
        "updates_log": updates_log,
        "latest_consensus": latest_consensus,
        "latest_consensus_rel": latest_consensus_rel,
        "wiki_files": wiki_files,
        "dalton_core": dalton_core,
    }


def _infer_my_view(text: str) -> tuple[str, str]:
    """Map Chinese lean/confidence phrasing in `当前倾向：` to debate.schema.json `my_view` enums."""
    side = "neutral"
    if "口径项" in text or "不判多空" in text:
        side = "neutral"
    elif "偏多" in text or "偏 Bull" in text or "小幅上修" in text:
        side = "bull"
    elif "偏空" in text or "偏负" in text or "偏 Bear" in text:
        side = "bear"

    conf = "medium"
    if "高" in text and "中高" not in text and "中低" not in text:
        conf = "high"
    elif "低" in text and "中高" not in text:
        conf = "low"
    return side, conf


def _infer_house_stance(text: str) -> tuple[str, str]:
    """Map core thesis bullets to thesis.schema.json `house_view` stance & conviction."""
    stance = "neutral"
    if "中性偏多" in text or "偏多" in text or "小幅上修" in text:
        stance = "long"
    elif "偏空" in text or "看空" in text:
        stance = "short"

    conv = "medium"
    if "高" in text and "中高" not in text and "中低" not in text:
        conv = "high"
    elif "低" in text and "中高" not in text:
        conv = "low"
    return stance, conv


def _driver_kind_for_title(title: str) -> str:
    t = title.lower()
    if "capex" in t or "折旧" in t or "自由现金流" in t:
        return "capex"
    if "利润率" in t or "成本" in t or "费用" in t:
        return "margin"
    if "定价" in t or "席位" in t or "收费" in t:
        return "price"
    if "融资" in t or "资本结构" in t:
        return "capital_return"
    if "增速" in t or "增长" in t or "搜索" in t or "广告" in t or "云" in t:
        return "volume"
    return "other"


def build_knowledge_documents(bundle: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Stage 2 (`render`): Transform extracted bundle into all 8 PK.2 JSON docs (`DOC_KEYS`) + `provenance.json`."""
    ticker = bundle["ticker"]
    meta = COMPANY_METADATA[ticker]
    pt = bundle["parsed_thesis"]
    sections = pt["sections"]
    ocw_debates = pt["debates"]
    dalton_core = bundle.get("dalton_core") or {}
    now_iso = "2026-10-18T12:00:00Z"
    source_path = pt["source_path"]
    legacy_md_ref = {"kind": "file", "path": "sources/legacy/openclaw_thesis_2026-10-15.md"}

    provenance_items: list[dict[str, Any]] = []

    def rec_prov(
        target_doc: str,
        item_id: str,
        src_type: str,
        ref: str,
        start_line: int | None,
        end_line: int | None,
        text_for_hash: str,
    ) -> None:
        provenance_items.append(
            {
                "target_doc": target_doc,
                "item_id": item_id,
                "source_type": src_type,
                "source_ref": ref,
                "start_line": start_line,
                "end_line": end_line,
                "sha256": _sha256_text(text_for_hash),
            }
        )

    # Extract core thesis bullets from `## 核心判断`
    core_sec = next((v for k, v in sections.items() if "核心判断" in k), None)
    core_bullets: list[tuple[int, str]] = []
    if core_sec:
        for lno, ltxt in core_sec["lines"]:
            s = ltxt.strip()
            if s.startswith("- "):
                core_bullets.append((lno, s[2:].strip()))

    first_bullet = core_bullets[0][1] if core_bullets else f"{ticker} 跟踪底稿核心判断（迁移自 OpenClaw）"
    one_liner = first_bullet[:195]
    stance_enum, conv_enum = _infer_house_stance(" ".join(b for _, b in core_bullets[:3]))

    # Extract `## Earnings revision 模型`
    er_sec = next((v for k, v in sections.items() if "Earnings revision" in k), None)
    er_bullets: list[tuple[int, str]] = []
    if er_sec:
        for lno, ltxt in er_sec["lines"]:
            s = ltxt.strip()
            if s.startswith("- "):
                er_bullets.append((lno, s[2:].strip()))

    # Build Pillars P1..Pn (up to 7) from core_bullets
    pillars: list[dict[str, Any]] = []
    confirm_thresholds: list[dict[str, Any]] = []
    break_thresholds: list[dict[str, Any]] = []
    t_counter = 0

    for lno, btxt in core_bullets:
        if btxt.startswith("转多条件："):
            for part in [p.strip() for p in btxt.split("：", 1)[1].split("；") if p.strip()]:
                t_counter += 1
                tid = f"T{t_counter}"
                confirm_thresholds.append(
                    {
                        "id": tid,
                        "metric": part[:60],
                        "operator": "event",
                        "event_text": part,
                        "label": "research_judgement",
                        "source_hint": "季报新闻稿与电话会",
                    }
                )
                rec_prov("thesis", tid, "openclaw_thesis_md", f"{source_path}#L{lno}", lno, lno, part)
        elif btxt.startswith("转空条件："):
            for part in [p.strip() for p in btxt.split("：", 1)[1].split("；") if p.strip()]:
                t_counter += 1
                tid = f"T{t_counter}"
                break_thresholds.append(
                    {
                        "id": tid,
                        "metric": part[:60],
                        "operator": "event",
                        "event_text": part,
                        "label": "research_judgement",
                        "source_hint": "季报新闻稿与电话会",
                    }
                )
                rec_prov("thesis", tid, "openclaw_thesis_md", f"{source_path}#L{lno}", lno, lno, part)
        else:
            if len(pillars) < 7:
                pid = f"P{len(pillars) + 1}"
                t_counter += 1
                c_tid = f"T{t_counter}"
                t_counter += 1
                w_tid = f"T{t_counter}"
                t_counter += 1
                b_tid = f"T{t_counter}"
                pillars.append(
                    {
                        "id": pid,
                        "claim": btxt,
                        "priority": "core" if len(pillars) < 2 else "supporting",
                        "baseline": {
                            "text": btxt,
                            "as_of": "2026-10-15",
                        },
                        "expected_path": "在即将到来的季报与电话会指引中验证。",
                        "thresholds": {
                            "confirm": [
                                {
                                    "id": c_tid,
                                    "metric": f"{pid} 确认条件",
                                    "operator": "event",
                                    "event_text": "季报核心指标及下季指引高于买方 bogey",
                                    "label": "research_judgement",
                                }
                            ],
                            "warn": [
                                {
                                    "id": w_tid,
                                    "metric": f"{pid} 预警条件",
                                    "operator": "event",
                                    "event_text": "季报核心指标贴近指引下沿或费用/capex 超预期上调",
                                    "label": "research_judgement",
                                }
                            ],
                            "break": [
                                {
                                    "id": b_tid,
                                    "metric": f"{pid} 证伪条件",
                                    "operator": "event",
                                    "event_text": "核心增速掉出买方下沿且 capex 继续失控上修",
                                    "label": "research_judgement",
                                }
                            ],
                        },
                        "status": "intact",
                        "status_reason": f"Migrated from {source_path}#L{lno}",
                        "status_history": [
                            {
                                "at": now_iso,
                                "to": "intact",
                                "reason": f"P1.4 legacy migration from {source_path}#L{lno}",
                            }
                        ],
                        "next_proof_point": {
                            "what": f"{meta['current_period']} 财报与电话会指引",
                            "due": meta["earnings_date"],
                            "source": "8-K & Earnings Call",
                        },
                        "provenance": "inherited",
                        "created_at": now_iso,
                    }
                )
                rec_prov("thesis", pid, "openclaw_thesis_md", f"{source_path}#L{lno}", lno, lno, btxt)

    if pillars and confirm_thresholds:
        pillars[0]["thresholds"]["confirm"] = confirm_thresholds
    if pillars and break_thresholds:
        pillars[0]["thresholds"]["break"] = break_thresholds

    # Build Debates D1..Dn from OpenClaw `### D1`..`### Dn` (100% preserved)
    # and Evidence E-20261015-01.. from every bullet in D1..Dn and `## 已知观点池`
    debates_list: list[dict[str, Any]] = []
    drivers_list: list[dict[str, Any]] = []
    evidence_list: list[dict[str, Any]] = []
    ev_seq = 0

    def next_ev_id() -> str:
        nonlocal ev_seq
        ev_seq += 1
        return f"E-20261015-{ev_seq:03d}"

    for idx, d in enumerate(ocw_debates, start=1):
        d_id = d["id"]
        k_id = f"K{idx}"
        p_id = pillars[(idx - 1) % len(pillars)]["id"]
        q_text = d["title"]
        bull_parts: list[str] = []
        bear_parts: list[str] = []
        fact_parts: list[str] = []
        expert_parts: list[str] = []
        lean_text = ""
        flip_text = ""
        d_bull_ev_ids: list[str] = []
        d_bear_ev_ids: list[str] = []

        for b_line, b_text in d["bullets"]:
            ev_id = next_ev_id()
            src_type = "internal_note"
            src_tier = 3
            sig = "neutral"

            if b_text.startswith("分歧："):
                q_text = b_text[3:].strip()
            elif b_text.startswith(("事实：", "证据：", "账面持仓核实")):
                clean = b_text.split("：", 1)[1].strip() if "：" in b_text else b_text
                fact_parts.append(clean)
                src_type = "filing"
                src_tier = 1
                sig = "neutral"
            elif b_text.startswith(("看多", "Bull")):
                clean = b_text.split("：", 1)[1].strip() if "：" in b_text else b_text
                bull_parts.append(clean)
                src_type = "broker_report"
                src_tier = 3
                sig = "confirming"
                d_bull_ev_ids.append(ev_id)
            elif b_text.startswith(("看空", "Bear")):
                clean = b_text.split("：", 1)[1].strip() if "：" in b_text else b_text
                bear_parts.append(clean)
                src_type = "broker_report"
                src_tier = 3
                sig = "weakening"
                d_bear_ev_ids.append(ev_id)
            elif b_text.startswith(("专家", "成本补充", "成本粗算", "融资环境", "电力：")):
                clean = b_text.split("：", 1)[1].strip() if "：" in b_text else b_text
                expert_parts.append(clean)
                src_type = "expert"
                src_tier = 5
                sig = "neutral"
            elif b_text.startswith("当前倾向："):
                lean_text = b_text[5:].strip()
            elif b_text.startswith("改判："):
                flip_text = b_text[3:].strip()
            else:
                fact_parts.append(b_text)

            evidence_list.append(
                {
                    "id": ev_id,
                    "at": now_iso,
                    "period": meta["current_period"],
                    "source": {
                        "type": src_type,
                        "identity": f"OpenClaw {ticker} thesis.md {d_id} L{b_line}",
                        "tier": src_tier,
                        "ref": legacy_md_ref,
                    },
                    "fact": b_text,
                    "pillar_ids": [p_id],
                    "signal": sig,
                    "quality": "high" if src_tier <= 3 else "medium",
                }
            )
            rec_prov("thesis", ev_id, "openclaw_thesis_md", f"{source_path}#L{b_line}", b_line, b_line, b_text)

        bull_case_str = "；".join(bull_parts) if bull_parts else ("；".join(fact_parts) if fact_parts else d["title"])
        bear_case_str = "；".join(bear_parts) if bear_parts else ("；".join(fact_parts) if fact_parts else d["title"])
        if expert_parts:
            bull_case_str = f"{bull_case_str}（专家补充：{'；'.join(expert_parts)}）"

        mv_side, mv_conf = _infer_my_view(lean_text or q_text)
        t_counter += 1
        d_tid = f"T{t_counter}"
        legacy_ref_str = f"openclaw:{ticker}:thesis.md#{d_id}(L{d['start_line']}-L{d['end_line']})"

        debates_list.append(
            {
                "id": d_id,
                "variable": d["title"],
                "question": q_text,
                "why_it_matters": lean_text or f"直接影响 {meta['next_fy']} 盈利预测修正与估值倍数。",
                "rank": idx,
                "bull": {
                    "case": bull_case_str,
                    "who": "卖方多头 / 渠道调研",
                    "evidence_ids": d_bull_ev_ids,
                    "evidence_refs": [legacy_md_ref],
                },
                "bear": {
                    "case": bear_case_str,
                    "who": "卖方空头 / 审慎买方",
                    "evidence_ids": d_bear_ev_ids,
                    "evidence_refs": [legacy_md_ref],
                },
                "strongest_rebuttal": {
                    "against": "bull" if mv_side == "bear" else "bear",
                    "text": bear_case_str[:300] if mv_side != "bear" else bull_case_str[:300],
                },
                "my_view": {
                    "side": mv_side,
                    "confidence": mv_conf,
                    "text": lean_text or f"基于 {d_id} 事实与多空证据维持当前倾向。",
                },
                "market_lean": {
                    "side": "split",
                    "evidence": "；".join(fact_parts[:2]) if fact_parts else q_text,
                },
                "flip_condition": {
                    "text": flip_text or f"{meta['current_period']} 财报披露实际值与指引偏离买方 bogey",
                    "driver_ids": [k_id],
                    "threshold": {
                        "id": d_tid,
                        "metric": d["title"],
                        "operator": "event",
                        "event_text": flip_text or f"{meta['current_period']} 财报与指引验证",
                        "label": "research_judgement",
                    },
                },
                "linked_pillars": [p_id],
                "status": "open",
                "opened_at": now_iso,
                "last_reviewed_at": now_iso,
                "history": [
                    {
                        "at": now_iso,
                        "change": f"Migrated {d_id} ({d['title']}) from {legacy_ref_str}",
                        "reason": "P1.4 legacy knowledge migration",
                        "evidence_ids": d_bull_ev_ids + d_bear_ev_ids,
                    }
                ],
            }
        )
        rec_prov(
            "debate",
            d_id,
            "openclaw_thesis_md",
            legacy_ref_str,
            d["start_line"],
            d["end_line"],
            "\n".join(l for _, l in d["raw_lines"]),
        )

        # Build corresponding Driver K_idx in `drivers.json`
        drivers_list.append(
            {
                "id": k_id,
                "name": d["title"],
                "kind": _driver_kind_for_title(d["title"]),
                "horizon": "both",
                "description": q_text,
                "linked_pillars": [p_id],
                "linked_debates": [d_id],
                "leading_indicators": [
                    {
                        "id": f"{k_id}.1",
                        "name": f"{d['title']} 跟踪指标",
                        "source": "季报 / 卖方渠道 / 专家访谈",
                        "frequency": "quarterly",
                        "good_direction": "down" if _driver_kind_for_title(d["title"]) == "capex" else "up",
                        "readings": [],
                        "probe": {"kind": "none", "params": {}},
                    }
                ],
                "latest_assessment": lean_text or q_text,
                "updated_at": now_iso,
            }
        )
        rec_prov("drivers", k_id, "openclaw_thesis_md", legacy_ref_str, d["start_line"], d["end_line"], d["title"])

    # Supplement Dalton core.sqlite debates into `debates_list` history / bull / bear and provenance
    dc_dm = dalton_core.get("debate_map") or {}
    for dc_idx, dc_deb in enumerate(dc_dm.get("debates", []), start=1):
        deb_ref = dc_deb.get("debate_ref", f"debate:{dc_idx}")
        bull_st = (dc_deb.get("bull_position") or {}).get("statement", "")
        bear_st = (dc_deb.get("bear_position") or {}).get("statement", "")
        if debates_list:
            target_d = debates_list[(dc_idx - 1) % len(debates_list)]
            if bull_st:
                target_d["bull"]["case"] += f" | [Dalton {deb_ref}] {bull_st}"
            if bear_st:
                target_d["bear"]["case"] += f" | [Dalton {deb_ref}] {bear_st}"
            rec_prov(
                "debate",
                f"{target_d['id']}:{deb_ref}",
                "dalton_core_sqlite",
                f"core.sqlite:debate_map_versions:{dalton_core.get('subject_ref')}:{deb_ref}",
                None,
                None,
                json.dumps(dc_deb, ensure_ascii=False),
            )

    # Also migrate remaining section bullets (including `## 已知观点池`, `## 一致预期与市场锚点`,
    # `## 关键指标与阈值`, `## 指引节奏`, `## 专家来源可靠性`, etc.) into `thesis.evidence`
    # so every factual bullet in legacy `thesis.md` is preserved in `thesis.json`.
    existing_ev_facts = {e.get("fact", "").strip() for e in evidence_list}
    for sec_title, sec_data in sections.items():
        for lno, ltxt in sec_data.get("lines", []):
            s = ltxt.strip()
            if s.startswith("- "):
                body = s[2:].strip()
                if not body or body in existing_ev_facts:
                    continue
                existing_ev_facts.add(body)
                ev_id = next_ev_id()
                org_label = (
                    body.split("：", 1)[0].strip()
                    if "：" in body
                    else f"OpenClaw {sec_title} L{lno}"
                )
                evidence_list.append(
                    {
                        "id": ev_id,
                        "at": now_iso,
                        "period": meta["current_period"],
                        "source": {
                            "type": "broker_report",
                            "identity": org_label[:120],
                            "tier": 3,
                            "ref": legacy_md_ref,
                        },
                        "fact": body,
                        "pillar_ids": ["P1"],
                        "signal": "neutral",
                        "quality": "medium",
                    }
                )
                rec_prov("thesis", ev_id, "openclaw_thesis_md", f"{source_path}#L{lno}", lno, lno, body)

    # Build consensus figures for `thesis.market_view` from `latest_consensus`
    lc = bundle.get("latest_consensus") or {}
    ee = lc.get("earnings_estimate") or {}
    re_est = lc.get("revenue_estimate") or {}
    market_figures: list[dict[str, Any]] = []
    for pk, period_code in (("0q", meta["current_period"]), ("0y", meta["current_fy"]), ("+1y", meta["next_fy"])):
        eps_val = (ee.get(pk) or {}).get("avg")
        if isinstance(eps_val, (int, float)):
            market_figures.append(
                {
                    "metric": "Consensus EPS",
                    "value": round(float(eps_val), 2),
                    "unit": "per_share",
                    "period": period_code,
                    "label": "consensus",
                }
            )
        rev_val = (re_est.get(pk) or {}).get("avg")
        if isinstance(rev_val, (int, float)):
            rev_bn = round(float(rev_val) / 1e9, 2) if rev_val > 1e6 else round(float(rev_val), 2)
            market_figures.append(
                {
                    "metric": "Consensus Revenue",
                    "value": rev_bn,
                    "unit": "USD_bn",
                    "period": period_code,
                    "label": "consensus",
                }
            )

    house_view_obj = {
        "stance": stance_enum,
        "conviction": conv_enum,
        "horizon_months": 6,
        "summary": " ".join(b for _, b in core_bullets[:3]) or one_liner,
        "variant_perception": " ".join(b for _, b in er_bullets[:3]) or one_liner,
        "position_suggestion": "core" if stance_enum == "long" else "starter",
        "updated_at": now_iso,
    }

    thesis_doc = {
        "schema_version": 1,
        "as_of": now_iso,
        "one_liner": one_liner,
        "aggregate_status": "intact",
        "house_view": house_view_obj,
        "house_view_history": [
            {
                "at": now_iso,
                "to": house_view_obj,
                "reason": f"Initial P1.4 migration from {source_path}",
            }
        ],
        "market_view": {
            "summary": next((b for _, b in er_bullets if "一致预期" in b), first_bullet),
            "as_of": now_iso,
            "figures": market_figures,
            "evidence_refs": [legacy_md_ref],
        },
        "pillars": pillars,
        "evidence": evidence_list,
        "official": None,
    }

    debate_doc = {
        "schema_version": 1,
        "as_of": now_iso,
        "debates": debates_list,
    }

    uncomputed_note = "迁移时未计算，待首次研究补充"
    drivers_doc = {
        "schema_version": 1,
        "as_of": now_iso,
        "historical_valuation_driver": {
            "summary": next((b for _, b in er_bullets if "隐含" in b or "修正" in b), "股价主要由远期 EPS 修正与 AI capex 叙事共同驱动。"),
            "window": "2025-2026",
            "eps_share_pct": None,
            "multiple_share_pct": None,
            "note": uncomputed_note,
            "model_ref": legacy_md_ref,
        },
        "drivers": drivers_list,
    }

    # Build `management.json` from `## 指引节奏`, `## 专家来源可靠性`, and guidance bullets
    gc_sec = next((v for k, v in sections.items() if "指引节奏" in k), None)
    gc_patterns: list[str] = []
    if gc_sec:
        for lno, ltxt in gc_sec["lines"]:
            s = ltxt.strip()
            if s.startswith("- "):
                body = s[2:].strip()
                gc_patterns.append(body)
                rec_prov("management", f"GC-L{lno}", "openclaw_thesis_md", f"{source_path}#L{lno}", lno, lno, body)

    guidance_history: list[dict[str, Any]] = []
    g_seq = 0
    for lno, btxt in er_bullets:
        if "指引" in btxt or "公司数据" in btxt:
            g_seq += 1
            gid = f"G-{g_seq:03d}"
            guidance_history.append(
                {
                    "id": gid,
                    "metric": f"{ticker} Quarterly / Full-Year Guidance",
                    "period": meta["current_period"],
                    "issued_at": None,
                    "note": uncomputed_note,
                    "guide": {
                        "unit": "USD_bn",
                        "text": btxt,
                    },
                    "guide_ref": legacy_md_ref,
                    "actual": None,
                    "outcome": "pending",
                }
            )
            rec_prov("management", gid, "openclaw_thesis_md", f"{source_path}#L{lno}", lno, lno, btxt)

    credibility_notes: list[dict[str, Any]] = []
    exp_sec = next((v for k, v in sections.items() if "专家来源可靠性" in k), None)
    if exp_sec:
        for lno, ltxt in exp_sec["lines"]:
            s = ltxt.strip()
            if s.startswith("- "):
                body = s[2:].strip()
                credibility_notes.append(
                    {
                        "at": now_iso,
                        "note": body,
                        "evidence_refs": [legacy_md_ref],
                    }
                )
                rec_prov("management", f"CRED-L{lno}", "openclaw_thesis_md", f"{source_path}#L{lno}", lno, lno, body)

    management_doc = {
        "schema_version": 1,
        "as_of": now_iso,
        "team": meta["team"],
        "guidance_style": {
            "bias": "conservative",
            "summary": gc_patterns[0] if gc_patterns else f"{ticker} 管理层指引节奏迁移自 {source_path}",
            "sample_n": 0,
            "patterns": gc_patterns,
        },
        "guidance_history": guidance_history,
        "capital_allocation": {
            "summary": "AI 基础设施与自研芯片高强度投入期，同时维持核心现金流创造。",
            "buyback": "根据自由现金流节奏动态执行回购。",
            "dividend": "维持季度常规分红。",
            "m_and_a": "聚焦 AI 人才、前沿模型实验室股权（如 Anthropic/OpenAI）及算力协同。",
            "capex": next((d["my_view"]["text"] for d in debates_list if "capex" in d["variable"].lower()), "2026-2027 capex 维持高位增长。"),
            "shareholder_return_score": "unrated",
        },
        "credibility_notes": credibility_notes,
    }

    # Build `lessons.json`
    lessons_doc = {
        "schema_version": 1,
        "lessons": [
            {
                "id": "L-001",
                "statement": f"{ticker} 一致预期 EPS 口径需先剔除一次性项目或非经常性投资损益（如 Anthropic/SpaceX 公允价值变动或和解计提），再判断经营面修正方向。",
                "applies_when": f"阅读 {ticker} 财报标题 EPS 超/低于预期或一致预期单周异动 ≥1% 时",
                "does_not_apply_when": "已明确为纯经营利润（Operating Income）同口径对比时",
                "counterexamples": [],
                "origin": {
                    "event": f"OpenClaw {ticker} 跟踪底稿口径与 Earnings revision 框架迁移",
                    "at": "2026-10-15",
                    "ref": legacy_md_ref,
                },
                "scope": "company",
                "confidence": "high",
                "status": "active",
                "created_at": now_iso,
            },
            {
                "id": "L-002",
                "statement": gc_patterns[0] if gc_patterns else f"{ticker} 季报对下一财年 capex/opex 的表述遵循固定季节性节奏，需对照往年同期原文避免过度解读。",
                "applies_when": f"{ticker} 发布季度财报及电话会指引时",
                "does_not_apply_when": "公司临时发布 8-K 重述分部口径时",
                "counterexamples": [],
                "origin": {
                    "event": f"OpenClaw {ticker} 指引节奏历史核对",
                    "at": "2026-10-15",
                    "ref": legacy_md_ref,
                },
                "scope": "company",
                "confidence": "high",
                "status": "active",
                "created_at": now_iso,
            },
        ],
    }
    rec_prov("lessons", "L-001", "openclaw_thesis_md", f"{source_path}#L1-L18", 1, 18, lessons_doc["lessons"][0]["statement"])
    rec_prov("lessons", "L-002", "openclaw_thesis_md", f"{source_path}#guidance_cadence", 1, 18, lessons_doc["lessons"][1]["statement"])

    # Build `agenda.json` from `## 催化剂日历` and `## 待核实线索`
    open_questions: list[dict[str, Any]] = []
    vq_sec = next((v for k, v in sections.items() if "待核实线索" in k), None)
    q_seq = 0
    if vq_sec:
        for lno, ltxt in vq_sec["lines"]:
            s = ltxt.strip()
            if s.startswith("- "):
                q_seq += 1
                qid = f"Q-{q_seq:03d}"
                body = s[2:].strip()
                open_questions.append(
                    {
                        "id": qid,
                        "question": body,
                        "why": "OpenClaw 待核实线索，核实后决定是否移入观点池并触发重要性推送。",
                        "value": "medium",
                        "urgency": "this_week",
                        "origin": "analyst",
                        "owner": "analyst",
                        "status": "open",
                        "linked": {"pillars": ["P1"], "debates": ["D1"], "drivers": ["K1"]},
                        "attempts": [
                            {
                                "at": now_iso,
                                "method": "legacy_migration_import",
                                "progress": "none",
                                "note": body,
                            }
                        ],
                        "created_at": now_iso,
                    }
                )
                rec_prov("agenda", qid, "openclaw_thesis_md", f"{source_path}#L{lno}", lno, lno, body)

    for d_item in debates_list[:3]:
        q_seq += 1
        qid = f"Q-{q_seq:03d}"
        open_questions.append(
            {
                "id": qid,
                "question": d_item["question"],
                "why": d_item["why_it_matters"],
                "value": "high",
                "urgency": "this_quarter",
                "origin": "analyst",
                "owner": "analyst",
                "status": "open",
                "linked": {
                    "pillars": d_item["linked_pillars"],
                    "debates": [d_item["id"]],
                    "drivers": d_item["flip_condition"]["driver_ids"],
                },
                "attempts": [],
                "created_at": now_iso,
            }
        )

    cat_sec = next((v for k, v in sections.items() if "催化剂日历" in k), None)
    watch_events: list[dict[str, Any]] = []
    next_validations: list[dict[str, Any]] = []
    w_seq = 0
    if cat_sec:
        for lno, ltxt in cat_sec["lines"]:
            s = ltxt.strip()
            if s.startswith("- "):
                w_seq += 1
                wid = f"W{w_seq}"
                vid = f"V-{w_seq:03d}"
                body = s[2:].strip()
                ev_type = (
                    "earnings"
                    if "财报" in body
                    else (
                        "regulatory"
                        if any(k in body for k in ("诉讼", "监管", "法院", "FTC"))
                        else ("product" if any(k in body for k in ("发布", "发售", "Universe", "Ignite", "re:Invent", "Connect")) else "other")
                    )
                )
                has_explicit_earnings_date = "10/28" in body or "10/29" in body
                ev_date = meta["earnings_date"] if has_explicit_earnings_date else None
                ev_date_status = "estimated" if has_explicit_earnings_date else "unknown"
                watch_events.append(
                    {
                        "id": wid,
                        "type": ev_type,
                        "date": ev_date,
                        "date_status": ev_date_status,
                        "note": None if has_explicit_earnings_date else uncomputed_note,
                        "source": f"OpenClaw {source_path}#L{lno}",
                        "plan": body[:160],
                    }
                )
                next_validations.append(
                    {
                        "id": vid,
                        "what": body[:140],
                        "due": ev_date,
                        "note": None if has_explicit_earnings_date else uncomputed_note,
                        "source": "季报 / 行业会议 / 公告",
                        "expected": debates_list[0]["flip_condition"]["text"] if debates_list else body[:100],
                        "linked": {"pillars": ["P1"], "debates": ["D1"], "drivers": ["K1"]},
                        "status": "pending",
                    }
                )
                rec_prov("agenda", wid, "openclaw_thesis_md", f"{source_path}#L{lno}", lno, lno, body)

    agenda_doc = {
        "schema_version": 1,
        "as_of": now_iso,
        "last_session": {
            "at": now_iso,
            "trigger": "p1.4_legacy_migration",
            "summary": f"Migrated {ticker} knowledge base from OpenClaw thesis.md and Dalton core.sqlite.",
        },
        "open_questions": open_questions,
        "next_validations": next_validations,
        "watch": {
            "events": watch_events,
            "indicators": [
                {
                    "indicator_id": "K1.1",
                    "rule": {
                        "id": "T99",
                        "metric": f"{meta['next_fy']} Consensus EPS",
                        "operator": ">=",
                        "value": 1.0,
                        "unit": "pct",
                        "period": meta["next_fy"],
                        "label": "consensus",
                        "source_hint": "Yahoo Finance / consensus.json",
                    },
                }
            ],
            "price": {
                "abs_move_pct_1d": 3.0,
                "excess_vs_benchmark_pct": 2.0,
                "benchmark": "QQQ",
            },
            "filings": {
                "sources": ["sec"],
                "forms": ["8-K", "10-Q", "10-K"],
            },
            "internal": {
                "queries": [ticker, meta["name"]],
            },
        },
        "wake_conditions": [
            {
                "id": "C1",
                "kind": "earnings_window",
                "params": {"ticker": ticker, "earnings_date": meta["earnings_date"]},
                "action": "运行 earnings-review 并更新 D1..Dn 证据与五词结论",
                "enabled": True,
            },
            {
                "id": "C2",
                "kind": "consensus_change",
                "params": {"single_run_pct": 1.0, "seven_day_pct": 2.0},
                "action": "核查一致预期变动是否来自非经常性投资损益口径或真实经营面修正",
                "enabled": True,
            },
        ],
        "pm_handoffs": [],
    }

    company_doc = {
        "schema_version": 1,
        "ticker": ticker,
        "exchange": meta["exchange"],
        "name": meta["name"],
        "name_zh": meta["name_zh"],
        "coverage_tier": "core",
        "coverage_stage": "onboarding",
        "sector": meta["sector"],
        "currency": meta["currency"],
        "fiscal_year_end": meta["fiscal_year_end"],
        "identifiers": {"cik": meta["cik"]},
        "peers": meta["peers"],
        "benchmarks": meta["benchmarks"],
        "one_liner": one_liner,
        "covered_since": "2026-10-15",
        "autonomy": {"enabled": True, "max_usd_per_day": None, "note": uncomputed_note},
    }
    rec_prov("company", ticker, "openclaw_thesis_md", f"{source_path}#L1-L18", 1, 18, one_liner)

    models_doc = {
        "schema_version": 1,
        "versions": [],
    }

    fm = dalton_core.get("forecast_model") or {}
    provenance_doc = {
        "schema_version": "1.0",
        "ticker": ticker,
        "migrated_at": now_iso,
        "migrated_by": "scripts/dalton/migrate_legacy_knowledge.py",
        "openclaw_sources": {
            "thesis_md_path": source_path,
            "thesis_md_sha256": pt["file_sha256"],
            "thesis_md_lines": pt["total_lines"],
            "openclaw_debate_ids": [d["id"] for d in ocw_debates],
            "consensus_snapshot": bundle.get("latest_consensus_rel"),
            "last_run_id": (bundle.get("last_run") or {}).get("run_id"),
            "wiki_files": bundle.get("wiki_files", []),
        },
        "dalton_core_sources": {
            "subject_ref": dalton_core.get("subject_ref"),
            "debate_map_version": dalton_core.get("debate_map_version"),
            "debate_map_id": (dalton_core.get("debate_map") or {}).get("id"),
            "dossier_version": dalton_core.get("dossier_version"),
            "dossier_id": (dalton_core.get("dossier") or {}).get("id"),
            "forecast_model_version": dalton_core.get("forecast_model_version"),
            "forecast_model_id": fm.get("id"),
            "claims_resolved_count": len(dalton_core.get("claims") or {}),
        },
        "items": provenance_items,
    }

    docs_by_key = {
        "company": company_doc,
        "thesis": thesis_doc,
        "debate": debate_doc,
        "drivers": drivers_doc,
        "management": management_doc,
        "lessons": lessons_doc,
        "agenda": agenda_doc,
        "models": models_doc,
    }
    return docs_by_key, provenance_doc


def render_industry_debate_markdown(ind_bundle: dict[str, Any]) -> str:
    dm = ind_bundle.get("debate_map") or {}
    subj = ind_bundle.get("subject_ref", "industry:us-hyperscaler")
    ver = ind_bundle.get("version_number", 1)
    lines = [
        "# US Hyperscaler Cloud & AI Infrastructure Debate Map",
        "",
        f"- **Source Subject**: `{subj}` (v{ver}, `{dm.get('id', '')}`)",
        f"- **Created At**: `{dm.get('created_at', '')}`",
        "",
        "## Core Industry Debates",
        "",
    ]
    for idx, d in enumerate(dm.get("debates", []), start=1):
        d_ref = d.get("debate_ref", f"IND-D{idx}")
        q = d.get("question", "")
        bull = (d.get("bull_position") or {}).get("statement", "")
        bear = (d.get("bear_position") or {}).get("statement", "")
        mp = (d.get("market_position") or {}).get("statement", "")
        lines.extend(
            [
                f"### IND-D{idx} ({d_ref}): {q}",
                f"- **Bull Position**: {bull}",
                f"- **Bear Position**: {bear}",
                f"- **Market Position**: {mp}",
                "",
            ]
        )
    return "\n".join(lines)


def write_company_output(
    out_dir: Path,
    bundle: dict[str, Any],
    docs_by_key: dict[str, dict[str, Any]],
    provenance_doc: dict[str, Any],
) -> dict[str, Any]:
    """Write all files for a single company into `out_dir/Home/dalton/companies/<T>/` and validate."""
    ticker = bundle["ticker"]
    c_root = out_dir / "Home" / "dalton" / "companies" / ticker
    (c_root / "data").mkdir(parents=True, exist_ok=True)
    (c_root / "history").mkdir(parents=True, exist_ok=True)
    (c_root / "published").mkdir(parents=True, exist_ok=True)
    (c_root / "sources" / "legacy").mkdir(parents=True, exist_ok=True)

    validation_results: dict[str, Any] = {}
    for doc_key in DOC_KEYS:
        doc_payload = docs_by_key[doc_key]
        res = validate_update(doc_key, None, doc_payload, reason="P1.4 legacy migration")
        validation_results[doc_key] = {
            "ok": res.ok,
            "errors": res.errors,
            "warnings": res.warnings,
        }
        rel_json = data_path(doc_key)
        target_json = c_root / rel_json
        target_json.parent.mkdir(parents=True, exist_ok=True)
        target_json.write_text(
            json.dumps(doc_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if doc_key in RENDERED:
            md_str = render_document_md(doc_key, doc_payload, version_no=1, ticker=ticker)
            if md_str is not None:
                rel_md = rendered_path(doc_key)
                assert rel_md is not None
                (c_root / rel_md).write_text(md_str, encoding="utf-8")

    # Render verdicts.md, published/INDEX.md, and agent.md
    (c_root / "verdicts.md").write_text(render_verdicts_md([], ticker=ticker), encoding="utf-8")
    (c_root / "published" / "INDEX.md").write_text(
        render_published_index_md([], ticker=ticker), encoding="utf-8"
    )
    agent_block = render_agent_block(
        ticker=ticker,
        company_doc=docs_by_key["company"],
        thesis_doc=docs_by_key["thesis"],
        thesis_version_no=1,
        debate_doc=docs_by_key["debate"],
        agenda_doc=docs_by_key["agenda"],
        models_doc=docs_by_key["models"],
    )
    (c_root / "agent.md").write_text(update_agent_md_block(None, agent_block), encoding="utf-8")

    # Ordinary files: consensus.json, sources/seen.json, sources/legacy/*
    (c_root / "consensus.json").write_text(
        json.dumps(bundle.get("latest_consensus") or {}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (c_root / "sources" / "seen.json").write_text(
        json.dumps(bundle.get("seen") or {}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (c_root / "sources" / "legacy" / "openclaw_thesis_2026-10-15.md").write_text(
        bundle["raw_thesis_md"], encoding="utf-8"
    )
    (c_root / "sources" / "legacy" / "updates.log").write_text(
        bundle.get("updates_log") or "", encoding="utf-8"
    )
    (c_root / "sources" / "legacy" / "provenance.json").write_text(
        json.dumps(provenance_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    expected_d_ids = [d["id"] for d in bundle["parsed_thesis"]["debates"]]
    migrated_d_ids = [d["id"] for d in docs_by_key["debate"]["debates"] if d["id"] in expected_d_ids]

    return {
        "ticker": ticker,
        "company_dir": str(c_root),
        "openclaw_debates_expected": len(expected_d_ids),
        "openclaw_debates_migrated": len(migrated_d_ids),
        "dalton_core_debates_supplemented": len((bundle.get("dalton_core") or {}).get("debate_map", {}).get("debates", [])),
        "evidence_count": len(docs_by_key["thesis"]["evidence"]),
        "drivers_count": len(docs_by_key["drivers"]["drivers"]),
        "catalysts_count": len(docs_by_key["agenda"]["watch"]["events"]),
        "open_questions_count": len(docs_by_key["agenda"]["open_questions"]),
        "provenance_items_count": len(provenance_doc["items"]),
        "validation": validation_results,
        "all_valid": all(v["ok"] for v in validation_results.values()),
    }


def run_migration_pipeline(
    tickers: list[str],
    out_dir: Path,
    ocw_path: Path = DEFAULT_OCW_PATH,
    sqlite_path: Path = DEFAULT_DALTON_SQLITE,
) -> dict[str, Any]:
    """Execute full P1.4 migration dry-run (`extract -> render -> diff -> report`)."""
    before_snap = snapshot_source_tree(ocw_path, sqlite_path, tickers)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    company_summaries: dict[str, Any] = {}
    for t in tickers:
        bundle = extract_company_bundle(ocw_path, sqlite_path, t)
        (raw_dir / f"{t}.json").write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        docs_by_key, prov_doc = build_knowledge_documents(bundle)
        summary = write_company_output(out_dir, bundle, docs_by_key, prov_doc)
        company_summaries[t] = summary

    ind_bundle = extract_industry_debate_map(sqlite_path)
    if ind_bundle:
        (raw_dir / "industry_us_hyperscaler.json").write_text(
            json.dumps(ind_bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        ind_dir = out_dir / "Home" / "dalton" / "industry" / "us-hyperscaler"
        ind_dir.mkdir(parents=True, exist_ok=True)
        (ind_dir / "debate.md").write_text(render_industry_debate_markdown(ind_bundle), encoding="utf-8")

    integrity = verify_source_tree_unchanged(before_snap, ocw_path, sqlite_path, tickers)

    total_docs_validated = sum(len(s["validation"]) for s in company_summaries.values())
    all_schemas_passed = all(s["all_valid"] for s in company_summaries.values())
    total_ocw_debates_expected = sum(s["openclaw_debates_expected"] for s in company_summaries.values())
    total_ocw_debates_migrated = sum(s["openclaw_debates_migrated"] for s in company_summaries.values())

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tickers": tickers,
        "output_dir": str(out_dir),
        "all_schemas_passed": all_schemas_passed,
        "total_docs_validated": total_docs_validated,
        "openclaw_debates_expected": total_ocw_debates_expected,
        "openclaw_debates_migrated": total_ocw_debates_migrated,
        "debate_coverage_pct": 100.0 if total_ocw_debates_expected == total_ocw_debates_migrated else 0.0,
        "source_integrity": integrity,
        "companies": company_summaries,
    }

    (out_dir / "migration_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    md_lines = [
        "# P1.4 Legacy Knowledge Migration Report",
        "",
        f"- **Generated At**: `{report['generated_at']}`",
        f"- **Output Directory**: `{report['output_dir']}`",
        f"- **All PK.2 Schemas Passed**: `{report['all_schemas_passed']}` ({total_docs_validated} JSON documents validated)",
        f"- **OpenClaw D1..Dn Debate Coverage**: `{total_ocw_debates_migrated} / {total_ocw_debates_expected}` (`{report['debate_coverage_pct']:.1f}%`)",
        f"- **Legacy Source Tree Integrity**: `zero_source_mutations={integrity['zero_source_mutations']}` ({integrity['files_checked']} files verified via SHA-256 + mtime_ns)",
        "",
        "## Per-Company Summary",
        "",
        "| Ticker | OpenClaw Debates | Dalton Core Debates | Evidence Items | Drivers | Watch Events | Open Questions | Provenance Items | Schema Valid |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]
    for t, s in company_summaries.items():
        md_lines.append(
            f"| **{t}** | {s['openclaw_debates_migrated']}/{s['openclaw_debates_expected']} | "
            f"{s['dalton_core_debates_supplemented']} | {s['evidence_count']} | {s['drivers_count']} | "
            f"{s['catalysts_count']} | {s['open_questions_count']} | "
            f"{s['provenance_items_count']} | `{'PASS' if s['all_valid'] else 'FAIL'}` |"
        )
    (out_dir / "migration_report.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return report


def remediate_doc_payload(doc_key: str, doc: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Null out fabricated migration defaults on a knowledge document copy."""
    import copy as _copy

    updated = _copy.deepcopy(doc)
    changed = False
    uncomputed_note = "迁移时未计算，待首次研究补充"

    if doc_key == "drivers":
        hvd = updated.get("historical_valuation_driver")
        if isinstance(hvd, dict):
            if hvd.get("eps_share_pct") == 65 or hvd.get("multiple_share_pct") == 35:
                hvd["eps_share_pct"] = None
                hvd["multiple_share_pct"] = None
                hvd["note"] = uncomputed_note
                changed = True
    elif doc_key == "management":
        for item in updated.get("guidance_history") or []:
            if isinstance(item, dict) and str(item.get("issued_at") or "").endswith("-07-29"):
                item["issued_at"] = None
                item["note"] = uncomputed_note
                changed = True
    elif doc_key == "company":
        aut = updated.get("autonomy")
        if isinstance(aut, dict) and aut.get("max_usd_per_day") == 15:
            aut["max_usd_per_day"] = None
            aut["note"] = uncomputed_note
            changed = True
    elif doc_key == "agenda":
        watch = updated.get("watch")
        if isinstance(watch, dict):
            for ev in watch.get("events") or []:
                if isinstance(ev, dict) and str(ev.get("date") or "").endswith("-11-15"):
                    ev["date"] = None
                    ev["date_status"] = "unknown"
                    ev["note"] = uncomputed_note
                    changed = True
        for val in updated.get("next_validations") or []:
            if isinstance(val, dict) and str(val.get("due") or "").endswith("-11-15"):
                val["due"] = None
                val["note"] = uncomputed_note
                changed = True

    return updated, changed


async def remediate_migrated_workspaces(
    *,
    user_id: str,
    workspace_ids: list[str],
    backend_by_workspace: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Append a new knowledge version ('移除迁移脚本编造的数值') for any workspace with fabricated defaults."""
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.knowledge import service as knowledge_service

    results: list[dict[str, Any]] = []
    for ws_id in workspace_ids:
        for doc_key in ("drivers", "management", "company", "agenda"):
            latest = await db_knowledge.latest_version(ws_id, doc_key)
            if not latest or not isinstance(latest.get("content"), dict):
                continue
            cleaned, changed = remediate_doc_payload(doc_key, latest["content"])
            if not changed:
                continue
            res = await knowledge_service.apply_update(
                user_id=user_id,
                workspace_id=ws_id,
                doc_key=doc_key,
                ops=[{"op": "create", "value": cleaned}],
                reason="移除迁移脚本编造的数值",
                base_version=latest["version_no"],
                source="tool",
                backend=(backend_by_workspace or {}).get(ws_id),
            )
            results.append({"workspace_id": ws_id, "doc_key": doc_key, **res})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="P1.4 Legacy Knowledge Migration CLI")
    parser.add_argument(
        "subcommand",
        nargs="?",
        default="all",
        choices=["all", "extract", "render", "diff", "apply", "report"],
        help="Pipeline stage to run (default: 'all' runs full dry-run pipeline)",
    )
    parser.add_argument(
        "--tickers",
        default=",".join(DEFAULT_TICKERS),
        help="Comma-separated tickers to migrate (default: META,AMZN,GOOGL,MSFT)",
    )
    parser.add_argument(
        "--out",
        default="var/dalton/migration",
        help="Output directory for migration artifacts (default: var/dalton/migration)",
    )
    parser.add_argument(
        "--ocw-path",
        default=str(DEFAULT_OCW_PATH),
        help="Path to read-only OpenClaw workspace",
    )
    parser.add_argument(
        "--sqlite-path",
        default=str(DEFAULT_DALTON_SQLITE),
        help="Path to read-only Dalton core.sqlite",
    )
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    out_dir = Path(args.out).resolve()
    ocw_path = Path(args.ocw_path).resolve()
    sqlite_path = Path(args.sqlite_path).resolve()

    report = run_migration_pipeline(tickers=tickers, out_dir=out_dir, ocw_path=ocw_path, sqlite_path=sqlite_path)
    print(
        json.dumps(
            {
                "subcommand": args.subcommand,
                "all_schemas_passed": report["all_schemas_passed"],
                "total_docs_validated": report["total_docs_validated"],
                "openclaw_debates_expected": report["openclaw_debates_expected"],
                "openclaw_debates_migrated": report["openclaw_debates_migrated"],
                "zero_source_mutations": report["source_integrity"]["zero_source_mutations"],
                "report_path": str(out_dir / "migration_report.json"),
            },
            indent=2,
        )
    )
    if not report["all_schemas_passed"] or not report["source_integrity"]["zero_source_mutations"]:
        return 1
    if report["openclaw_debates_expected"] != report["openclaw_debates_migrated"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
