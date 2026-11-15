#!/usr/bin/env python3
"""每周 LLM Token 用量与刊例价成本周报 -> 飞书卡片 (Card Schema 2.0)

基于 skills/model-usage-cost 统计过去 7 天本机命令行工具 + Dalton 生产 LLM 用量：
1. 总量与成本拆分为「本机命令行工具」与「Dalton 生产」两部分并给出合计；
2. 无公开价模型单列（带 reference_only 的附参考价估算并标明仅供参考），不计入总成本；
3. 页脚展示价格表日期及生效说明（如 Argon/Carbon 12-01 起调价）；
4. 构造飞书 Card Schema 2.0 互动卡片并写入 $DALTON_OUTBOX_FILE（或 --dry-run 打印卡片 JSON）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DALTON_PROD_SOURCE = "Dalton 生产（订阅）"
COLLECTOR_STALE_SECONDS = 600

_ISSUE_LABELS: dict[str, str] = {
    "malformed_json": "JSON 解析失败",
    "missing_timestamp": "缺少时间戳",
    "invalid_usage": "无效用量数据",
    "conflicting_identity_usage": "同标识冲突记录",
    "agy_db_unreadable": "Antigravity 库不可读",
    "agy_db_no_steps_table": "Antigravity 库缺表",
    "agy_gateway_unreconciled": "Antigravity 网关未对账",
    "agy_gateway_db_missing": "Antigravity 网关缺库记录",
    "agy_model_unresolved": "Antigravity 枚举待识别",
    "agy_message_without_authority": "Antigravity 缺权威冲突",
    "codex_trajectory_usage_mismatch": "Codex trajectory 冲突",
    "gateway_trajectory_unreconciled": "网关/trajectory 冲突",
    "zcode_db_rollout_unreconciled": "ZCode 库/rollout 冲突",
    "muse_gateway_sum_mismatch": "Muse 网关汇总冲突",
    "dalton_prod_unavailable": "Dalton 生产未配置",
    "dalton_prod_fetch_failed": "Dalton 生产读取失败",
}


def resolve_state_dir() -> Path:
    raw = (os.environ.get("DALTON_USAGE_STATE_DIR") or "").strip()
    return Path(raw or "~/.openclaw/usage-accounting").expanduser()


def resolve_workspace_dir() -> Path:
    raw = (os.environ.get("OPENCLAW_WORKSPACE") or os.environ.get("DALTON_DATA_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent.parent


def format_tokens(n: int) -> str:
    """格式化 Token 数量（B/M/K）。"""
    if abs(n) >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def format_usd(val: float | None) -> str:
    """格式化美元金额。"""
    if val is None:
        return "无公开价"
    return f"${val:,.2f}"


def normalize_provider(prov_str: str, model: str = "") -> str:
    """归一化提供商名称，便于汇总展示。"""
    p = prov_str.lower()
    m = model.lower()
    if m.startswith("claude-"):
        return "Anthropic"
    if m.startswith("gemini-"):
        return "Google"
    if m.startswith("muse-"):
        return "Meta"
    if m.startswith("agy-enum-"):
        return "Antigravity（模型待识别）"
    if "bigmodel" in p:
        return "智谱 (GLM)"
    if p.startswith(("builtin:", "account:")):
        return "ZCode 内建"
    if "openai" in p:
        return "OpenAI"
    if "claude" in p or "anthropic" in p:
        return "Anthropic"
    if "google" in p or "antigravity" in p:
        return "Google"
    if "zai" in p or "zhipu" in p or "glm" in p:
        return "智谱 (GLM)"
    if "deepseek" in p:
        return "DeepSeek"
    if "xai" in p:
        return "xAI"
    if "muse" in p:
        return "Meta"
    return prov_str


def _is_dalton_prod_row(row: dict[str, Any]) -> bool:
    return bool(row.get("dalton_prod")) or DALTON_PROD_SOURCE in str(row.get("source") or "")


def _row_record_count(row: dict[str, Any]) -> int:
    if row.get("records") is not None:
        return int(row["records"])
    return 1 if _is_dalton_prod_row(row) else int(row.get("calls") or 0)


def merge_rows_by_model(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一厂商同一模型合并为一行。"""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["norm_provider"], r["model"])
        m = merged.get(key)
        if m is None:
            m = merged[key] = {
                "norm_provider": r["norm_provider"],
                "model": r["model"],
                "providers": [],
                "sources": [],
                "records": 0,
                "calls": 0,
                "runs": 0,
                "fresh": 0,
                "cache_write": 0,
                "cache_read": 0,
                "output": 0,
                "total_tokens": 0,
                "prompt_tokens": 0,
                "cost_usd": None,
                "unpriced_tokens": 0,
                "estimated_split_records": 0,
                "units": {},
                "no_public_price": False,
                "reference_cost_usd": None,
                "reference_model": None,
                "price_note": None,
            }
        for field, value in (("providers", r["provider"]), ("sources", r.get("source"))):
            if value and value not in m[field]:
                m[field].append(value)
        m["records"] += _row_record_count(r)
        for field in ("calls", "fresh", "cache_write", "cache_read", "output", "total_tokens", "prompt_tokens"):
            m[field] += int(r.get(field) or 0)
        m["runs"] += int(r.get("runs") or 0)
        m["estimated_split_records"] += int(r.get("estimated_split_records") or 0)
        m["unpriced_tokens"] += int(
            r.get("unpriced_tokens", r["total_tokens"] if r.get("cost_usd") is None else 0)
        )
        if r.get("cost_usd") is not None:
            m["cost_usd"] = (m["cost_usd"] or 0.0) + float(r["cost_usd"])
        if r.get("no_public_price"):
            m["no_public_price"] = True
        if r.get("reference_cost_usd") is not None:
            m["reference_cost_usd"] = (m["reference_cost_usd"] or 0.0) + float(r["reference_cost_usd"])
        if r.get("reference_model") and not m["reference_model"]:
            m["reference_model"] = r["reference_model"]
        if r.get("price_note") and not m["price_note"]:
            m["price_note"] = r["price_note"]
        for unit, count in r.get("units", {}).items():
            m["units"][unit] = m["units"].get(unit, 0) + count
    for m in merged.values():
        m["provider"] = "+".join(m["providers"])
        m["source"] = "|".join(m["sources"])
    return list(merged.values())


def fetch_usage_data(days: int = 7, as_of: str | None = None) -> dict[str, Any]:
    """通过 model-usage-cost skill 获取过去 N 天的汇总数据。"""
    workspace_dir = resolve_workspace_dir()
    skill_dir = workspace_dir / "skills" / "model-usage-cost" / "scripts"
    cli_py = skill_dir / "model_usage_cost_cli.py"
    run_sh = skill_dir / "run.sh"
    if not cli_py.is_file():
        local_cli = Path(__file__).resolve().parent.parent / "skills" / "model-usage-cost" / "scripts" / "model_usage_cost_cli.py"
        if local_cli.is_file():
            cli_py = local_cli
            run_sh = local_cli.parent / "run.sh"

    if cli_py.is_file():
        cmd = [sys.executable, str(cli_py), "--json", "summary", "--days", str(days), "--by-model"]
    elif run_sh.is_file():
        cmd = ["bash", str(run_sh), "--json", "summary", "--days", str(days), "--by-model"]
    else:
        raise FileNotFoundError("找不到 model-usage-cost 统计脚本")

    if as_of:
        cmd += ["--as-of", as_of]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"解析 model-usage-cost JSON 输出失败: {exc}\nSTDOUT: {res.stdout[:500]}") from exc
    return data


def _summarize_bucket(bucket_rows: list[dict[str, Any]]) -> dict[str, Any]:
    fresh = sum(int(r.get("fresh") or 0) for r in bucket_rows)
    cw = sum(int(r.get("cache_write") or 0) for r in bucket_rows)
    cr = sum(int(r.get("cache_read") or 0) for r in bucket_rows)
    out = sum(int(r.get("output") or 0) for r in bucket_rows)
    tokens = fresh + cw + cr + out
    prompt = fresh + cw + cr
    cost = sum(float(r["cost_usd"]) for r in bucket_rows if r.get("cost_usd") is not None)
    unpriced = sum(int(r.get("unpriced_tokens", tokens if r.get("cost_usd") is None else 0)) for r in bucket_rows)
    if not any("unpriced_tokens" in r for r in bucket_rows):
        unpriced = sum(
            (int(r.get("fresh") or 0) + int(r.get("cache_write") or 0) + int(r.get("cache_read") or 0) + int(r.get("output") or 0))
            for r in bucket_rows
            if r.get("cost_usd") is None
        )
    records = sum(_row_record_count(r) for r in bucket_rows)
    calls = sum(int(r.get("calls") or 0) for r in bucket_rows)
    return {
        "fresh": fresh,
        "cache_write": cw,
        "cache_read": cr,
        "output": out,
        "tokens": tokens,
        "prompt": prompt,
        "cost": cost,
        "unpriced_tokens": unpriced,
        "records": records,
        "calls": calls,
        "models": len({(r.get("norm_provider") or r.get("provider"), r["model"]) for r in bucket_rows}),
        "cache_hit_rate": (cr / prompt * 100) if prompt > 0 else 0.0,
    }


def process_usage_stats(raw_data: dict[str, Any]) -> dict[str, Any]:
    """处理并计算关键指标、本机/Dalton 拆分、无公开价清单及页脚信息。"""
    if raw_data.get("ok") is False:
        raise ValueError("计量采集失败，停止生成周报")
    window = raw_data["windows"][0]
    if window.get("row_count", len(window["rows"])) != len(window["rows"]):
        raise ValueError("模型行被裁剪，停止生成不完整周报")
    from_iso = window["from"]
    to_iso = window["to"]
    raw_rows = window["rows"]

    for r in raw_rows:
        r["total_tokens"] = r["fresh"] + r["cache_write"] + r["cache_read"] + r["output"]
        r["prompt_tokens"] = r["fresh"] + r["cache_write"] + r["cache_read"]
        r["norm_provider"] = normalize_provider(r["provider"], r["model"])
        if "unpriced_tokens" not in r:
            r["unpriced_tokens"] = r["total_tokens"] if r.get("cost_usd") is None else 0

    local_raw = [r for r in raw_rows if not _is_dalton_prod_row(r)]
    dalton_raw = [r for r in raw_rows if _is_dalton_prod_row(r)]
    local_bucket = _summarize_bucket(local_raw)
    dalton_bucket = _summarize_bucket(dalton_raw)

    dalton_prod = raw_data.get("dalton_prod")
    if dalton_prod is None:
        dalton_prod = {
            "ok": True,
            "error": None,
            "runs": sum(int(r.get("runs") or 0) for r in dalton_raw),
            "calls": sum(int(r.get("calls") or 0) for r in dalton_raw),
            "models": dalton_bucket["models"],
        }
    else:
        dalton_prod = dict(dalton_prod)
    dalton_bucket["runs"] = int(dalton_prod.get("runs") or 0)
    dalton_bucket["api_calls"] = int(dalton_prod.get("calls") or dalton_bucket["calls"])

    rows = merge_rows_by_model(raw_rows)
    rows_by_tokens = sorted(rows, key=lambda x: -x["total_tokens"])

    total_fresh = sum(r["fresh"] for r in rows)
    total_cw = sum(r["cache_write"] for r in rows)
    total_cr = sum(r["cache_read"] for r in rows)
    total_out = sum(r["output"] for r in rows)
    total_tokens = total_fresh + total_cw + total_cr + total_out
    total_prompt = total_fresh + total_cw + total_cr
    total_records = sum(r["records"] for r in rows)
    total_calls = sum(r["calls"] for r in rows)
    total_cost = sum((r["cost_usd"] or 0.0) for r in rows)
    unpriced_tokens = sum(r["unpriced_tokens"] for r in rows)
    estimated_split_records = sum(r["estimated_split_records"] for r in rows)
    cache_hit_rate = (total_cr / total_prompt * 100) if total_prompt > 0 else 0.0

    expected = window.get("totals")
    if expected:
        if total_tokens != expected["tokens"] or total_records != expected["records"]:
            raise ValueError("周报聚合与计量总表不一致")
        if expected.get("calls") is not None and total_calls != int(expected["calls"]):
            raise ValueError("周报调用次数与计量总表不一致")
        if abs(total_cost - expected["priced_cost_usd"]) > 0.0001 or unpriced_tokens != expected["unpriced_tokens"]:
            raise ValueError("周报价格聚合与计量总表不一致")

    prov_rollup: dict[str, dict[str, Any]] = {}
    for r in rows:
        p_name = r["norm_provider"]
        entry = prov_rollup.setdefault(p_name, {"cost": 0.0, "tokens": 0, "calls": 0, "unpriced_tokens": 0})
        entry["cost"] += r["cost_usd"] or 0.0
        entry["tokens"] += r["total_tokens"]
        entry["calls"] += r["calls"]
        entry["unpriced_tokens"] += r["unpriced_tokens"]

    prov_list = []
    for p_name, p_data in prov_rollup.items():
        prov_list.append({
            "provider": p_name,
            "cost": p_data["cost"],
            "cost_pct": (p_data["cost"] / total_cost * 100) if total_cost > 0 else 0.0,
            "tokens": p_data["tokens"],
            "tokens_pct": (p_data["tokens"] / total_tokens * 100) if total_tokens > 0 else 0.0,
            "calls": p_data["calls"],
            "unpriced_tokens": p_data["unpriced_tokens"],
        })
    prov_list.sort(key=lambda x: -x["cost"])

    top10 = rows_by_tokens[:10]
    for idx, r in enumerate(top10, 1):
        r["rank"] = idx
        r["token_pct"] = (r["total_tokens"] / total_tokens * 100) if total_tokens > 0 else 0.0
        c = r["cost_usd"]
        r["cost_pct"] = (c / total_cost * 100) if (c is not None and total_cost > 0) else 0.0

    tail = rows_by_tokens[10:]
    tail_tokens = sum(r["total_tokens"] for r in tail)
    tail_cost = sum((r["cost_usd"] or 0.0) for r in tail)
    tail_calls = sum(r["calls"] for r in tail)

    unpriced_models = [
        {
            "model": r["model"],
            "norm_provider": r["norm_provider"],
            "source": r["source"],
            "tokens": r["total_tokens"],
            "unpriced_tokens": r["unpriced_tokens"],
            "cost_usd": r["cost_usd"],
            "reference_cost_usd": r.get("reference_cost_usd"),
            "reference_model": r.get("reference_model"),
            "price_note": r.get("price_note"),
        }
        for r in rows_by_tokens
        if r["unpriced_tokens"] > 0 or r["cost_usd"] is None
    ]

    price_catalog = dict(raw_data.get("price_catalog") or {})

    return {
        "days": window["days"],
        "from_iso": from_iso,
        "to_iso": to_iso,
        "total_tokens": total_tokens,
        "total_fresh": total_fresh,
        "total_cw": total_cw,
        "total_cr": total_cr,
        "total_out": total_out,
        "total_prompt": total_prompt,
        "total_records": total_records,
        "total_calls": total_calls,
        "total_cost": total_cost,
        "unpriced_tokens": unpriced_tokens,
        "unpriced_models": unpriced_models,
        "local_stats": local_bucket,
        "dalton_stats": dalton_bucket,
        "dalton_prod": dalton_prod,
        "collector_status": inspect_collector_status(raw_data),
        "price_catalog": price_catalog,
        "estimated_split_records": estimated_split_records,
        "time_estimated_records": expected.get("request_start_time_records", 0) if expected else 0,
        "tier_context_unknown_records": expected.get("tier_context_unknown_records", 0) if expected else 0,
        "provider_reported_cost_records": expected.get("provider_reported_cost_records", 0) if expected else 0,
        "cache_hit_rate": cache_hit_rate,
        "prov_list": prov_list,
        "top10": top10,
        "tail": tail,
        "tail_tokens": tail_tokens,
        "tail_cost": tail_cost,
        "tail_calls": tail_calls,
        "coverage": raw_data.get("coverage", {}),
        "notes": raw_data.get("notes", []),
        "version": raw_data.get("version", "unknown"),
    }


def _parse_iso_timestamp(raw: Any) -> float | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw) / (1000.0 if raw > 1e11 else 1.0)
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def inspect_collector_status(
    raw_data: dict[str, Any] | None = None,
    *,
    state_dir: Path | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """检查 DALTON_USAGE_STATE_DIR/collector-status.json 新鲜度与状态。"""
    embedded = raw_data.get("collector_status") if isinstance(raw_data, dict) else None
    status_path = (state_dir or resolve_state_dir()) / "collector-status.json"

    if isinstance(embedded, dict):
        if embedded.get("fresh") is True and embedded.get("ok") is True and "collected_at" not in embedded:
            return {"present": True, "ok": True, "stale": False, "age_minutes": 0, "error": None, "warning": None}
        payload = embedded
        fallback_mtime: float | None = None
    elif status_path.is_file():
        try:
            loaded = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            return {
                "present": True,
                "ok": False,
                "stale": True,
                "age_minutes": None,
                "error": str(exc),
                "warning": f"⚠️ **本机采集器告警**：状态文件不可读（{exc}），Grok 等需持续采集的来源可能缺失",
            }
        if not isinstance(loaded, dict):
            return {
                "present": True,
                "ok": False,
                "stale": True,
                "age_minutes": None,
                "error": "invalid collector-status payload",
                "warning": "⚠️ **本机采集器告警**：状态文件格式无效，Grok 等需持续采集的来源可能缺失",
            }
        payload = loaded
        fallback_mtime = status_path.stat().st_mtime
    else:
        # No status file: the collector never ran here, or DALTON_USAGE_STATE_DIR points elsewhere.
        # Continuous-only sources (Grok logs rotate within hours) may be missing, so say so.
        return {
            "present": False,
            "ok": None,
            "stale": True,
            "age_minutes": None,
            "error": "collector-status.json missing",
            "warning": "⚠️ **本机采集器告警**：找不到采集器状态文件，无法确认采集器在持续运行，Grok 等需持续采集的来源可能缺失",
        }

    collected_ts = (
        _parse_iso_timestamp(payload.get("collected_at"))
        or _parse_iso_timestamp(payload.get("updated_at"))
        or fallback_mtime
    )
    ref_now = now_ts if now_ts is not None else datetime.datetime.now(datetime.timezone.utc).timestamp()
    age_seconds = max(0.0, ref_now - collected_ts) if collected_ts is not None else None
    is_stale = age_seconds is None or age_seconds > COLLECTOR_STALE_SECONDS
    is_ok = payload.get("ok") is True
    err = str(payload.get("error") or "").strip() or None
    age_minutes = max(1, int(age_seconds // 60)) if age_seconds is not None else None

    warning = None
    if is_stale and age_minutes is not None:
        warning = f"⚠️ **本机采集器告警**：本机采集器 {age_minutes} 分钟未更新，Grok 等需持续采集的来源可能缺失"
        if not is_ok and err:
            warning += f"（状态异常：{err}）"
    elif is_stale or not is_ok:
        detail = err or ("缺少更新时间" if collected_ts is None else "状态非 ok")
        warning = f"⚠️ **本机采集器告警**：本机采集器状态异常（{detail}），Grok 等需持续采集的来源可能缺失"

    return {
        "present": True,
        "ok": is_ok,
        "stale": is_stale,
        "age_minutes": age_minutes,
        "error": err,
        "warning": warning,
    }


def _issue_label(code: str) -> str:
    if code in _ISSUE_LABELS:
        return _ISSUE_LABELS[code]
    if "unreadable" in code or "schema_error" in code:
        return f"来源不可读 ({code})"
    if "mismatch" in code or "conflict" in code or "unreconciled" in code:
        return f"冲突未对账 ({code})"
    if "unresolved" in code or "unknown" in code or "unavailable" in code:
        return f"缺省/待识别 ({code})"
    return code


def _format_coverage_issues(coverage: dict[str, Any]) -> str | None:
    raw_issues = coverage.get("issues")
    if not isinstance(raw_issues, dict):
        return None
    items = [
        (str(k), int(v))
        for k, v in raw_issues.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool) and int(v) > 0
    ]
    if not items:
        return None
    items.sort(key=lambda pair: (-pair[1], pair[0]))
    shown = [f"{_issue_label(code)} {count:,} 项" for code, count in items[:8]]
    extra = f" 等共 {len(items)} 类" if len(items) > 8 else ""
    return "· 覆盖异常（coverage.issues）：" + "；".join(shown) + extra + "。"


def _format_effective_notes(price_catalog: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    for raw in price_catalog.get("effective_notes") or []:
        text = str(raw).strip()
        if text:
            notes.append(text)
    if not notes:
        notes.append("gemini-4-argon / gemini-4-carbon：2026-11-30 前适用推出价，2026-12-01 起按翻倍正式价估算。")
    return notes


def build_card(stats: dict[str, Any]) -> dict[str, Any]:
    """构造飞书 Card Schema 2.0 卡片。"""
    try:
        from_dt = datetime.datetime.fromisoformat(stats["from_iso"]).astimezone(ZoneInfo("Asia/Hong_Kong"))
        to_dt = datetime.datetime.fromisoformat(stats["to_iso"]).astimezone(ZoneInfo("Asia/Hong_Kong"))
        date_range_str = f"{from_dt.strftime('%m-%d %H:%M')} 至 {to_dt.strftime('%m-%d %H:%M')} 香港时间"
    except Exception:
        date_range_str = "过去 7 天"

    total_tokens_str = format_tokens(stats["total_tokens"])
    total_cost_str = format_usd(stats["total_cost"])
    hit_rate_str = f"{stats['cache_hit_rate']:.1f}%"

    header = {
        "title": {
            "tag": "plain_text",
            "content": "每周 LLM Token 用量与参考成本周报" + (" · 修订版" if stats.get("revision") else ""),
        },
        "subtitle": {
            "tag": "plain_text",
            "content": f"{date_range_str} · {stats['days']:g}D · Dalton 自有价格表估算",
        },
        "template": "indigo",
        "text_tag_list": [
            {
                "tag": "text_tag",
                "text": {"tag": "plain_text", "content": "周度 Tracking"},
                "color": "blue",
            },
            {
                "tag": "text_tag",
                "text": {"tag": "plain_text", "content": "全口径大盘"},
                "color": "purple",
            },
        ],
    }

    elements: list[dict[str, Any]] = []

    if stats.get("revision"):
        elements.append({
            "tag": "markdown",
            "content": "**同窗口修订版**：已校准模型归属、跨来源重复与价格生效时段，本卡替代同窗口早先周报。",
        })

    collector_status = stats.get("collector_status") or {}
    if collector_status.get("warning"):
        elements.append({
            "tag": "markdown",
            "content": collector_status["warning"],
        })

    # 1. 核心看板区：拆分「本机命令行工具」与「Dalton 生产」后再给合计
    local_s = stats["local_stats"]
    dalton_s = stats["dalton_stats"]
    dalton_prod = stats.get("dalton_prod") or {"ok": True}

    if dalton_prod.get("ok") is False:
        err_reason = str(dalton_prod.get("error") or "未知错误")
        dalton_line = (
            f"· **Dalton 生产**：⚠️ **Dalton 生产用量读取失败：{err_reason}**"
            "（未将 Dalton 生产计为 0，下方合计仅含本机命令行工具）"
        )
    else:
        dalton_unpriced_note = (
            f"，含 {format_tokens(dalton_s['unpriced_tokens'])} 无公开价"
            if dalton_s["unpriced_tokens"]
            else ""
        )
        dalton_line = (
            f"· **Dalton 生产**：**{format_tokens(dalton_s['tokens'])}** Tokens ｜ "
            f"**{format_usd(dalton_s['cost'])}**{dalton_unpriced_note} ｜ "
            f"{dalton_s['runs']:,} 次运行 / {dalton_s['api_calls']:,} 次调用"
        )

    local_unpriced_note = (
        f"，含 {format_tokens(local_s['unpriced_tokens'])} 无公开价"
        if local_s["unpriced_tokens"]
        else ""
    )
    local_line = (
        f"· **本机命令行工具**：**{format_tokens(local_s['tokens'])}** Tokens ｜ "
        f"**{format_usd(local_s['cost'])}**{local_unpriced_note} ｜ "
        f"{local_s['records']:,} 条计量记录"
    )
    combined_note = "（仅本机可用）" if dalton_prod.get("ok") is False else ""
    total_line = (
        f"· **合计{combined_note}**：**{total_tokens_str}** Tokens ｜ "
        f"已定价参考成本 **{total_cost_str}**"
    )

    overview_text = (
        f"过去 {stats['days']:g} 天总览：\n"
        f"{local_line}\n"
        f"{dalton_line}\n"
        f"{total_line}\n\n"
        f"**用量构成**\n"
        f"· **Prompt 缓存读取占比**: **{hit_rate_str}**（按 Token 加权）\n"
        f"· **输入构成 (Prompt)**: **{format_tokens(stats['total_prompt'])}** "
        f"（缓存读取 {format_tokens(stats['total_cr'])} ｜ 新增 {format_tokens(stats['total_fresh'])} ｜ 写入 {format_tokens(stats['total_cw'])}）\n"
        f"· **输出（含正文与推理）**: **{format_tokens(stats['total_out'])}**"
    )
    if stats["unpriced_tokens"]:
        overview_text += f"\n· **无公开价用量**: **{format_tokens(stats['unpriced_tokens'])}** Tokens，已计入总用量，不计入总成本。"
    elements.append({"tag": "markdown", "content": overview_text})
    elements.append({"tag": "hr"})

    # 2. 供应商成本分布
    prov_lines = []
    for p in stats["prov_list"]:
        cost_str = format_usd(p["cost"])
        if p["unpriced_tokens"] == p["tokens"]:
            cost_str = "无公开价"
        elif p["unpriced_tokens"]:
            cost_str += f"（另有 {format_tokens(p['unpriced_tokens'])} Tokens 无公开价）"
        tok_str = format_tokens(p["tokens"])
        cost_share = f" ({p['cost_pct']:.1f}%)" if p["unpriced_tokens"] == 0 else ""
        prov_lines.append(
            f"· **{p['provider']}**: **{cost_str}**{cost_share} ｜ {tok_str} Tokens ({p['tokens_pct']:.1f}%) ｜ {p['calls']:,} 次调用"
        )
    prov_text = "**模型厂商参考成本与用量**\n" + ("\n".join(prov_lines) if prov_lines else "· 暂无记录")
    elements.append({"tag": "markdown", "content": prov_text})
    elements.append({"tag": "hr"})

    # 3. 前 10 Token 量模型榜单
    elements.append({
        "tag": "markdown",
        "text_size": "notation",
        "content": "<font color='grey'>核心榜单 · 过去一周 Token 消耗量前 10 大模型</font>",
    })

    for r in stats["top10"]:
        rank = r["rank"]
        model = r["model"]
        prov = r["norm_provider"]
        tok_str = format_tokens(r["total_tokens"])
        tok_pct = r["token_pct"]
        cost_val = r["cost_usd"]
        cost_pct = r["cost_pct"]
        calls = r["calls"]

        fresh_str = format_tokens(r["fresh"])
        cr_str = format_tokens(r["cache_read"])
        out_str = format_tokens(r["output"])

        if cost_val is not None:
            cost_disp = f"{format_usd(cost_val)} ({cost_pct:.1f}%)"
            if r["unpriced_tokens"]:
                cost_disp += f"，另有 {format_tokens(r['unpriced_tokens'])} Tokens 无公开价"
        else:
            cost_disp = "无公开价"
            if r.get("reference_cost_usd") is not None:
                ref_m = r.get("reference_model") or "参考模型"
                cost_disp += f"（参考价估算 {format_usd(r['reference_cost_usd'])}，按 {ref_m}，仅供参考）"

        item_text = (
            f"**{rank}. {model}** <font color='grey'>({prov})</font> · **{tok_str}** ({tok_pct:.1f}%)\n"
            f"<font color='grey'>· 参考成本: {cost_disp} ｜ 调用: {calls:,} 次</font>\n"
            f"<font color='grey'>· 构成拆分: 缓存读取 {cr_str} ｜ 缓存写入 {format_tokens(r['cache_write'])} ｜ 新增 {fresh_str} ｜ 输出 {out_str}</font>"
        )
        elements.append({"tag": "markdown", "content": item_text})

    # 4. 长尾模型折叠面板
    if stats["tail"]:
        elements.append({"tag": "hr"})
        tail_lines = []
        for idx, r in enumerate(stats["tail"], 11):
            model = r["model"]
            prov = r["norm_provider"]
            tok_str = format_tokens(r["total_tokens"])
            cost_str = format_usd(r["cost_usd"])
            if r["cost_usd"] is None and r.get("reference_cost_usd") is not None:
                cost_str = f"无公开价（参考 {format_usd(r['reference_cost_usd'])}）"
            tail_lines.append(
                f"**{idx}. {model}** <font color='grey'>({prov})</font>: {tok_str} Tokens ｜ {cost_str} ｜ {r['calls']:,} 次调用"
            )

        tail_summary = (
            f"**其余 {len(stats['tail'])} 个长尾模型（点击展开）** "
            f"<font color='grey'>合计 {format_tokens(stats['tail_tokens'])} ｜ {format_usd(stats['tail_cost'])} ｜ {stats['tail_calls']:,} 次调用</font>"
        )
        elements.append({
            "tag": "collapsible_panel",
            "expanded": False,
            "header": {
                "title": {"tag": "markdown", "content": tail_summary},
                "vertical_align": "center",
                "padding": "4px 8px 4px 8px",
                "icon": {
                    "tag": "standard_icon",
                    "token": "down-small-ccm_outlined",
                    "size": "12px 12px",
                },
                "icon_position": "right",
                "icon_expanded_angle": -180,
            },
            "border": {"color": "grey", "corner_radius": "6px"},
            "vertical_spacing": "6px",
            "padding": "8px 8px 8px 8px",
            "elements": [{"tag": "markdown", "content": "\n".join(tail_lines)}],
        })

    # 5. 无公开价模型专栏
    unpriced_models = stats.get("unpriced_models") or []
    elements.append({"tag": "hr"})
    if unpriced_models:
        u_lines = ["**无公开价模型** <font color='grey'>（以下用量已计入总 Tokens，不计入总成本）</font>"]
        for item in unpriced_models:
            line = f"· **{item['model']}** ({item['norm_provider']}): **{format_tokens(item['unpriced_tokens'])}** Tokens · 成本：无公开价"
            if item.get("reference_cost_usd") is not None:
                ref_m = item.get("reference_model") or "相近模型"
                line += f" ｜ 参考价估算：**{format_usd(item['reference_cost_usd'])}**（按 {ref_m}，仅供参考，不计入总成本）"
            u_lines.append(line)
        elements.append({"tag": "markdown", "content": "\n".join(u_lines)})
    else:
        elements.append({"tag": "markdown", "content": "**无公开价模型**：本期无"})

    # 6. 页脚覆盖与价格表说明
    coverage = stats.get("coverage", {})
    price_catalog = stats.get("price_catalog") or {}
    catalog_date = price_catalog.get("generated") or "未知"
    eff_notes = _format_effective_notes(price_catalog)

    notes = [
        "**覆盖与价格表说明**",
        f"· 价格表日期：{catalog_date}（来源：`references/prices.json`）；成本按每条记录的时间选择生效价格估算。",
    ]
    for en in eff_notes:
        notes.append(f"· 价格生效说明：{en}")
    notes.extend([
        "· 范围：本机命令行工具（OpenClaw 与手动 CLI）与 Dalton 生产只读按模型聚合；Dalton 生产直连供应商，不与本机来源去重。",
        "· 记录数包含模型响应、网关请求、运行汇总与模型聚合等不同单位；缓存读取占比按 Token 加权。",
    ])
    if stats["time_estimated_records"]:
        notes.append(f"· {stats['time_estimated_records']:,} 条历史记录仅保留开始时间，按开始时间归档。")
    pending = coverage.get("unresolved_aggregate_records", 0)
    if pending:
        notes.append(
            f"· 待核对：{pending:,} 条汇总、{format_tokens(coverage.get('unresolved_aggregate_tokens_not_additive', 0))} Tokens，可能与已计入用量重叠，未叠加。"
        )
    if stats["tier_context_unknown_records"]:
        notes.append(f"· {stats['tier_context_unknown_records']:,} 条汇总缺少单响应上下文，费用暂按基础档估算。")
    if stats["estimated_split_records"]:
        notes.append(f"· {stats['estimated_split_records']:,} 条历史记录的缓存拆分为估算。")
    issues_line = _format_coverage_issues(coverage)
    if issues_line:
        notes.append(issues_line)
    if collector_status.get("warning"):
        notes.append(f"· {collector_status['warning'].replace('**', '')}。")
    notes.append(f"· 统计器版本：{stats['version']}。")
    elements.extend([{"tag": "hr"}, {"tag": "markdown", "text_size": "notation", "content": "\n".join(notes)}])

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": header,
        "body": {
            "direction": "vertical",
            "vertical_spacing": "10px",
            "elements": elements,
        },
    }


def write_outbox_card(outbox_path: str | Path, card: dict[str, Any]) -> None:
    """将 Feishu Card 2.0 写入 Dalton outbox JSON。"""
    target = Path(outbox_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"feishu_cards": [{"card": card, "images": []}]}
    tmp_path = target.with_name(f".{target.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(target)


def self_test() -> int:
    """运行自检测试（不依赖 ~/.openclaw 或网络）。"""
    mock_data = {
        "version": "3.1.0",
        "price_catalog": {
            "generated": "2026-11-14",
            "effective_notes": [
                "gemini-4-argon (2026-12-01 起): 12-01 起价格翻倍（$4/$20）",
            ],
        },
        "dalton_prod": {"ok": True, "error": None, "runs": 120, "calls": 450, "models": 2},
        "collector_status": {"ok": True, "fresh": True},
        "windows": [
            {
                "days": 7.0,
                "from": "2026-11-07T00:00:00+00:00",
                "to": "2026-11-14T00:00:00+00:00",
                "rows": [
                    {
                        "source": "openclaw",
                        "provider": "openai",
                        "model": "gpt-6-luna",
                        "records": 100,
                        "calls": 100,
                        "fresh": 10000,
                        "cache_write": 2000,
                        "cache_read": 80000,
                        "output": 5000,
                        "cost_usd": 12.5,
                        "estimated_split": False,
                    },
                    {
                        "source": DALTON_PROD_SOURCE,
                        "provider": "google",
                        "model": "gemini-4-argon-high-oauth",
                        "dalton_prod": True,
                        "records": 1,
                        "calls": 450,
                        "runs": 50,
                        "fresh": 500000,
                        "cache_write": 0,
                        "cache_read": 100000,
                        "output": 20000,
                        "cost_usd": 1.21,
                        "estimated_split": False,
                    },
                    {
                        "source": "muse-cli(手动)",
                        "provider": "muse-cli-gateway",
                        "model": "muse-wonder-full-1.0",
                        "records": 10,
                        "calls": 10,
                        "fresh": 1000,
                        "cache_write": 0,
                        "cache_read": 2000,
                        "output": 500,
                        "cost_usd": None,
                        "unpriced_tokens": 3500,
                        "no_public_price": True,
                        "reference_cost_usd": 0.0144,
                        "reference_model": "muse-wonder-1.0",
                        "estimated_split": False,
                    },
                ],
                "totals": {
                    "tokens": 97000 + 620000 + 3500,
                    "records": 111,
                    "calls": 560,
                    "priced_cost_usd": 13.71,
                    "unpriced_tokens": 3500,
                },
            }
        ],
        "notes": ["self-test"],
    }

    stats = process_usage_stats(mock_data)
    assert stats["total_tokens"] == 97000 + 620000 + 3500
    assert abs(stats["local_stats"]["cost"] - 12.5) < 1e-6
    assert abs(stats["dalton_stats"]["cost"] - 1.21) < 1e-6
    assert abs(stats["total_cost"] - 13.71) < 1e-6
    assert stats["unpriced_tokens"] == 3500
    assert len(stats["unpriced_models"]) == 1

    card = build_card(stats)
    card_text = json.dumps(card, ensure_ascii=False)
    assert card["schema"] == "2.0"
    assert "本机命令行工具" in card_text
    assert "Dalton 生产" in card_text
    assert "无公开价模型" in card_text
    assert "参考价估算" in card_text
    assert "2026-11-14" in card_text
    assert "2026-12-01" in card_text

    # 验证 Dalton 生产接口失败时不计为 0 且明确披露失败原因
    failed_data = {
        "version": "3.1.0",
        "price_catalog": {"generated": "2026-11-14", "effective_notes": []},
        "dalton_prod": {"ok": False, "error": "HTTP 503: Service Unavailable", "runs": 0, "calls": 0, "models": 0},
        "windows": [
            {
                "days": 7.0,
                "from": "2026-11-07T00:00:00+00:00",
                "to": "2026-11-14T00:00:00+00:00",
                "rows": [mock_data["windows"][0]["rows"][0]],
            }
        ],
    }
    failed_card_text = json.dumps(build_card(process_usage_stats(failed_data)), ensure_ascii=False)
    assert "Dalton 生产用量读取失败：HTTP 503: Service Unavailable" in failed_card_text

    print("self-test PASSED")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="每周 LLM Token 用量与刊例价成本周报卡片生成")
    parser.add_argument("--days", type=int, default=7, help="统计天数窗口，默认 7 天")
    parser.add_argument("--dry-run", action="store_true", help="仅打印卡片 JSON，不写 outbox 文件")
    parser.add_argument("--self-test", action="store_true", help="运行自检")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出统计指标")
    parser.add_argument("--as-of", help="固定统计截止时间（ISO 8601）")
    parser.add_argument("--input-json", help="使用已验证的计量快照生成报告")
    parser.add_argument("--revision", action="store_true", help="标记同窗口修订版")
    parser.add_argument("--outbox-file", help="指定输出卡片的 outbox JSON 路径（默认读取 DALTON_OUTBOX_FILE）")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    raw_data = (
        json.loads(Path(args.input_json).read_text(encoding="utf-8"))
        if args.input_json
        else fetch_usage_data(days=args.days, as_of=args.as_of)
    )
    stats = process_usage_stats(raw_data)
    stats["revision"] = args.revision
    card = build_card(stats)

    if args.dry_run:
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return 0

    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    outbox_target = (args.outbox_file or os.environ.get("DALTON_OUTBOX_FILE") or "").strip()
    if outbox_target:
        write_outbox_card(outbox_target, card)
        print(
            json.dumps(
                {
                    "ok": True,
                    "outbox_file": outbox_target,
                    "total_tokens": stats["total_tokens"],
                    "total_cost_usd": round(stats["total_cost"], 6),
                    "dalton_prod_ok": stats["dalton_prod"].get("ok"),
                },
                ensure_ascii=False,
            )
        )
    else:
        print(json.dumps(card, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
