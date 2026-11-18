#!/usr/bin/env python3
"""Goldman Sachs Prime Services US L/S leverage & Mag7 exposure store, parser, and dashboard renderer."""
from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from openclaw_workspace import workspace_root

try:
    from dashboard_copy import UI_COPY, format_stale_notice
except ImportError:  # pragma: no cover - standalone fallback
    UI_COPY = {
        "gs_prime": {
            "group_title": "高盛 Prime 账本对冲基金仓位监控",
            "missing_title": "高盛 Prime 仓位监控",
            "missing_msg": "高盛 Prime 历史数据本次不可用，未展示旧值。",
            "card_net_title": "美股多空（L/S）净杠杆（GS Prime）",
            "card_gross_title": "美股多空（L/S）总杠杆（GS Prime）",
            "card_mag7_net_title": "美股七大科技龙头（Mag7）净敞口占比",
            "card_mag7_gross_title": "美股七大科技龙头（Mag7）总敞口占比",
            "source_label": "高盛 Prime 周报",
            "rule_tpl": (
                "左图提取自高盛 Prime 周报美股多空基金净/总杠杆（1年或5年分位 ≤{cold_pct}% 为蓝区极度出清、≥{hot_pct}% 为橙区极度拥挤）；"
                "右图为2021年10月以来七大科技龙头（Mag7）占美股总敞口比例（≥{mag7_hot:.0f}% 拥挤、≤{mag7_cold:.0f}% 出清）。"
            ),
        }
    }

    def format_stale_notice(source: str, as_of: str | None) -> str:
        return f"⚠️ {source}数据未更新（上次成功：{as_of or '未知'}），当前展示历史缓存，请注意时效。"


WORKSPACE = workspace_root(__file__)
STATE_REL = "state/sentiment-dashboard/gs_prime_positioning.json"
VECTORS_DB_REL = "skills/market-digest/vectors.db"
SEED_PKL_REL = "temp/mag7_compare_20261026/gs_exposure.pkl"
GS_WEEKLY_STALE_DAYS = 11

WORD_TO_PCTILE: dict[str, tuple[int, str]] = {
    "record high": (100, "5y"),
    "five-year high": (100, "5y"),
    "five-year highs": (100, "5y"),
    "5-year high": (100, "5y"),
    "5-year highs": (100, "5y"),
    "three-year high": (100, "3y"),
    "three-year highs": (100, "3y"),
    "3-year high": (100, "3y"),
    "3-year highs": (100, "3y"),
    "one-year high": (100, "1y"),
    "one-year highs": (100, "1y"),
    "1-year high": (100, "1y"),
    "1-year highs": (100, "1y"),
    "five-year low": (0, "5y"),
    "five-year lows": (0, "5y"),
    "5-year low": (0, "5y"),
    "5-year lows": (0, "5y"),
    "three-year low": (0, "3y"),
    "three-year lows": (0, "3y"),
    "3-year low": (0, "3y"),
    "3-year lows": (0, "3y"),
    "one-year low": (0, "1y"),
    "one-year lows": (0, "1y"),
    "1-year low": (0, "1y"),
    "1-year lows": (0, "1y"),
}

LEVERAGE_PATTERN = re.compile(
    r"(US\s+(?:Fundamental\s+)?L/S(?:\s+HF)?|U\.S\.\s+fundamental\s+L/S(?:\s+HF)?)\s*,?\s*Gross\s+[Ll]everage"
    r".{0,160}?(?:to|at|of|\()\s*(\d{3}(?:\.\d+)?)%\s*(?:\(([^)]+)\))?"
    r".{0,120}?(?:Net\s+[Ll]everage)"
    r".{0,200}?(?:to|at|of|\()\s*(\d{2}(?:\.\d+)?)%\s*(?:\(([^)]+)\))?",
    re.I,
)

MAG7_TEXT_PATTERN = re.compile(
    r"Mag\s*7\s+net\s+exposure[^.;]{0,120}?(?:at|to|of)\s+(\d{1,2}\.\d+)%"
    r"[^.;]{0,120}?gross\s+exposure[^.;]{0,120}?(?:at|to|of)\s+(\d{1,2}\.\d+)%",
    re.I,
)


def parse_pctile(text: str) -> dict[str, int]:
    """Parse 1Y / 3Y / 5Y percentile integers from GS Prime commentary."""
    out: dict[str, int] = {}
    t = (text or "").lower()
    for m in re.finditer(
        r"(\d+)\s*(?:st|nd|rd|th)\s*(?:percentile|%ile)"
        r"(?:\s*(?:vs\.?\s*(?:the\s*)?past|on\s*a|on)?\s*"
        r"(one-year|1-year|1-yr|1y|1\s*yr|three-year|3-year|3-yr|3y|3\s*yr|five-year|5-year|5-yr|5y|5\s*yr))?",
        t,
    ):
        val = int(m.group(1))
        hz_raw = m.group(2) or ""
        if not hz_raw:
            pre = t[max(0, m.start() - 45) : m.start()]
            if any(k in pre for k in ("five-year", "5-year", "5 yr", "5yr")):
                hz_raw = "5y"
            elif any(k in pre for k in ("three-year", "3-year", "3 yr", "3yr")):
                hz_raw = "3y"
        if any(k in hz_raw for k in ("5", "five")):
            out.setdefault("pctile_5y", val)
        elif any(k in hz_raw for k in ("3", "three")):
            out.setdefault("pctile_3y", val)
        else:
            out.setdefault("pctile_1y", val)
    for phrase, (val, hz) in WORD_TO_PCTILE.items():
        if phrase in t:
            out.setdefault(f"pctile_{hz}", val)
    return out


def to_friday(date_str: str, is_weekend_recap: bool) -> str:
    """Snap Saturday/Sunday (or Monday 'last week' recap) dates to the corresponding Friday close."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").date()
    if dt.weekday() == 5:
        return (dt - timedelta(days=1)).isoformat()
    if dt.weekday() == 6:
        return (dt - timedelta(days=2)).isoformat()
    if is_weekend_recap and dt.weekday() == 0:
        return (dt - timedelta(days=3)).isoformat()
    return date_str


def extract_leverage_from_text(clean_text: str, raw_date: str, doc_id: str, subj: str) -> list[dict[str, Any]]:
    """Extract US Fundamental L/S Gross and Net Leverage readings from a single email body."""
    results: list[dict[str, Any]] = []
    for m in LEVERAGE_PATTERN.finditer(clean_text):
        gross = round(float(m.group(2)), 1)
        gross_p = parse_pctile(m.group(3) or "")
        net = round(float(m.group(4)), 1)
        same_sentence_tail = clean_text[m.end() : m.end() + 140].split(".")[0]
        tail_context = (m.group(5) or "") + " " + same_sentence_tail
        net_p = parse_pctile(tail_context)
        pre_context = clean_text[max(0, m.start() - 60) : m.start()].lower()
        is_weekend = "last week" in pre_context or "weekend" in subj.lower() or "on the week" in subj.lower()
        as_of = to_friday(raw_date, is_weekend)
        results.append(
            {
                "date": as_of,
                "published_date": raw_date,
                "doc_id": doc_id,
                "subject": subj.strip(),
                "gross_leverage": gross,
                "net_leverage": net,
                "gross_pctile_1y": gross_p.get("pctile_1y"),
                "gross_pctile_3y": gross_p.get("pctile_3y"),
                "gross_pctile_5y": gross_p.get("pctile_5y"),
                "net_pctile_1y": net_p.get("pctile_1y"),
                "net_pctile_3y": net_p.get("pctile_3y"),
                "net_pctile_5y": net_p.get("pctile_5y"),
            }
        )
    return results


def merge_leverage_rows(existing_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge and deduplicate weekly US L/S leverage rows by Friday date and adjacent identical values."""
    by_date: dict[str, dict[str, Any]] = {r["date"]: dict(r) for r in existing_rows if r.get("date")}
    pct_keys = ("gross_pctile_1y", "gross_pctile_3y", "gross_pctile_5y", "net_pctile_1y", "net_pctile_3y", "net_pctile_5y")
    for rec in new_rows:
        d = rec["date"]
        if d not in by_date:
            by_date[d] = dict(rec)
            continue
        cur = by_date[d]
        for k in pct_keys:
            if cur.get(k) is None and rec.get(k) is not None:
                cur[k] = rec[k]
        if "ON THE WEEK" in rec.get("subject", "").upper() and "ON THE WEEK" not in cur.get("subject", "").upper():
            cur["doc_id"] = rec["doc_id"]
            cur["subject"] = rec["subject"]
            cur["gross_leverage"] = rec["gross_leverage"]
            cur["net_leverage"] = rec["net_leverage"]

    deduped: list[dict[str, Any]] = []
    for d in sorted(by_date.keys()):
        rec = by_date[d]
        if deduped:
            prev = deduped[-1]
            dt_curr = datetime.strptime(d, "%Y-%m-%d").date()
            dt_prev = datetime.strptime(prev["date"], "%Y-%m-%d").date()
            if (
                (dt_curr - dt_prev).days <= 5
                and prev.get("gross_leverage") == rec.get("gross_leverage")
                and prev.get("net_leverage") == rec.get("net_leverage")
            ):
                for k in pct_keys:
                    if prev.get(k) is None and rec.get(k) is not None:
                        prev[k] = rec[k]
                continue
        deduped.append(rec)
    return deduped


def seed_mag7_from_pickle(pkl_path: Path) -> list[dict[str, Any]]:
    """Load the digitized 2021-10 to 2026-10-09 GS Prime Mag7 Net & Gross Exposure series."""
    if not pkl_path.exists():
        return []
    df = pd.read_pickle(pkl_path)
    if df.empty or "Net_Exp" not in df.columns or "Gross_Exp" not in df.columns:
        return []
    rows: list[dict[str, Any]] = []
    dates = list(df.index)
    for i, ts in enumerate(dates):
        d_str = pd.Timestamp(ts).strftime("%Y-%m-%d")
        if i == len(dates) - 1 and d_str == "2026-10-10":
            d_str = "2026-10-09"
        rows.append(
            {
                "date": d_str,
                "net_exp": round(float(df["Net_Exp"].iloc[i]), 2),
                "gross_exp": round(float(df["Gross_Exp"].iloc[i]), 2),
            }
        )
    return rows


def update_gs_prime_store(workspace: Path = WORKSPACE, *, force_full_scan: bool = False) -> dict[str, Any]:
    """Load and incrementally update state/sentiment-dashboard/gs_prime_positioning.json from vectors.db."""
    store_path = workspace / STATE_REL
    db_path = workspace / VECTORS_DB_REL
    seed_pkl = workspace / SEED_PKL_REL

    store: dict[str, Any] = {}
    if store_path.exists() and not force_full_scan:
        try:
            store = json.loads(store_path.read_text(encoding="utf-8"))
        except Exception:
            store = {}

    leverage_rows: list[dict[str, Any]] = list(store.get("leverage_history") or [])
    mag7_rows: list[dict[str, Any]] = list(store.get("mag7_history") or [])
    last_chunk_id = int(store.get("last_scanned_chunk_id") or 0) if not force_full_scan else 0

    if not mag7_rows:
        mag7_rows = seed_mag7_from_pickle(seed_pkl)

    dirty = not store_path.exists() or force_full_scan
    if db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute("SELECT MAX(id) FROM email_chunks")
            max_id_row = cur.fetchone()
            max_id = int(max_id_row[0] or 0) if max_id_row else 0
            if max_id > last_chunk_id or not leverage_rows:
                scan_from = 0 if not leverage_rows else last_chunk_id
                cur.execute(
                    """
                    SELECT DISTINCT email_id
                    FROM email_chunks
                    WHERE id > ?
                      AND (content LIKE '%Gross leverage%' OR content LIKE '%Net leverage%'
                           OR content LIKE '%Gross Leverage%' OR content LIKE '%Net Leverage%'
                           OR content LIKE '%Mag 7%' OR content LIKE '%Mag7%')
                    """,
                    (scan_from,),
                )
                candidate_ids = [r[0] for r in cur.fetchall() if r[0]]
                new_lev: list[dict[str, Any]] = []
                mag7_by_date = {r["date"]: dict(r) for r in mag7_rows if r.get("date")}
                for email_id in candidate_ids:
                    if str(email_id).startswith("manual:"):
                        continue
                    cur.execute(
                        "SELECT id, date, sender, subject, content FROM email_chunks WHERE email_id = ? ORDER BY chunk_index",
                        (email_id,),
                    )
                    chunks = cur.fetchall()
                    if not chunks:
                        continue
                    min_id, db_date, sender, subj = chunks[0][0], chunks[0][1] or "", chunks[0][2] or "", chunks[0][3] or ""
                    if any(b in sender.lower() for b in ("bofa", "jpmorgan", "jefferies")):
                        continue
                    text = "\n".join(c[4] or "" for c in chunks)
                    clean = re.sub(r"\s+", " ", text)
                    pub_m = re.search(r"Published\s+(\d{1,2}\s+[A-Za-z]+,?\s+202\d)", clean)
                    pub_date = None
                    if pub_m:
                        raw_pub = pub_m.group(1).replace(",", "")
                        for fmt_str in ("%d %b %Y", "%d %B %Y"):
                            try:
                                pub_date = datetime.strptime(raw_pub, fmt_str).strftime("%Y-%m-%d")
                                break
                            except ValueError:
                                pass
                    raw_date = pub_date or db_date
                    doc_id = f"mail:{min_id}"
                    new_lev.extend(extract_leverage_from_text(clean, raw_date, doc_id, subj))
                    for mm in MAG7_TEXT_PATTERN.finditer(clean):
                        m_date = to_friday(raw_date, True)
                        mag7_by_date[m_date] = {
                            "date": m_date,
                            "net_exp": round(float(mm.group(1)), 2),
                            "gross_exp": round(float(mm.group(2)), 2),
                            "doc_id": doc_id,
                        }
                if new_lev:
                    leverage_rows = merge_leverage_rows(leverage_rows, new_lev)
                mag7_rows = [mag7_by_date[d] for d in sorted(mag7_by_date.keys())]
                last_chunk_id = max_id
                dirty = True
            conn.close()
        except Exception:
            pass

    payload = {
        "ok": bool(leverage_rows and mag7_rows),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "last_scanned_chunk_id": last_chunk_id,
        "leverage_as_of": leverage_rows[-1]["date"] if leverage_rows else "",
        "mag7_as_of": mag7_rows[-1]["date"] if mag7_rows else "",
        "leverage_count": len(leverage_rows),
        "mag7_count": len(mag7_rows),
        "leverage_history": leverage_rows,
        "mag7_history": mag7_rows,
    }
    if dirty and payload["ok"]:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def _pctile_label(row: dict[str, Any], prefix: str, hot: int, cold: int) -> tuple[str, int | None, str]:
    """Return (display_label, trigger_pctile, direction) for net or gross leverage row."""
    p1 = row.get(f"{prefix}_pctile_1y")
    p5 = row.get(f"{prefix}_pctile_5y")
    p3 = row.get(f"{prefix}_pctile_3y")
    parts: list[str] = []
    if p1 is not None:
        parts.append(f"1年{int(p1)}分位")
    if p5 is not None:
        parts.append(f"5年{int(p5)}分位")
    if not parts and p3 is not None:
        parts.append(f"3年{int(p3)}分位")
    label = "/".join(parts) if parts else "分位未披露"

    candidates = [(p5, "5年"), (p1, "1年"), (p3, "3年")]
    for val, hz in candidates:
        if val is not None and int(val) <= cold:
            return f"{hz}{int(val)}分位", int(val), "cold"
    for val, hz in candidates:
        if val is not None and int(val) >= hot:
            return f"{hz}{int(val)}分位", int(val), "hot"
    return label, (int(p1) if p1 is not None else int(p5) if p5 is not None else int(p3) if p3 is not None else None), "neutral"


def evaluate_gs_prime_signals(store: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Evaluate GS Prime leverage and Mag7 exposure alerts for signals.json and Feishu digest."""
    gp_cfg = config.get("gs_prime") or {}
    hot_pctile = int(gp_cfg.get("leverage_hot_pctile", 90))
    cold_pctile = int(gp_cfg.get("leverage_cold_pctile", 10))
    mag7_hot = float(gp_cfg.get("mag7_net_hot_pct", 20.0))
    mag7_cold = float(gp_cfg.get("mag7_net_cold_pct", 15.0))
    mag7_fresh_days = int(gp_cfg.get("mag7_fresh_days", 7))

    lev_rows = store.get("leverage_history") or []
    mag7_rows = store.get("mag7_history") or []
    if not lev_rows and not mag7_rows:
        return {
            "ok": False,
            "hits": [],
            "chips": [],
            "reading": "高盛 Prime 仓位数据暂不可用",
            "mag7_quiet": "",
        }

    last_lev = lev_rows[-1] if lev_rows else {}
    last_mag7 = mag7_rows[-1] if mag7_rows else {}
    lev_as_of = str(last_lev.get("date") or "")
    mag7_as_of = str(last_mag7.get("date") or "")
    net_lev = last_lev.get("net_leverage")
    gross_lev = last_lev.get("gross_leverage")
    mag7_net = last_mag7.get("net_exp")
    mag7_gross = last_mag7.get("gross_exp")

    net_label, _net_p, net_dir = _pctile_label(last_lev, "net", hot_pctile, cold_pctile)
    gross_label, _gross_p, gross_dir = _pctile_label(last_lev, "gross", hot_pctile, cold_pctile)

    hits: list[str] = []
    chips: list[dict[str, str]] = []

    if net_lev is not None and net_dir in ("cold", "hot"):
        tag = "极度出清" if net_dir == "cold" else "极度拥挤"
        gross_ctx = f"；总杠杆 {gross_lev:.1f}%/{gross_label}" if gross_lev is not None else ""
        hits.append(f"美股 L/S 净杠杆 {net_lev:.1f}%（{net_label}，{tag}{gross_ctx}，截至 {lev_as_of}）")
        chips.append({"text": f"GS Prime 净杠杆 {net_lev:.1f}%（{net_label}，{tag}）", "tone": net_dir})

    if gross_lev is not None and gross_dir in ("cold", "hot"):
        tag = "极度出清" if gross_dir == "cold" else "极度拥挤"
        hits.append(f"美股 L/S 总杠杆 {gross_lev:.1f}%（{gross_label}，{tag}，截至 {lev_as_of}）")
        chips.append({"text": f"GS Prime 总杠杆 {gross_lev:.1f}%（{gross_label}，{tag}）", "tone": gross_dir})

    mag7_fresh = True
    if lev_as_of and mag7_as_of:
        try:
            d_lev = datetime.strptime(lev_as_of, "%Y-%m-%d").date()
            d_mag = datetime.strptime(mag7_as_of, "%Y-%m-%d").date()
            mag7_fresh = (d_lev - d_mag).days <= mag7_fresh_days
        except ValueError:
            mag7_fresh = False

    mag7_in_hits = False
    if mag7_net is not None and mag7_fresh:
        if float(mag7_net) >= mag7_hot:
            mag7_in_hits = True
            hits.append(f"Mag7 净敞口 {mag7_net:.1f}%（≥{mag7_hot:.0f}%，极度拥挤；总敞口 {mag7_gross:.1f}%，截至 {mag7_as_of}）")
            chips.append({"text": f"GS Prime Mag7 净敞口 {mag7_net:.1f}%（极度拥挤）", "tone": "hot"})
        elif float(mag7_net) <= mag7_cold:
            mag7_in_hits = True
            hits.append(f"Mag7 净敞口 {mag7_net:.1f}%（≤{mag7_cold:.0f}%，极度出清；总敞口 {mag7_gross:.1f}%，截至 {mag7_as_of}）")
            chips.append({"text": f"GS Prime Mag7 净敞口 {mag7_net:.1f}%（极度出清）", "tone": "cold"})

    reading_parts: list[str] = []
    if net_lev is not None and gross_lev is not None:
        reading_parts.append(f"美股 L/S 净杠杆 {net_lev:.1f}%（{net_label}）、总杠杆 {gross_lev:.1f}%（{gross_label}），截至 {lev_as_of}")
    if mag7_net is not None and mag7_gross is not None:
        reading_parts.append(f"Mag7 净敞口 {mag7_net:.1f}%、总敞口 {mag7_gross:.1f}%，截至 {mag7_as_of}")
    reading = "；".join(reading_parts) or "读数暂缺"

    mag7_quiet = (
        f"GS Prime Mag7：净敞口 {mag7_net:.1f}%、总敞口 {mag7_gross:.1f}%（图表截至 {mag7_as_of}）"
        if hits and not mag7_in_hits and mag7_net is not None and mag7_gross is not None
        else ""
    )

    return {
        "ok": True,
        "hits": hits,
        "chips": chips,
        "reading": reading,
        "mag7_quiet": mag7_quiet,
        "leverage": {
            "as_of": lev_as_of,
            "doc_id": last_lev.get("doc_id", ""),
            "net_leverage": net_lev,
            "gross_leverage": gross_lev,
            "net_pctile_1y": last_lev.get("net_pctile_1y"),
            "net_pctile_3y": last_lev.get("net_pctile_3y"),
            "net_pctile_5y": last_lev.get("net_pctile_5y"),
            "gross_pctile_1y": last_lev.get("gross_pctile_1y"),
            "gross_pctile_3y": last_lev.get("gross_pctile_3y"),
            "gross_pctile_5y": last_lev.get("gross_pctile_5y"),
            "net_label": net_label,
            "gross_label": gross_label,
            "net_direction": net_dir,
            "gross_direction": gross_dir,
        },
        "mag7": {
            "as_of": mag7_as_of,
            "net_exp": mag7_net,
            "gross_exp": mag7_gross,
            "fresh": mag7_fresh,
        },
        "lines": {
            "leverage_hot_pctile": hot_pctile,
            "leverage_cold_pctile": cold_pctile,
            "mag7_net_hot_pct": mag7_hot,
            "mag7_net_cold_pct": mag7_cold,
        },
    }


def _weekly_stale(as_of: str, threshold_days: int = GS_WEEKLY_STALE_DAYS) -> bool:
    if not as_of or as_of == "—":
        return False
    try:
        delta = (datetime.now().date() - datetime.strptime(as_of, "%Y-%m-%d").date()).days
        return delta > threshold_days
    except ValueError:
        return False


def _format_weekly_meta(
    *,
    as_of: str,
    change_pt: float | None,
    source_label: str,
    extra: str = "",
) -> str:
    chg_text = f"（较上期 {change_pt:+.1f} 个百分点）" if change_pt is not None else "（较上期变化暂缺）"
    as_of_label = f"{as_of}（美东时间）" if as_of and as_of != "—" else (as_of or "未知")
    parts = [f"周频 · 截至 {as_of_label}{chg_text}", f"来源：{source_label}"]
    if extra:
        parts.append(extra)
    if _weekly_stale(as_of):
        parts.append(f"数据未更新（上次成功：{as_of_label}）")
    return " · ".join(parts)


def render_gs_prime_section(
    *,
    store: dict[str, Any],
    signals_block: dict[str, Any],
    qqq_price: pd.Series,
    chart: Any,
    series_payload: Any,
    hline: Any,
    metric_card: Any,
    fmt: Any,
) -> str:
    """Render the dual-chart GS Prime Hedge Fund Positioning block."""
    copy = UI_COPY["gs_prime"]
    lev_rows = store.get("leverage_history") or []
    mag7_rows = store.get("mag7_history") or []
    if not lev_rows and not mag7_rows:
        return (
            f'<h3 class="monitor-group-title">{html.escape(copy["group_title"])}</h3>'
            f'<article class="panel"><h3>{html.escape(copy["missing_title"])}</h3>'
            f'<p class="empty">{html.escape(copy["missing_msg"])}</p></article>'
        )

    lev_info = signals_block.get("leverage") or {}
    mag7_info = signals_block.get("mag7") or {}
    lines_cfg = signals_block.get("lines") or {}
    hot_pct = int(lines_cfg.get("leverage_hot_pctile", 90))
    cold_pct = int(lines_cfg.get("leverage_cold_pctile", 10))
    mag7_hot = float(lines_cfg.get("mag7_net_hot_pct", 20.0))
    mag7_cold = float(lines_cfg.get("mag7_net_cold_pct", 15.0))

    lev_as_of = str(lev_info.get("as_of") or "—")
    mag7_as_of = str(mag7_info.get("as_of") or "—")
    net_now = lev_info.get("net_leverage")
    gross_now = lev_info.get("gross_leverage")
    net_label = lev_info.get("net_label") or "—"
    gross_label = lev_info.get("gross_label") or "—"
    net_dir = lev_info.get("net_direction") or "neutral"
    gross_dir = lev_info.get("gross_direction") or "neutral"
    mag7_net_now = mag7_info.get("net_exp")
    mag7_gross_now = mag7_info.get("gross_exp")

    prev_lev = lev_rows[-2] if len(lev_rows) >= 2 else {}
    net_chg = (
        float(net_now) - float(prev_lev["net_leverage"])
        if net_now is not None and prev_lev.get("net_leverage") is not None
        else None
    )
    gross_chg = (
        float(gross_now) - float(prev_lev["gross_leverage"])
        if gross_now is not None and prev_lev.get("gross_leverage") is not None
        else None
    )

    prev_mag7 = mag7_rows[-2] if len(mag7_rows) >= 2 else {}
    mag7_net_chg = (
        float(mag7_net_now) - float(prev_mag7["net_exp"])
        if mag7_net_now is not None and prev_mag7.get("net_exp") is not None
        else None
    )
    mag7_gross_chg = (
        float(mag7_gross_now) - float(prev_mag7["gross_exp"])
        if mag7_gross_now is not None and prev_mag7.get("gross_exp") is not None
        else None
    )

    net_state_str = "极度出清" if net_dir == "cold" else "极度拥挤" if net_dir == "hot" else "中性区间"
    gross_state_str = "极度出清" if gross_dir == "cold" else "极度拥挤" if gross_dir == "hot" else "中性区间"
    mag7_tone = (
        "hot"
        if mag7_net_now is not None and float(mag7_net_now) >= mag7_hot
        else "cold"
        if mag7_net_now is not None and float(mag7_net_now) <= mag7_cold
        else "neutral"
    )
    mag7_state_str = (
        f"高位拥挤(≥{mag7_hot:.0f}%)"
        if mag7_tone == "hot"
        else f"低位出清(≤{mag7_cold:.0f}%)"
        if mag7_tone == "cold"
        else "中间区间"
    )
    source_label = copy["source_label"]

    cards_html = '<section class="cards">' + "".join(
        [
            metric_card(
                title=copy["card_net_title"],
                value=fmt(net_now, 1, "%"),
                subtitle=f"{net_label} · {net_state_str}",
                tone=net_dir,
                meta=_format_weekly_meta(
                    as_of=lev_as_of,
                    change_pt=net_chg,
                    source_label=source_label,
                    extra=f"预警分位 ≤{cold_pct}% / ≥{hot_pct}%",
                ),
            ),
            metric_card(
                title=copy["card_gross_title"],
                value=fmt(gross_now, 1, "%"),
                subtitle=f"{gross_label} · {gross_state_str}",
                tone=gross_dir,
                meta=_format_weekly_meta(
                    as_of=lev_as_of,
                    change_pt=gross_chg,
                    source_label=source_label,
                    extra=f"预警分位 ≤{cold_pct}% / ≥{hot_pct}%",
                ),
            ),
            metric_card(
                title=copy["card_mag7_net_title"],
                value=fmt(mag7_net_now, 1, "%"),
                subtitle=f"占美股总净敞口 · {mag7_state_str}",
                tone=mag7_tone,
                meta=_format_weekly_meta(
                    as_of=mag7_as_of,
                    change_pt=mag7_net_chg,
                    source_label=source_label,
                    extra=f"拥挤线 {mag7_hot:.0f}% / 出清线 {mag7_cold:.0f}%",
                ),
            ),
            metric_card(
                title=copy["card_mag7_gross_title"],
                value=fmt(mag7_gross_now, 1, "%"),
                subtitle="占美股总敞口 · 2021年10月以来序列",
                tone="neutral",
                meta=_format_weekly_meta(
                    as_of=mag7_as_of,
                    change_pt=mag7_gross_chg,
                    source_label=source_label,
                    extra="历史区间 6.3%–13.7%",
                ),
            ),
        ]
    ) + "</section>"

    lev_idx = pd.to_datetime([r["date"] for r in lev_rows])
    net_series = pd.Series([float(r["net_leverage"]) for r in lev_rows], index=lev_idx, dtype=float)
    gross_series = pd.Series([float(r["gross_leverage"]) for r in lev_rows], index=lev_idx, dtype=float)

    left_note = (
        f"左轴净杠杆(%)、右轴总杠杆(%)；截至 {lev_as_of} 净杠杆 {fmt(net_now,1,'%')}（{net_label}）、"
        f"总杠杆 {fmt(gross_now,1,'%')}（{gross_label}）。"
    )
    left_chart = chart(
        "高盛 Prime：美股多空（L/S）净杠杆与总杠杆（周频）",
        [
            series_payload(net_series, "美股 L/S 净杠杆(%)", "#4da3ff", "left", True, 500, 45),
            series_payload(gross_series, "美股 L/S 总杠杆(%，右轴)", "#ffbf3c", "right", False, 500, 45),
        ],
        left_note,
        None,
        360,
        None,
        False,
        [
            hline(56.0, "left", "净杠杆高位56%", "hot"),
            hline(45.0, "left", "5年低位区45%", "cold"),
        ],
    )

    mag7_idx = pd.to_datetime([r["date"] for r in mag7_rows])
    mag7_net_s = pd.Series([float(r["net_exp"]) for r in mag7_rows], index=mag7_idx, dtype=float)
    mag7_gross_s = pd.Series([float(r["gross_exp"]) for r in mag7_rows], index=mag7_idx, dtype=float)
    qqq_plot = qqq_price.loc[qqq_price.index >= "2021-10-05"] if not qqq_price.empty else pd.Series(dtype=float)

    right_note = (
        f"左轴七大科技（Mag7）占美股总净/总敞口(%)，右轴纳指100 ETF（QQQ）；截至 {mag7_as_of} 净敞口 {fmt(mag7_net_now,1,'%')}、"
        f"总敞口 {fmt(mag7_gross_now,1,'%')}。"
    )
    right_chart = chart(
        "高盛 Prime：七大科技龙头（Mag7）净敞口与总敞口占比（叠加QQQ）",
        [
            series_payload(mag7_net_s, "Mag7 净敞口(%)", "#4da3ff", "left", True, 1000, 30),
            series_payload(mag7_gross_s, "Mag7 总敞口(%)", "#a377ff", "left", False, 1000, 30),
            series_payload(qqq_plot, "QQQ 收盘价（右轴）", "#ffbf3c", "right", False, 1000, 14),
        ],
        right_note,
        None,
        360,
        None,
        False,
        [
            hline(mag7_hot, "left", f"拥挤{mag7_hot:.0f}%", "hot"),
            hline(mag7_cold, "left", f"出清{mag7_cold:.0f}%", "cold"),
        ],
    )

    rule_text = copy["rule_tpl"].format(
        cold_pct=cold_pct,
        hot_pct=hot_pct,
        mag7_hot=mag7_hot,
        mag7_cold=mag7_cold,
    )
    stale_banner = ""
    if _weekly_stale(lev_as_of):
        stale_banner += f'<p class="stale-banner">{html.escape(format_stale_notice("高盛 Prime 杠杆周报", lev_as_of))}</p>'
    return (
        f'<h3 class="monitor-group-title">{html.escape(copy["group_title"])}</h3>'
        + stale_banner
        + f'<p class="note rule">{html.escape(rule_text)}</p>'
        + cards_html
        + '<div class="grid two">'
        + left_chart
        + right_chart
        + "</div>"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Update and inspect GS Prime positioning store.")
    parser.add_argument("--force", action="store_true", help="Rescan all email chunks in vectors.db")
    parser.add_argument("--add-mag7", nargs=3, metavar=("DATE", "NET_EXP", "GROSS_EXP"), help="Append/update a Mag7 exposure point")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args()

    store = update_gs_prime_store(WORKSPACE, force_full_scan=args.force)
    if args.add_mag7:
        dt_str, net_v, gross_v = args.add_mag7[0], round(float(args.add_mag7[1]), 2), round(float(args.add_mag7[2]), 2)
        by_date = {r["date"]: dict(r) for r in (store.get("mag7_history") or [])}
        by_date[dt_str] = {"date": dt_str, "net_exp": net_v, "gross_exp": gross_v, "doc_id": "manual"}
        store["mag7_history"] = [by_date[d] for d in sorted(by_date.keys())]
        store["mag7_as_of"] = store["mag7_history"][-1]["date"]
        store["mag7_count"] = len(store["mag7_history"])
        (WORKSPACE / STATE_REL).write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary = {
        "ok": store.get("ok", False),
        "state_path": str(WORKSPACE / STATE_REL),
        "leverage_as_of": store.get("leverage_as_of"),
        "leverage_count": store.get("leverage_count"),
        "latest_leverage": (store.get("leverage_history") or [None])[-1],
        "mag7_as_of": store.get("mag7_as_of"),
        "mag7_count": store.get("mag7_count"),
        "latest_mag7": (store.get("mag7_history") or [None])[-1],
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"GS Prime store updated: {summary['state_path']}")
        print(f"  Leverage ({summary['leverage_count']} rows, as of {summary['leverage_as_of']}): {summary['latest_leverage']}")
        print(f"  Mag7     ({summary['mag7_count']} rows, as of {summary['mag7_as_of']}): {summary['latest_mag7']}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
