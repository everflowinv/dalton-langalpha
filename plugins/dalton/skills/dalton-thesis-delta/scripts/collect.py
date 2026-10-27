#!/usr/bin/env python3
"""
collect.py — 每晚 thesis 增量包采集与状态提交脚本（PS.11）

纯确定性代码，不调用大语言模型。
子命令：
  1. collect --ticker <TICKER> --workspace-dir <DIR> [--since YYYY-MM-DD] [--no-ae] [--no-web]
     - 读取覆盖阈值（优先 <workspace_dir>/research/tracker/thresholds.json，回退 config/default-thresholds.json）
     - 拉取一致预期快照并写入 <workspace_dir>/data/consensus/<YYYY-MM-DD_HHMM>.json
     - 与上一份一致预期快照比对（若 fetched_at 相同则跳过对比），生成 deltas 与 auto_flags
     - 检索未在 seen.json 中的六个内部来源（company-wiki, guidepoint, funda-ai, sales-notes, tmtb, alphaengine）与 SEC filing（前推2天）
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

INTERNAL_ROUTES = ["company-wiki", "guidepoint", "funda-ai", "sales-notes", "tmtb"]
ALL_INTERNAL_SOURCES = [*INTERNAL_ROUTES, "alphaengine"]
_GP_LIBRARY_TS_RE = re.compile(r"library_ts:([A-Za-z0-9-]+)")


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


RESEARCHDATA_SERVER = "researchdata"
# researchdata 的两条调用路：HTTP 桥（DALTON_RESEARCHDATA_URL，显式覆盖）与沙箱内
# PTC 生成的包装模块 tools.researchdata（ExecuteCode 调 researchdata 的同一模块；
# Bash 工具在运行本脚本前会 export PYTHONPATH 与 PTC_WORKSPACE_CONFIG，所以这里
# 的 import 与代码单元里一样成立）。两条都不可用是配置错误，collect 启动时应报
# 一次 researchdata_transport_unconfigured，而不是让每个来源各失败一次。
RESEARCHDATA_TRANSPORT_UNCONFIGURED = "researchdata_transport_unconfigured"


def resolve_researchdata_transport() -> dict[str, Any]:
    """选定 researchdata 调用路：DALTON_RESEARCHDATA_URL 桥优先（覆盖），否则包装模块。

    Returns:
        {"mode": "bridge"|"wrapper"|None, "call": url或callable, "error": 不可用原因}。
    """
    bridge_url = os.environ.get("DALTON_RESEARCHDATA_URL", "").strip()
    if bridge_url:
        return {"mode": "bridge", "call": bridge_url, "error": ""}

    try:
        from tools import researchdata as wrapper  # type: ignore
    except Exception as e:  # noqa: BLE001 - import 失败的任何形态都是"包装模块不可用"
        return {
            "mode": None,
            "call": None,
            "error": f"无法导入沙箱包装模块 tools.{RESEARCHDATA_SERVER}（{str(e)[:200]}）",
        }
    try:
        import mcp_client as mcp_client_module  # type: ignore # 包装模块经它调 MCP，缺它时包装模块只剩会抛错的桩
    except Exception as e:  # noqa: BLE001
        return {
            "mode": None,
            "call": None,
            "error": f"沙箱缺少 mcp_client 运行时（{str(e)[:200]}），包装模块无法调用 MCP",
        }
    config_path = os.environ.get(
        "PTC_WORKSPACE_CONFIG", ".agents/tools/mcp_client_config.json"
    )
    # 配置读不到时不能认为包装模块可用：没有它，启用集合未知、每次调用都会
    # 在运行时里失败——启动即报配置错误，而不是 7 个来源各失败一次。
    try:
        with open(config_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        enabled = set((cfg.get("servers") or {}).keys())
    except OSError as e:
        return {
            "mode": None,
            "call": None,
            "error": f"无法读取工作区 MCP 配置（{config_path}）：{str(e)[:160]}",
        }
    except ValueError as e:
        return {
            "mode": None,
            "call": None,
            "error": f"工作区 MCP 配置不可解析（{config_path}）：{str(e)[:160]}",
        }
    if RESEARCHDATA_SERVER not in enabled:
        return {
            "mode": None,
            "call": None,
            "error": f"工作区未启用 {RESEARCHDATA_SERVER} MCP 服务器（{config_path}）",
        }
    # 优先用保留 MCP isError 标志的入口（运行时随本仓库发布）；旧运行时里没有
    # 它时退回 _call_mcp_tool（值里看不到错误标志，只能靠 error 字段判错）。
    flagged = getattr(mcp_client_module, "call_mcp_tool_with_error_flag", None)
    if callable(flagged):
        return {"mode": "wrapper", "call": flagged, "error": ""}
    call_fn = getattr(wrapper, "_call_mcp_tool", None)
    if not callable(call_fn):
        return {
            "mode": None,
            "call": None,
            "error": f"tools.{RESEARCHDATA_SERVER} 模块缺少 _call_mcp_tool",
        }
    return {"mode": "wrapper", "call": call_fn, "error": ""}


def _extract_non_empty_error(obj: dict[str, Any]) -> str | None:
    """提取字典中非空的 error 字段文本；空串、None、False 不算错误。"""
    raw_err = obj.get("error")
    if raw_err is None or raw_err is False:
        return None
    if isinstance(raw_err, str):
        stripped = raw_err.strip()
        return stripped or None
    if isinstance(raw_err, (dict, list)):
        return json.dumps(raw_err, ensure_ascii=False) if raw_err else None
    text = str(raw_err).strip()
    return text or None


def _extract_mcp_content_error_text(obj: dict[str, Any]) -> str | None:
    """当 MCP 原始包返回 content 文本块时提取其中的错误文本。"""
    content = obj.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            if text:
                return text
    return None


def _normalize_dict_result(
    value: dict[str, Any],
    *,
    outer_is_error: bool = False,
    default_error: str = "wrapper_call_failed",
) -> dict[str, Any]:
    structured = (
        value.get("structuredContent")
        if isinstance(value.get("structuredContent"), dict)
        else None
    )
    err_from_value = _extract_non_empty_error(value) or (
        _extract_non_empty_error(structured) if structured else None
    )
    mcp_error = bool(
        outer_is_error
        or value.get("isError") is True
        or value.get("is_error") is True
        or (
            structured
            and (
                structured.get("isError") is True
                or structured.get("is_error") is True
            )
        )
    )
    payload_ok = (
        value.get("ok")
        if "ok" in value
        else (structured.get("ok") if structured else None)
    )
    raw_status = value.get("status") or (
        structured.get("status") if structured else None
    )
    status_str = str(raw_status or "").strip()
    is_ok = (
        not mcp_error
        and err_from_value is None
        and payload_ok is not False
        and status_str not in ("error", "failed")
    )
    if is_ok:
        err_msg = None
        final_status = status_str or "ok"
    else:
        reason = value.get("reason") or (
            structured.get("reason") if structured else None
        )
        message = value.get("message") or (
            structured.get("message") if structured else None
        )
        content_err = _extract_mcp_content_error_text(value)
        err_msg = str(
            err_from_value
            or reason
            or message
            or content_err
            or (status_str if status_str and status_str != "ok" else "")
            or default_error
        )
        final_status = (
            status_str if status_str and status_str != "ok" else "error"
        )
    return {
        "ok": is_ok,
        "status": final_status,
        "error": err_msg,
        "data": value,
    }


def _call_wrapper(call_fn: Any, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """经包装模块调用一个 researchdata 工具，返回与 HTTP 桥路径同构的结果。

    call_fn 是运行时的 call_mcp_tool_with_error_flag 时返回 (is_error, value)：
    MCP isError=true 的应答解包后值里没有错误标志（如 {"message": "unauthorized"}），
    is_error 是唯一判据，为真即失败并保留值里的错误文本。"""
    try:
        result = call_fn(RESEARCHDATA_SERVER, tool_name, arguments)
    except Exception as e:  # noqa: BLE001 - MCP 调用失败按来源失败记录，不让脚本崩溃
        return {"ok": False, "status": "exception", "error": str(e)[:300]}
    is_error = False
    value = result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
        is_error, value = result
    if isinstance(value, str):
        raw_text = value.strip()
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {
                "ok": False,
                "status": "empty_bridge_content",
                "error": raw_text[:300] if raw_text else "wrapper returned non-JSON text",
            }
    if isinstance(value, dict):
        return _normalize_dict_result(
            value, outer_is_error=is_error, default_error="wrapper_call_failed"
        )
    return {
        "ok": False,
        "status": "invalid_format",
        "error": f"wrapper returned non-dict result ({type(value).__name__}): {str(value)[:200]}",
        "data": clean_val(value),
    }


def _call_bridge_http(
    bridge_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    """经 DALTON_RESEARCHDATA_URL 指向的 HTTP 桥调用 researchdata 工具。"""
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
        if isinstance(raw, dict) and raw.get("error"):
            return {"ok": False, "status": "rpc_error", "error": str(raw.get("error"))}
        res = raw.get("result") or {}
        content = res.get("content") or []
        if content and isinstance(content[0], dict) and content[0].get("text"):
            data = json.loads(content[0]["text"])
            if isinstance(data, dict):
                return _normalize_dict_result(
                    data,
                    outer_is_error=bool(res.get("isError", False)),
                    default_error="bridge_call_failed",
                )
            return {
                "ok": False,
                "status": "invalid_format",
                "error": f"bridge returned non-dict result ({type(data).__name__})",
                "data": clean_val(data),
            }
        if isinstance(res, dict) and (
            res.get("isError") is True
            or isinstance(res.get("structuredContent"), dict)
            or _extract_non_empty_error(res)
        ):
            return _normalize_dict_result(
                res,
                outer_is_error=bool(res.get("isError", False)),
                default_error="bridge_call_failed",
            )
        return {"ok": False, "status": "empty_bridge_content", "error": str(raw.get("error") or "empty_bridge_content")}
    except Exception as e:
        return {"ok": False, "status": "exception", "error": str(e)[:300]}


def call_bridge_jsonrpc(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
    """调用一个 researchdata 工具：HTTP 桥覆盖优先，默认走沙箱包装模块（tools.researchdata）。"""
    transport = resolve_researchdata_transport()
    if transport["mode"] == "bridge":
        return _call_bridge_http(transport["call"], tool_name, arguments, timeout)
    if transport["mode"] == "wrapper":
        return _call_wrapper(transport["call"], tool_name, arguments)
    return {
        "ok": False,
        "status": RESEARCHDATA_TRANSPORT_UNCONFIGURED,
        "error": transport["error"] or RESEARCHDATA_TRANSPORT_UNCONFIGURED,
    }


def build_guidepoint_query(meta: dict[str, Any]) -> str:
    """构造 Guidepoint 检索词：合并公司名与 ticker（去重）。"""
    ticker = str(meta.get("ticker") or "").strip()
    name = str(meta.get("name") or "").strip()
    parts = [p for p in dict.fromkeys([ticker, name]) if p]
    return " ".join(parts) or ticker


def build_internal_route_request(
    route: str,
    meta: dict[str, Any],
    since: str,
    until: str,
) -> tuple[str, dict[str, Any]]:
    """构造六个内部研究来源（company-wiki, guidepoint, funda-ai, sales-notes, tmtb, alphaengine）的请求参数。"""
    aliases = meta.get("aliases") or [meta.get("ticker") or "", meta.get("name") or ""]
    query_str = " ".join(str(a) for a in aliases if a).strip() or str(meta.get("ticker") or "")

    if route == "company-wiki":
        return (
            "research",
            {
                "route": "company-wiki",
                "operation": "list",
                "limit": 50,
                "params": {"indexed_since": since, "media": "text"},
            },
        )
    if route == "guidepoint":
        return (
            "research",
            {
                "route": "guidepoint",
                "operation": "search_library",
                "query": build_guidepoint_query(meta),
                "start": since,
                "end": until,
                "limit": 20,
            },
        )
    if route == "alphaengine":
        ae_start = (date.fromisoformat(since) - timedelta(days=3)).isoformat()
        return (
            "research",
            {
                "route": "alphaengine",
                "operation": "search_library",
                "query": str(meta.get("name") or meta.get("ticker") or ""),
                "start": ae_start,
                "end": until,
                "limit": 20,
                "params": {"sort": "relevance"},
            },
        )
    if route in ("sales-notes", "tmtb", "funda-ai"):
        return (
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
    raise ValueError(f"Unsupported internal route: {route}")


def build_sec_filing_request(ticker: str, form: str, limit: int = 5) -> tuple[str, dict[str, Any]]:
    """构造 SEC list-filings 请求的 (tool_name, arguments)。"""
    return (
        "finance",
        {
            "route": "sec",
            "operation": "list-filings",
            "ticker": ticker,
            "form": form,
            "limit": limit,
        },
    )


def _extract_route_items(data: Any) -> list[dict[str, Any]]:
    """从 bridge 响应（含 MCP 代理 structuredContent / data 嵌套层）提取条目列表。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("results", "items", "documents", "rows"):
        val = data.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
    sc = data.get("structuredContent")
    if isinstance(sc, dict):
        for key in ("results", "data", "items", "documents"):
            val = sc.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
    inner = data.get("data")
    if isinstance(inner, list):
        return [x for x in inner if isinstance(x, dict)]
    if isinstance(inner, dict):
        items = _extract_route_items(inner)
        if items:
            return items
        content = inner.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    try:
                        parsed = json.loads(block["text"])
                        nested = _extract_route_items(parsed)
                        if nested:
                            return nested
                    except Exception:
                        continue
    return []


def _normalize_guidepoint_doc_id(r: dict[str, Any]) -> str | None:
    """将 Guidepoint 文档 ID 规范化为与 seen.json 一致的 `guidepoint:<id>`。"""
    raw = str(
        r.get("document_id")
        or r.get("id")
        or r.get("transcript_id")
        or r.get("doc_id")
        or ""
    ).strip()
    if not raw:
        for field in ("snippet", "summary", "title", "transcript_name"):
            m = _GP_LIBRARY_TS_RE.search(str(r.get(field) or ""))
            if m:
                raw = m.group(1)
                break
    if not raw:
        return None
    m = _GP_LIBRARY_TS_RE.search(raw)
    if m:
        raw = m.group(1)
    if raw.startswith("guidepoint:"):
        return raw
    return f"guidepoint:{raw}"


def summarize_source_failures(
    internal_data: dict[str, Any],
    ae_data: dict[str, Any] | None = None,
    sec_data: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """汇总所有 ok=False 的取数来源及原因，供短路门禁与 packet/汇总输出使用。"""
    failures: list[dict[str, str]] = []
    seen_sources: set[str] = set()
    int_status = (internal_data or {}).get("status") or {}
    if isinstance(int_status, dict):
        for route, st in int_status.items():
            if isinstance(st, dict) and not st.get("ok", False):
                reason = str(st.get("error") or st.get("status") or "unknown_error")
                failures.append({"source": str(route), "error": reason})
                seen_sources.add(str(route))

    ae_status = (ae_data or {}).get("status") or []
    if isinstance(ae_status, list) and "alphaengine" not in seen_sources:
        for st in ae_status:
            if isinstance(st, dict) and not st.get("skipped") and not st.get("ok", False):
                reason = str(st.get("error") or st.get("status") or "unknown_error")
                failures.append({"source": "alphaengine", "error": reason})
                seen_sources.add("alphaengine")
                break

    sec_status = (sec_data or {}).get("status") or []
    if isinstance(sec_status, list):
        failed_forms = [
            f"{st.get('form')}:{st.get('error') or st.get('status') or 'failed'}"
            for st in sec_status
            if isinstance(st, dict) and not st.get("ok", False)
        ]
        if failed_forms:
            failures.append({"source": "sec", "error": "; ".join(failed_forms)})

    return failures


def can_short_circuit(
    *,
    auto_flags: list[str],
    internal_data: dict[str, Any],
    ae_data: dict[str, Any],
    sec_data: dict[str, Any],
) -> bool:
    """短路判断：要求所有来源都 ok:true，且 auto_flags 为空，且所有来源都是 0 新增。"""
    if summarize_source_failures(internal_data, ae_data, sec_data):
        return False
    if auto_flags:
        return False
    total_new = (
        len((internal_data or {}).get("new") or {})
        + len((ae_data or {}).get("new") or {})
        + len((sec_data or {}).get("new") or {})
    )
    return total_new == 0


def collect_internal_and_ae(
    meta: dict[str, Any],
    since: str,
    until: str,
    seen: set[str],
    skip_ae: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """采集六个内部研究来源（company-wiki, guidepoint, funda-ai, sales-notes, tmtb, alphaengine）新增文档。"""
    internal_new: dict[str, Any] = {}
    internal_status: dict[str, Any] = {}
    ae_new: dict[str, Any] = {}
    ae_status: list[dict[str, Any]] = []

    pat = re.compile("|".join(re.escape(a) for a in meta["aliases"] if a), re.IGNORECASE)

    for route in INTERNAL_ROUTES:
        tool_name, req_args = build_internal_route_request(route, meta, since, until)
        res = call_bridge_jsonrpc(tool_name, req_args)
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        items = _extract_route_items(data)
        ok = bool(res.get("ok", False))
        err_msg = res.get("error") or data.get("error")
        status_val = data.get("status") or res.get("status")

        if route == "company-wiki":
            internal_status[route] = {
                "ok": ok,
                "listed": len(items),
                "status": status_val,
                **({"error": err_msg} if err_msg else {}),
            }
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
        elif route == "guidepoint":
            internal_status[route] = {
                "ok": ok,
                "returned": len(items),
                "status": status_val,
                **({"error": err_msg} if err_msg else {}),
            }
            for r in items:
                did = _normalize_guidepoint_doc_id(r)
                if not did or did in seen:
                    continue
                item_date = str(
                    r.get("date")
                    or r.get("published_at")
                    or r.get("publish_date")
                    or r.get("created_at")
                    or ""
                ).strip()
                date_prefix = item_date[:10]
                if re.match(r"^\d{4}-\d{2}-\d{2}$", date_prefix):
                    if date_prefix < since or date_prefix > until:
                        continue
                internal_new[did] = {
                    "route": "guidepoint",
                    "title": r.get("transcript_name") or r.get("title") or r.get("headline"),
                    "date": date_prefix or item_date or None,
                    "from": r.get("original_source") or r.get("source") or "Guidepoint",
                    "snippet": str(r.get("snippet") or r.get("summary") or "")[:600],
                }
        else:
            internal_status[route] = {
                "ok": ok,
                "returned": len(items),
                "status": status_val,
                **({"error": err_msg} if err_msg else {}),
            }
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
        ae_status.append({"skipped": "--no-ae", "ok": True})
        internal_status["alphaengine"] = {"ok": True, "skipped": "--no-ae", "status": "skipped"}
    else:
        tool_name, req_args = build_internal_route_request("alphaengine", meta, since, until)
        res = call_bridge_jsonrpc(tool_name, req_args)
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        items = _extract_route_items(data)
        ok = bool(res.get("ok", False))
        err_msg = res.get("error") or data.get("error")
        status_val = data.get("status") or res.get("status")
        ae_status.append({
            "query": meta["name"],
            "ok": ok,
            "returned": len(items),
            "status": status_val,
            "error": err_msg,
        })
        internal_status["alphaengine"] = {
            "ok": ok,
            "returned": len(items),
            "status": status_val,
            **({"error": err_msg} if err_msg else {}),
        }
        for r in items:
            doc_id = r.get("doc_id") or r.get("document_id") or r.get("id")
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
        tool_name, req_args = build_sec_filing_request(meta["ticker"], form, limit=5)
        res = call_bridge_jsonrpc(tool_name, req_args)
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        rows = data.get("rows") or []
        ok = bool(res.get("ok", False))
        err_msg = res.get("error") or data.get("error")
        status.append({
            "form": form,
            "ok": ok,
            "count": len(rows),
            **({"error": err_msg} if err_msg else {}),
        })
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
    internal_obj = pk.get("internal") or {}
    ae_obj = pk.get("alphaengine") or {}
    sec_obj = pk.get("sec") or {}
    failures = pk.get("failed_sources")
    if not isinstance(failures, list):
        failures = summarize_source_failures(internal_obj, ae_obj, sec_obj)
    all_ok = len(failures) == 0

    lines = [
        f"# {pk['ticker']} 每晚增量数据包（{pk['run_id']}）",
        "",
        f"- **窗口**：{pk['since']} 至 {pk['until']}（美东）",
        f"- **上次成功提交**：{pk.get('last_committed_at') or '首次运行'}",
        f"- **一致预期快照文件**：`{pk.get('snapshot_file')}`",
        f"- **全量来源取数状态（`all_sources_ok`）**：`{str(all_ok).lower()}`",
        f"- **是否允许短路结束（`short_circuit_eligible`）**：`{str(bool(pk.get('short_circuit_eligible', False))).lower()}`",
    ]
    if failures:
        lines.append("- **取数失败来源（严禁当成“本轮无新文档”，严禁走短路分支）**：")
        for f_item in failures:
            lines.append(f"  - ⚠️ `{f_item.get('source')}` 本轮取数失败（{f_item.get('error')}）")
    lines.extend([
        "",
        "## 一、自动触发提示（`auto_flags`，是否重要需按 `materiality.md` 判断）",
    ])
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

    lines.extend(["", "## 三、内部库新文档（company-wiki / guidepoint / funda-ai / sales-notes / tmtb）"])
    int_status = internal_obj.get("status") if isinstance(internal_obj.get("status"), dict) else {}
    if int_status:
        lines.append(f"- **内部来源状态（`internal_status`）**：`{json.dumps(int_status, ensure_ascii=False)}`")
        for r_name in INTERNAL_ROUTES:
            st = int_status.get(r_name)
            if isinstance(st, dict) and not st.get("ok", False):
                lines.append(f"- ⚠️ **{r_name} 本轮取数失败（{st.get('error') or st.get('status') or 'unknown_error'}）**")
    int_new = internal_obj.get("new") or {}
    if int_new:
        for did, info in int_new.items():
            lines.append(f"- `{did}`｜{info.get('date')}｜{info.get('route')}｜{info.get('title')}｜{info.get('from') or ''}")
            if info.get("snippet"):
                lines.append(f"  - 摘要：{info['snippet']}")
    elif any(
        isinstance(int_status.get(r_name), dict) and not int_status[r_name].get("ok", False)
        for r_name in INTERNAL_ROUTES
    ):
        lines.append("- 本节存在取数失败来源（见上方告警），不可视为“本轮无新文档”")
    else:
        lines.append("- 无")

    lines.extend(["", "## 四、AlphaEngine 新研报与点评"])
    ae_st_entry = int_status.get("alphaengine")
    if isinstance(ae_st_entry, dict) and not ae_st_entry.get("ok", False):
        lines.append(f"- ⚠️ **alphaengine 本轮取数失败（{ae_st_entry.get('error') or ae_st_entry.get('status') or 'unknown_error'}）**")
    ae_new = ae_obj.get("new") or {}
    if ae_new:
        for did, info in ae_new.items():
            lines.append(f"- `{did}`｜{info.get('publish_time')}｜{info.get('title')}")
            if info.get("snippet"):
                lines.append(f"  - 摘要：{info['snippet']}")
    elif isinstance(ae_st_entry, dict) and not ae_st_entry.get("ok", False):
        lines.append("- alphaengine 本轮取数失败，不可视为“本轮无新文档”")
    else:
        lines.append("- 无")

    lines.extend(["", "## 五、SEC 新公告"])
    sec_status = sec_obj.get("status") if isinstance(sec_obj.get("status"), list) else []
    failed_sec = [st for st in sec_status if isinstance(st, dict) and not st.get("ok", False)]
    for st in failed_sec:
        lines.append(f"- ⚠️ **sec ({st.get('form')}) 本轮取数失败（{st.get('error') or 'failed'}）**")
    sec_new = sec_obj.get("new") or {}
    if sec_new:
        for acc, info in sec_new.items():
            lines.append(f"- `{acc}`｜{info.get('form')}｜提交日 {info.get('filing_date')}｜报告期 {info.get('report_date') or ''}")
    elif failed_sec:
        lines.append("- SEC 本轮取数失败，不可视为“本轮无新公告”")
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

    # 启动即核 researchdata 调用路；两条都不可用是配置错误，一次性返回，不逐来源失败。
    transport = resolve_researchdata_transport()
    if transport["mode"] is None:
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": RESEARCHDATA_TRANSPORT_UNCONFIGURED,
                    "ticker": ticker,
                    "error": transport["error"],
                    "hint": (
                        "沙箱内应经 PTC 包装模块（tools.researchdata）调用，或设 "
                        "DALTON_RESEARCHDATA_URL 走 HTTP 桥；两者皆不可用，本轮未采集任何来源，"
                        "按取数失败如实汇报，不要当成“本轮无新文档”"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

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

    failed_sources = summarize_source_failures(internal_data, ae_data, sec_data)
    all_sources_ok = len(failed_sources) == 0
    short_circuit_eligible = can_short_circuit(
        auto_flags=auto_flags,
        internal_data=internal_data,
        ae_data=ae_data,
        sec_data=sec_data,
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
        "all_sources_ok": all_sources_ok,
        "failed_sources": failed_sources,
        "short_circuit_eligible": short_circuit_eligible,
        "internal_status": internal_data["status"],
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
        "ok": all_sources_ok,
        "all_sources_ok": all_sources_ok,
        "failed_sources": failed_sources,
        "short_circuit_eligible": short_circuit_eligible,
        "ticker": ticker,
        "run_id": run_id,
        "since": since,
        "until": until,
        "packet_json": str(pkt_json_path.relative_to(ws_dir)),
        "packet_md": str(pkt_md_path.relative_to(ws_dir)),
        "snapshot_file": rel_snap,
        "auto_flags": auto_flags,
        "internal_status": internal_data["status"],
        "alphaengine_status": ae_data["status"],
        "sec_status": sec_data["status"],
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
