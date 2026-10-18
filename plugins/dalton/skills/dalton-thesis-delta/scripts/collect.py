#!/usr/bin/env python3
"""
collect.py — 每晚 thesis 增量包采集与状态提交脚本（PS.11）

纯确定性代码，不调用大语言模型。
子命令：
  1. collect --ticker <TICKER> --workspace-dir <DIR> [--since YYYY-MM-DD] [--no-ae] [--no-web]
     - 读取覆盖阈值（优先 <workspace_dir>/research/tracker/thresholds.json，回退 config/default-thresholds.json）
     - 拉取一致预期快照并写入 <workspace_dir>/data/consensus/<YYYY-MM-DD_HHMM>.json
     - 与上一份一致预期快照比对（若 fetched_at 相同则跳过对比），生成 deltas 与 auto_flags
     - 检索未在 seen.json 中的内部库文档（sales-notes, tmtb, funda-ai, company-wiki）、AlphaEngine（前推3天）、SEC filing（前推2天）
     - 写入 <workspace_dir>/research/tracker/<run_id>/packet.json 与 packet.md
     - 注意：collect 阶段绝不修改 seen.json

  2. commit --workspace-dir <DIR> --run-id <RUN_ID> --material yes|no --summary "..."
     - 读取 <workspace_dir>/research/tracker/<run_id>/packet.json
     - 将本轮所有新文档/公告/帖子 ID 并入 <workspace_dir>/research/tracker/seen.json
     - 更新 <workspace_dir>/research/tracker/last_run.json
     - 追加一行记录到 <workspace_dir>/research/tracker/updates.log

  3. status --workspace-dir <DIR> [--ticker <TICKER>]
  4. self-test
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_THRESHOLDS_PATH = SKILL_DIR / "config" / "default-thresholds.json"

DEFAULT_SEC_FORMS = [
    "8-K",
    "10-Q",
    "10-K",
    "6-K",
    "20-F",
    "S-3ASR",
    "424B2",
    "424B5",
    "FWP",
    "Form 4",
    "SC 13D",
    "SC 13G",
]

INTERNAL_ROUTES = ["sales-notes", "tmtb", "funda-ai", "company-wiki"]


def now_et() -> datetime:
    return datetime.now(ET)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def dump_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def clean_val(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): clean_val(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean_val(v) for v in x]
    if hasattr(x, "item"):
        try:
            x = x.item()
        except Exception:
            pass
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    if isinstance(x, (datetime, date)):
        return x.isoformat()
    return x


def tracker_paths(workspace_dir: Path) -> dict[str, Path]:
    ws = workspace_dir.resolve()
    tr = ws / "research" / "tracker"
    return {
        "workspace": ws,
        "tracker": tr,
        "thresholds": tr / "thresholds.json",
        "seen": tr / "seen.json",
        "last_run": tr / "last_run.json",
        "log": tr / "updates.log",
        "consensus": ws / "data" / "consensus",
        "company_json": ws / "data" / "company.json",
    }


def load_thresholds(workspace_dir: Path) -> dict[str, Any]:
    base = load_json(DEFAULT_THRESHOLDS_PATH, {
        "eps_fy1_vs_last_pct": 1.0,
        "eps_fy1_vs_7d_pct": 2.0,
        "rev_fy1_or_q1_vs_last_pct": 0.7,
        "target_price_vs_last_pct": 2.0,
        "down_revisions_7d_min": 5,
        "price_1d_pct": 4.0,
        "price_5d_pct": 8.0,
        "any_new_sec_filing": True,
    })
    override_p = tracker_paths(workspace_dir)["thresholds"]
    override = load_json(override_p, {})
    if isinstance(override, dict):
        base.update(override)
    return base


def resolve_company_meta(ticker: str, workspace_dir: Path) -> dict[str, Any]:
    """从工作区 data/company.json 或 ../Home/dalton/coverage.yaml 获取别名与基准 ETF。"""
    t = ticker.upper().strip()
    meta: dict[str, Any] = {
        "ticker": t,
        "name": t,
        "aliases": [t],
        "benchmark": "QQQ",
        "sec_forms": list(DEFAULT_SEC_FORMS),
    }
    comp_json = load_json(tracker_paths(workspace_dir)["company_json"], {})
    if isinstance(comp_json, dict):
        if comp_json.get("name"):
            meta["name"] = str(comp_json["name"])
        if isinstance(comp_json.get("benchmarks"), list) and comp_json["benchmarks"]:
            meta["benchmark"] = str(comp_json["benchmarks"][0])
        if isinstance(comp_json.get("aliases"), list) and comp_json["aliases"]:
            meta["aliases"] = [str(a) for a in comp_json["aliases"]]

    # 尝试从 coverage.yaml 补充
    cov_candidates = [
        workspace_dir / "../Home/dalton/coverage.yaml",
        workspace_dir / "dalton/coverage.yaml",
        Path("config/dalton/coverage.yaml"),
    ]
    for cp in cov_candidates:
        if cp.exists():
            try:
                import yaml  # type: ignore
                cov = yaml.safe_load(cp.read_text(encoding="utf-8")) or {}
                for c in cov.get("companies") or []:
                    if str(c.get("ticker", "")).upper() == t:
                        meta["name"] = c.get("name") or meta["name"]
                        if isinstance(c.get("etfs"), list) and c["etfs"]:
                            meta["benchmark"] = str(c["etfs"][0])
                        als = (c.get("aliases") or {}).get("search_names") or []
                        if als:
                            meta["aliases"] = list(dict.fromkeys([t, meta["name"], *als]))
                        break
            except Exception:
                pass
            break

    if meta["name"] not in meta["aliases"]:
        meta["aliases"].append(meta["name"])
    return meta


def fetch_consensus_snapshot(ticker: str, benchmark: str = "QQQ") -> dict[str, Any]:
    """获取一致预期与价格位置快照（带 _meta 字段，符合 P0-D.6 规范）。"""
    fetched_at = now_et().isoformat()
    out: dict[str, Any] = {
        "_meta": {
            "route": "estimates",
            "fetched_at": fetched_at,
            "source": "Yahoo Finance via yfinance",
        },
        "ticker": ticker,
        "fetched_at": fetched_at,
        "source": "Yahoo Finance via yfinance",
        "earnings_estimate": {},
        "revenue_estimate": {},
        "eps_trend": {},
        "eps_revisions": {},
        "price_targets": {},
        "price": {},
    }
    try:
        import yfinance as yf  # type: ignore

        t = yf.Ticker(ticker)
        for field in ("earnings_estimate", "revenue_estimate", "eps_trend", "eps_revisions"):
            try:
                df = getattr(t, field, None)
                if df is not None and not getattr(df, "empty", True):
                    out[field] = clean_val(df.to_dict(orient="index"))
            except Exception as e:
                out[field] = {"error": str(e)[:200]}

        try:
            out["price_targets"] = clean_val(dict(t.analyst_price_targets or {}))
        except Exception as e:
            out["price_targets"] = {"error": str(e)[:200]}

        try:
            syms = list(dict.fromkeys([ticker, benchmark]))
            h = yf.download(syms, period="3mo", auto_adjust=False, progress=False)["Close"].dropna(how="all")
            if h is not None and len(h) >= 2:
                t_s = h[ticker].dropna() if hasattr(h, "columns") and ticker in h.columns else h.dropna()
                b_s = h[benchmark].dropna() if hasattr(h, "columns") and benchmark in h.columns else t_s
                last_p, prev_p = float(t_s.iloc[-1]), float(t_s.iloc[-2])
                five_p = float(t_s.iloc[-6]) if len(t_s) >= 6 else float(t_s.iloc[0])
                b_last, b_prev = float(b_s.iloc[-1]), float(b_s.iloc[-2])
                b_five = float(b_s.iloc[-6]) if len(b_s) >= 6 else float(b_s.iloc[0])
                high_3m = float(t_s.max())
                out["price"] = {
                    "as_of": t_s.index[-1].date().isoformat(),
                    "close": round(last_p, 2),
                    "chg_1d_pct": round((last_p / prev_p - 1.0) * 100.0, 2) if prev_p else None,
                    "chg_5d_pct": round((last_p / five_p - 1.0) * 100.0, 2) if five_p else None,
                    "benchmark": benchmark,
                    "bench_chg_1d_pct": round((b_last / b_prev - 1.0) * 100.0, 2) if b_prev else None,
                    "bench_chg_5d_pct": round((b_last / b_five - 1.0) * 100.0, 2) if b_five else None,
                    "high_3m": round(high_3m, 2),
                    "drawdown_from_3m_high_pct": round((last_p / high_3m - 1.0) * 100.0, 2) if high_3m else None,
                }
        except Exception as e:
            out["price"] = {"error": str(e)[:200]}
    except Exception as e:
        out["error"] = str(e)[:300]
    return out


def compute_consensus_deltas(
    cur: dict[str, Any],
    prev: dict[str, Any] | None,
    thr: dict[str, Any],
    sec_new_count: int = 0,
) -> tuple[dict[str, Any], list[str]]:
    """计算一致预期变化百分比与自动提示（auto_flags）。"""
    deltas: dict[str, Any] = {}
    flags: list[str] = []

    def pct_diff(a: Any, b: Any) -> float | None:
        try:
            fa, fb = float(a), float(b)
            if fb == 0 or math.isnan(fa) or math.isnan(fb):
                return None
            return round((fa / fb - 1.0) * 100.0, 2)
        except (TypeError, ValueError):
            return None

    trend = cur.get("eps_trend") if isinstance(cur.get("eps_trend"), dict) else {}
    for per in ("0q", "+1q", "0y", "+1y"):
        row = trend.get(per) if isinstance(trend.get(per), dict) else {}
        deltas[f"eps_{per}_vs_7d_pct"] = pct_diff(row.get("current"), row.get("7daysAgo"))
        deltas[f"eps_{per}_vs_30d_pct"] = pct_diff(row.get("current"), row.get("30daysAgo"))

    revs = cur.get("eps_revisions") if isinstance(cur.get("eps_revisions"), dict) else {}
    rev_1y = revs.get("+1y") if isinstance(revs.get("+1y"), dict) else {}
    deltas["fy1_revisions_7d"] = {
        "up": rev_1y.get("upLast7days"),
        "down": rev_1y.get("downLast7Days"),
    }

    # 若 prev 存在且 fetched_at 与 cur 不同（排除周末/节假日未更新快照），计算对上次运行的变化
    if prev and prev.get("fetched_at") != cur.get("fetched_at"):
        cur_ee = cur.get("earnings_estimate") if isinstance(cur.get("earnings_estimate"), dict) else {}
        prev_ee = prev.get("earnings_estimate") if isinstance(prev.get("earnings_estimate"), dict) else {}
        cur_re = cur.get("revenue_estimate") if isinstance(cur.get("revenue_estimate"), dict) else {}
        prev_re = prev.get("revenue_estimate") if isinstance(prev.get("revenue_estimate"), dict) else {}
        for per in ("0q", "+1q", "0y", "+1y"):
            c_e = (cur_ee.get(per) or {}).get("avg") if isinstance(cur_ee.get(per), dict) else None
            p_e = (prev_ee.get(per) or {}).get("avg") if isinstance(prev_ee.get(per), dict) else None
            deltas[f"eps_{per}_vs_last_pct"] = pct_diff(c_e, p_e)

            c_r = (cur_re.get(per) or {}).get("avg") if isinstance(cur_re.get(per), dict) else None
            p_r = (prev_re.get(per) or {}).get("avg") if isinstance(prev_re.get(per), dict) else None
            deltas[f"rev_{per}_vs_last_pct"] = pct_diff(c_r, p_r)

        cur_pt = (cur.get("price_targets") or {}).get("mean") if isinstance(cur.get("price_targets"), dict) else None
        prev_pt = (prev.get("price_targets") or {}).get("mean") if isinstance(prev.get("price_targets"), dict) else None
        deltas["target_price_vs_last_pct"] = pct_diff(cur_pt, prev_pt)
        deltas["last_run_snapshot_at"] = prev.get("fetched_at")
    elif prev and prev.get("fetched_at") == cur.get("fetched_at"):
        deltas["skipped_same_fetched_at"] = True

    def check_threshold(delta_key: str, thr_val: float, label: str) -> None:
        val = deltas.get(delta_key)
        if val is not None and abs(float(val)) >= float(thr_val):
            flags.append(f"{label} {float(val):+.2f}%（阈值 ±{thr_val}%）")

    eps_last_thr = float(thr.get("eps_fy1_vs_last_pct", 1.0))
    eps_7d_thr = float(thr.get("eps_fy1_vs_7d_pct", 2.0))
    rev_last_thr = float(thr.get("rev_fy1_or_q1_vs_last_pct", 0.7))
    pt_last_thr = float(thr.get("target_price_vs_last_pct", 2.0))
    down_min = int(thr.get("down_revisions_7d_min", 5))
    p1d_thr = float(thr.get("price_1d_pct", 4.0))
    p5d_thr = float(thr.get("price_5d_pct", 8.0))
    sec_flag_enabled = bool(thr.get("any_new_sec_filing", True))

    check_threshold("eps_+1y_vs_last_pct", eps_last_thr, "下一财年(+1y) EPS 较上次运行")
    check_threshold("eps_0y_vs_last_pct", eps_last_thr, "本财年(0y) EPS 较上次运行")
    check_threshold("eps_+1y_vs_7d_pct", eps_7d_thr, "下一财年(+1y) EPS 较 7 天前")
    check_threshold("rev_+1y_vs_last_pct", rev_last_thr, "下一财年(+1y) 收入较上次运行")
    check_threshold("rev_+1q_vs_last_pct", rev_last_thr, "下一季度(+1q) 收入较上次运行")
    check_threshold("target_price_vs_last_pct", pt_last_thr, "目标价均值较上次运行")

    down_cnt = (deltas.get("fy1_revisions_7d") or {}).get("down") or 0
    if isinstance(down_cnt, (int, float)) and down_cnt >= down_min:
        flags.append(f"下一财年 EPS 近 7 天下调 {int(down_cnt)} 家（阈值 >={down_min} 家）")

    p_info = cur.get("price") if isinstance(cur.get("price"), dict) else {}
    bench_sym = p_info.get("benchmark", "QQQ")
    c1d = p_info.get("chg_1d_pct")
    if c1d is not None and abs(float(c1d)) >= p1d_thr:
        b1d = p_info.get("bench_chg_1d_pct")
        b_str = f"（{bench_sym} {float(b1d):+.2f}%）" if b1d is not None else ""
        flags.append(f"股价 1 日涨跌 {float(c1d):+.2f}%{b_str}（阈值 ±{p1d_thr}%）")

    c5d = p_info.get("chg_5d_pct")
    if c5d is not None and abs(float(c5d)) >= p5d_thr:
        b5d = p_info.get("bench_chg_5d_pct")
        b_str = f"（{bench_sym} {float(b5d):+.2f}%）" if b5d is not None else ""
        flags.append(f"股价 5 日涨跌 {float(c5d):+.2f}%{b_str}（阈值 ±{p5d_thr}%）")

    if sec_flag_enabled and sec_new_count > 0:
        flags.append(f"发现 {sec_new_count} 份新 SEC filing")

    return deltas, flags


def call_bridge_jsonrpc(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
    """若环境配置了 DALTON_RESEARCHDATA_URL，直接经 HTTP 桥调用 researchdata 工具。"""
    bridge_url = os.environ.get("DALTON_RESEARCHDATA_URL", "").strip()
    if not bridge_url:
        return {"ok": False, "status": "bridge_url_not_configured"}

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }
    req = urllib.request.Request(
        bridge_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
        res = raw.get("result") or {}
        content = res.get("content") or []
        if content and isinstance(content[0], dict) and content[0].get("text"):
            data = json.loads(content[0]["text"])
            return {"ok": not res.get("isError", False), "data": data}
        return {"ok": False, "error": raw.get("error") or "empty_bridge_content"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def collect_internal_and_ae(
    meta: dict[str, Any],
    since: str,
    until: str,
    seen: set[str],
    skip_ae: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """采集内部库（sales-notes, tmtb, funda-ai, company-wiki）与 AlphaEngine 新增文档。"""
    internal_new: dict[str, Any] = {}
    internal_status: dict[str, Any] = {}
    ae_new: dict[str, Any] = {}
    ae_status: list[dict[str, Any]] = []

    query_str = " ".join(meta["aliases"])
    pat = re.compile("|".join(re.escape(a) for a in meta["aliases"] if a), re.IGNORECASE)

    for route in INTERNAL_ROUTES:
        if route == "company-wiki":
            res = call_bridge_jsonrpc(
                "research",
                {"route": "company-wiki", "operation": "list", "limit": 50, "params": {"indexed_since": since, "media": "text"}},
            )
            data = res.get("data") or {}
            items = data.get("results") or []
            internal_status[route] = {"ok": res.get("ok", False), "listed": len(items), "status": data.get("status")}
            for r in items:
                did = r.get("document_id")
                if not did or did in seen:
                    continue
                is_own = str(r.get("company") or "").upper() == meta["ticker"]
                snippet_text = f"{r.get('title') or ''} {r.get('snippet') or ''}"
                hits = len(pat.findall(snippet_text))
                if is_own or hits >= 2:
                    internal_new[did] = {
                        "route": "company-wiki",
                        "title": r.get("title"),
                        "date": r.get("date"),
                        "indexed_at": r.get("indexed_at"),
                        "from": r.get("original_source"),
                        "location": r.get("location"),
                    }
        else:
            res = call_bridge_jsonrpc(
                "research",
                {
                    "route": route,
                    "operation": "search",
                    "query": query_str,
                    "start": since,
                    "end": until,
                    "limit": 40,
                    "params": {"media": "text", "search_mode": "keyword"},
                },
            )
            data = res.get("data") or {}
            items = data.get("results") or []
            internal_status[route] = {"ok": res.get("ok", False), "returned": len(items), "status": data.get("status")}
            for r in items:
                did = r.get("document_id")
                if not did or str(did).startswith("image:") or did in seen:
                    continue
                internal_new[did] = {
                    "route": route,
                    "title": r.get("title"),
                    "date": r.get("date"),
                    "from": r.get("original_source"),
                    "snippet": (r.get("snippet") or "")[:600],
                }

    if skip_ae:
        ae_status.append({"skipped": "--no-ae"})
    else:
        ae_start = (date.fromisoformat(since) - timedelta(days=3)).isoformat()
        res = call_bridge_jsonrpc(
            "research",
            {
                "route": "alphaengine",
                "operation": "search_library",
                "query": meta["name"],
                "start": ae_start,
                "end": until,
                "limit": 20,
                "params": {"sort": "relevance", "document_categories": ["sell_side_report", "sell_side_comment"]},
            },
        )
        data = res.get("data") or {}
        items = data.get("results") or []
        ae_status.append({"query": meta["name"], "ok": res.get("ok", False), "returned": len(items), "error": res.get("error")})
        for r in items:
            doc_id = r.get("doc_id") or r.get("document_id")
            if not doc_id:
                continue
            did = f"alphaengine:{doc_id}" if not str(doc_id).startswith("alphaengine:") else str(doc_id)
            if did in seen:
                continue
            ae_new[did] = {
                "title": r.get("title"),
                "publish_time": r.get("publish_time") or r.get("date"),
                "sources": r.get("sources"),
                "document_code": r.get("document_code"),
                "snippet": (r.get("snippet") or "")[:800],
            }

    return {"status": internal_status, "new": internal_new}, {"status": ae_status, "new": ae_new}


def collect_sec_filings(meta: dict[str, Any], since: str, seen: set[str]) -> dict[str, Any]:
    """采集窗口起点前推 2 天以来的 SEC 新 filing。"""
    floor = (date.fromisoformat(since) - timedelta(days=2)).isoformat()
    found: dict[str, Any] = {}
    status: list[dict[str, Any]] = []
    for form in meta["sec_forms"]:
        res = call_bridge_jsonrpc(
            "finance",
            {"route": "sec", "operation": "list-filings", "ticker": meta["ticker"], "form": form, "limit": 5},
        )
        data = res.get("data") or {}
        rows = data.get("rows") or []
        status.append({"form": form, "ok": res.get("ok", False), "count": len(rows)})
        for r in rows:
            acc_raw = r.get("accession_no") or r.get("accession")
            if not acc_raw:
                continue
            acc = f"sec:{acc_raw}" if not str(acc_raw).startswith("sec:") else str(acc_raw)
            f_date = str(r.get("filing_date") or "")
            if acc not in seen and f_date >= floor:
                found[acc] = {
                    "form": r.get("form") or form,
                    "filing_date": f_date,
                    "report_date": r.get("report_date"),
                    "accession": acc_raw,
                }
    return {"status": status, "new": found}


def render_packet_md(pk: dict[str, Any]) -> str:
    """将 packet.json 渲染为结构化 packet.md 供分析师审阅。"""
    lines = [
        f"# {pk['ticker']} 每晚增量数据包（{pk['run_id']}）",
        "",
        f"- **窗口**：{pk['since']} 至 {pk['until']}（美东）",
        f"- **上次成功提交**：{pk.get('last_committed_at') or '首次运行'}",
        f"- **一致预期快照文件**：`{pk.get('snapshot_file')}`",
        "",
        "## 一、自动触发提示（`auto_flags`，是否重要需按 `materiality.md` 判断）",
    ]
    if pk.get("auto_flags"):
        for f in pk["auto_flags"]:
            lines.append(f"- {f}")
    else:
        lines.append("- 无")

    c = pk.get("consensus") or {}
    p = c.get("price") or {}
    lines.extend(["", "## 二、股价与一致预期快照"])
    if "close" in p:
        bench = p.get("benchmark", "QQQ")
        lines.append(
            f"- **收盘（{p.get('as_of')}）**：{p.get('close')} 美元｜"
            f"1 日 {p.get('chg_1d_pct')}%（{bench} {p.get('bench_chg_1d_pct')}%）｜"
            f"5 日 {p.get('chg_5d_pct')}%（{bench} {p.get('bench_chg_5d_pct')}%）｜"
            f"距 3 个月高点 {p.get('drawdown_from_3m_high_pct')}%"
        )

    ee = c.get("earnings_estimate") if isinstance(c.get("earnings_estimate"), dict) else {}
    re_ = c.get("revenue_estimate") if isinstance(c.get("revenue_estimate"), dict) else {}
    et = c.get("eps_trend") if isinstance(c.get("eps_trend"), dict) else {}
    er = c.get("eps_revisions") if isinstance(c.get("eps_revisions"), dict) else {}
    for per in ("0q", "+1q", "0y", "+1y"):
        e_row = ee.get(per) if isinstance(ee.get(per), dict) else {}
        r_row = re_.get(per) if isinstance(re_.get(per), dict) else {}
        t_row = et.get(per) if isinstance(et.get(per), dict) else {}
        v_row = er.get(per) if isinstance(er.get(per), dict) else {}
        if not e_row and not r_row:
            continue
        rev_bn = round(float(r_row["avg"]) / 1e9, 2) if r_row.get("avg") else None
        lines.append(
            f"- **{per}**：EPS 均值 {e_row.get('avg')}（区间 {e_row.get('low')} 至 {e_row.get('high')}，{e_row.get('numberOfAnalysts')} 家）｜"
            f"收入均值 {rev_bn} 十亿美元｜"
            f"EPS 7/30 天前：{t_row.get('7daysAgo')} / {t_row.get('30daysAgo')}｜"
            f"近 7 天上调 {v_row.get('upLast7days')} / 下调 {v_row.get('downLast7Days')}"
        )

    pt = c.get("price_targets") if isinstance(c.get("price_targets"), dict) else {}
    if pt:
        lines.append(f"- **目标价**：均值 {pt.get('mean')}｜中位数 {pt.get('median')}｜区间 {pt.get('low')} 至 {pt.get('high')}")
    lines.append(f"- **修正差值（deltas）**：`{json.dumps(pk.get('deltas') or {}, ensure_ascii=False)}`")

    lines.extend(["", "## 三、内部库新文档（sales-notes / tmtb / funda-ai / company-wiki）"])
    int_new = (pk.get("internal") or {}).get("new") or {}
    if int_new:
        for did, info in int_new.items():
            lines.append(f"- `{did}`｜{info.get('date')}｜{info.get('route')}｜{info.get('title')}｜{info.get('from') or ''}")
            if info.get("snippet"):
                lines.append(f"  - 摘要：{info['snippet']}")
    else:
        lines.append("- 无")

    lines.extend(["", "## 四、AlphaEngine 新研报与点评"])
    ae_new = (pk.get("alphaengine") or {}).get("new") or {}
    if ae_new:
        for did, info in ae_new.items():
            lines.append(f"- `{did}`｜{info.get('publish_time')}｜{info.get('title')}")
            if info.get("snippet"):
                lines.append(f"  - 摘要：{info['snippet']}")
    else:
        lines.append("- 无")

    lines.extend(["", "## 五、SEC 新公告"])
    sec_new = (pk.get("sec") or {}).get("new") or {}
    if sec_new:
        for acc, info in sec_new.items():
            lines.append(f"- `{acc}`｜{info.get('form')}｜提交日 {info.get('filing_date')}｜报告期 {info.get('report_date') or ''}")
    else:
        lines.append("- 无")

    return "\n".join(lines) + "\n"


def latest_previous_snapshot(consensus_dir: Path, current_file: Path, last_run_hint: str | None, ws_dir: Path) -> dict[str, Any] | None:
    if last_run_hint:
        hint_p = ws_dir / last_run_hint
        if hint_p.exists() and hint_p.resolve() != current_file.resolve():
            loaded = load_json(hint_p, None)
            if isinstance(loaded, dict):
                return loaded
    files = sorted(f for f in consensus_dir.glob("*.json") if f.resolve() != current_file.resolve())
    if not files:
        return None
    return load_json(files[-1], None)


def cmd_collect(args: argparse.Namespace) -> int:
    ws_dir = Path(args.workspace_dir).resolve()
    P = tracker_paths(ws_dir)
    ticker = args.ticker.upper().strip()
    thr = load_thresholds(ws_dir)
    meta = resolve_company_meta(ticker, ws_dir)

    last_run = load_json(P["last_run"], {})
    seen = set(load_json(P["seen"], []))

    now = now_et()
    until = now.date().isoformat()
    if args.since:
        since = args.since
    elif last_run.get("until"):
        since = (date.fromisoformat(str(last_run["until"])) - timedelta(days=1)).isoformat()
    else:
        since = (now.date() - timedelta(days=3)).isoformat()

    run_id = args.run_id or now.strftime("%Y%m%d-%H%M")
    run_dir = P["tracker"] / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. 拉取并保存一致预期快照到 data/consensus/<YYYY-MM-DD_HHMM>.json
    cons = fetch_consensus_snapshot(ticker, benchmark=meta["benchmark"])
    snap_name = f"{now.strftime('%Y-%m-%d_%H%M')}.json"
    snap_path = P["consensus"] / snap_name
    dump_json(snap_path, cons)

    prev_snap = latest_previous_snapshot(P["consensus"], snap_path, last_run.get("snapshot_file"), ws_dir)

    # 2. 检索内部库、AlphaEngine、SEC
    internal_data, ae_data = collect_internal_and_ae(meta, since, until, seen, skip_ae=args.no_ae)
    sec_data = collect_sec_filings(meta, since, seen)

    # 3. 计算一致预期变化与自动触发提示
    deltas, auto_flags = compute_consensus_deltas(
        cur=cons,
        prev=prev_snap,
        thr=thr,
        sec_new_count=len(sec_data["new"]),
    )

    rel_snap = str(snap_path.relative_to(ws_dir))
    packet = {
        "ticker": ticker,
        "run_id": run_id,
        "since": since,
        "until": until,
        "last_committed_at": last_run.get("committed_at"),
        "snapshot_file": rel_snap,
        "thresholds": thr,
        "consensus": cons,
        "deltas": deltas,
        "auto_flags": auto_flags,
        "internal": internal_data,
        "alphaengine": ae_data,
        "sec": sec_data,
        "x": {"status": [], "new": {}, "note": "X 由分析师在需要时用 web_search / x_api.search_posts 补充"},
    }

    pkt_json_path = run_dir / "packet.json"
    pkt_md_path = run_dir / "packet.md"
    dump_json(pkt_json_path, packet)
    pkt_md_path.write_text(render_packet_md(packet), encoding="utf-8")

    summary = {
        "ok": True,
        "ticker": ticker,
        "run_id": run_id,
        "since": since,
        "until": until,
        "packet_json": str(pkt_json_path.relative_to(ws_dir)),
        "packet_md": str(pkt_md_path.relative_to(ws_dir)),
        "snapshot_file": rel_snap,
        "auto_flags": auto_flags,
        "counts": {
            "internal": len(internal_data["new"]),
            "alphaengine": len(ae_data["new"]),
            "sec": len(sec_data["new"]),
            "x": 0,
        },
        "next": (
            f"python3 .agents/skills/dalton-thesis-delta/scripts/collect.py commit "
            f"--workspace-dir {ws_dir} --run-id {run_id} --material yes|no --summary '...'"
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_commit(args: argparse.Namespace) -> int:
    ws_dir = Path(args.workspace_dir).resolve()
    P = tracker_paths(ws_dir)
    pkt_path = P["tracker"] / args.run_id / "packet.json"
    pk = load_json(pkt_path, None)
    if not isinstance(pk, dict):
        print(json.dumps({"ok": False, "error": f"找不到数据包: {pkt_path}"}, ensure_ascii=False))
        return 2

    seen = set(load_json(P["seen"], []))
    new_ids = (
        set((pk.get("internal") or {}).get("new") or {})
        | set((pk.get("alphaengine") or {}).get("new") or {})
        | set((pk.get("sec") or {}).get("new") or {})
        | set((pk.get("x") or {}).get("new") or {})
    )
    seen |= new_ids
    dump_json(P["seen"], sorted(seen))

    committed_at = now_et().isoformat()
    last_run_obj = {
        "ticker": pk.get("ticker") or args.ticker,
        "run_id": args.run_id,
        "committed_at": committed_at,
        "since": pk.get("since"),
        "until": pk.get("until"),
        "snapshot_file": pk.get("snapshot_file"),
        "material": args.material,
        "summary": args.summary.strip(),
    }
    dump_json(P["last_run"], last_run_obj)

    P["log"].parent.mkdir(parents=True, exist_ok=True)
    with P["log"].open("a", encoding="utf-8") as f:
        f.write(f"{now_et().strftime('%Y-%m-%d %H:%M')} | {args.run_id} | material={args.material} | {args.summary.strip()}\n")

    res = {
        "ok": True,
        "committed": args.run_id,
        "material": args.material,
        "new_ids_marked_seen": len(new_ids),
        "seen_total": len(seen),
        "last_run_file": str(P["last_run"].relative_to(ws_dir)),
        "updates_log": str(P["log"].relative_to(ws_dir)),
    }
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ws_dir = Path(args.workspace_dir).resolve()
    P = tracker_paths(ws_dir)
    recent_log = P["log"].read_text(encoding="utf-8").splitlines()[-10:] if P["log"].exists() else []
    snaps = sorted(P["consensus"].glob("*.json")) if P["consensus"].exists() else []
    res = {
        "ok": True,
        "workspace_dir": str(ws_dir),
        "last_run": load_json(P["last_run"], {}),
        "seen_count": len(load_json(P["seen"], [])),
        "snapshots_count": len(snaps),
        "latest_snapshot": str(snaps[-1].relative_to(ws_dir)) if snaps else None,
        "recent_log": recent_log,
    }
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_self_test() -> int:
    thr = load_json(DEFAULT_THRESHOLDS_PATH, {})
    required_keys = {
        "eps_fy1_vs_last_pct": 1.0,
        "eps_fy1_vs_7d_pct": 2.0,
        "rev_fy1_or_q1_vs_last_pct": 0.7,
        "target_price_vs_last_pct": 2.0,
        "down_revisions_7d_min": 5,
        "price_1d_pct": 4.0,
        "price_5d_pct": 8.0,
        "any_new_sec_filing": True,
    }
    thr_ok = all(thr.get(k) == v for k, v in required_keys.items())

    prev = {
        "fetched_at": "2026-10-17T21:00:00-04:00",
        "earnings_estimate": {"+1y": {"avg": 30.0}},
        "revenue_estimate": {"+1y": {"avg": 200e9}},
        "price_targets": {"mean": 600.0},
    }
    cur = {
        "fetched_at": "2026-10-18T21:00:00-04:00",
        "earnings_estimate": {"+1y": {"avg": 30.6}},  # +2.0% vs last
        "revenue_estimate": {"+1y": {"avg": 202e9}},  # +1.0% vs last
        "eps_trend": {"+1y": {"current": 30.6, "7daysAgo": 29.8}},  # +2.68% vs 7d
        "eps_revisions": {"+1y": {"upLast7days": 8, "downLast7Days": 6}},
        "price_targets": {"mean": 618.0},  # +3.0% vs last
        "price": {"chg_1d_pct": -4.5, "chg_5d_pct": -8.2, "benchmark": "QQQ", "bench_chg_1d_pct": -0.8, "bench_chg_5d_pct": -1.5},
    }
    deltas, flags = compute_consensus_deltas(cur, prev, thr, sec_new_count=1)
    checks = {
        "default_thresholds_exact": thr_ok,
        "eps_fy1_delta_2pct": deltas.get("eps_+1y_vs_last_pct") == 2.0,
        "rev_fy1_delta_1pct": deltas.get("rev_+1y_vs_last_pct") == 1.0,
        "pt_delta_3pct": deltas.get("target_price_vs_last_pct") == 3.0,
        "flags_triggered": len(flags) == 8,
    }
    ok = all(checks.values())
    print(json.dumps({"ok": ok, "checks": checks, "sample_flags": flags}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Dalton 每晚 thesis 增量采集与状态提交工具 (PS.11)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_col = sub.add_parser("collect", help="采集一致预期快照与增量包，写入 research/tracker/<run_id>/")
    p_col.add_argument("--ticker", required=True, help="目标股票代码，如 META")
    p_col.add_argument("--workspace-dir", default=".", help="公司工作区目录路径（默认当前目录）")
    p_col.add_argument("--since", help="检索窗口起始日期 YYYY-MM-DD（默认上次 until - 1 天，首次回看 3 天）")
    p_col.add_argument("--run-id", help="指定 run_id（默认按美东时间 YYYYMMDD-HHMM 生成）")
    p_col.add_argument("--no-ae", action="store_true", help="跳过 AlphaEngine 检索")
    p_col.add_argument("--no-web", action="store_true", help="跳过公开网页/X 检索")
    p_col.add_argument("--json", action="store_true", help="输出 JSON 格式")

    p_com = sub.add_parser("commit", help="提交本轮增量结果，更新 seen.json、last_run.json 与 updates.log")
    p_com.add_argument("--workspace-dir", default=".", help="公司工作区目录路径（默认当前目录）")
    p_com.add_argument("--ticker", help="目标股票代码（可选，默认从 packet.json 读取）")
    p_com.add_argument("--run-id", required=True, help="要提交的 run_id")
    p_com.add_argument("--material", choices=["yes", "no"], required=True, help="本轮是否有重要变化")
    p_com.add_argument("--summary", required=True, help="本轮一句话结论摘要")

    p_st = sub.add_parser("status", help="查看工作区增量跟踪状态")
    p_st.add_argument("--workspace-dir", default=".", help="公司工作区目录路径（默认当前目录）")
    p_st.add_argument("--ticker", help="股票代码")

    sub.add_parser("self-test", help="验证默认阈值与一致预期差值计算")

    args = parser.parse_args()
    if args.cmd == "collect":
        return cmd_collect(args)
    if args.cmd == "commit":
        return cmd_commit(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "self-test":
        return cmd_self_test()
    return 1


if __name__ == "__main__":
    sys.exit(main())
