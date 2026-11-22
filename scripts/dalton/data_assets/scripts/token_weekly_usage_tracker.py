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
from decimal import Decimal
import importlib.util
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


def resolve_workspace_dir() -> Path:
    raw = (os.environ.get("OPENCLAW_WORKSPACE") or os.environ.get("DALTON_DATA_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent.parent


REQUIRED_USAGE_SOURCES_SERIES_API_VERSION = 2


def _sibling_skill_scripts_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "skills" / "model-usage-cost" / "scripts"


def _skill_scripts_dir() -> Path:
    """Resolve helper scripts dir strictly from the sibling directory matching this script."""
    return _sibling_skill_scripts_dir()


def _load_required_usage_sources(scripts_dir: Path) -> Any:
    """Load sibling ``usage_sources.py`` only and enforce exact series API version match."""
    required = (
        "load_antigravity_series_module",
        "load_antigravity_series_catalog",
        "build_antigravity_series_from_overlays",
        "_parse_antigravity_key",
    )
    target = scripts_dir / "usage_sources.py"
    if not target.is_file():
        raise RuntimeError(
            f"找不到同目录匹配版本的 usage_sources.py（期望路径：{target}）；"
            "本脚本不会回退到其他目录的旧模块，请先运行 scripts/dalton/seed_openclaw_data.sh --apply 同步技能目录，或从完整仓库目录运行。"
        )
    existing = sys.modules.get("usage_sources")
    if existing is not None:
        existing_file = getattr(existing, "__file__", None)
        if existing_file and Path(existing_file).resolve() == target.resolve():
            mod = existing
        else:
            spec = importlib.util.spec_from_file_location("_weekly_tracker_usage_sources", target)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"无法加载 usage_sources.py：{target}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
    else:
        spec = importlib.util.spec_from_file_location("usage_sources", target)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载 usage_sources.py：{target}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["usage_sources"] = mod
        spec.loader.exec_module(mod)
    missing = [attr for attr in required if not hasattr(mod, attr)]
    raw_ver = getattr(mod, "USAGE_SOURCES_SERIES_API_VERSION", 0)
    ver = int(raw_ver) if isinstance(raw_ver, int) and not isinstance(raw_ver, bool) else 0
    if missing or ver < REQUIRED_USAGE_SOURCES_SERIES_API_VERSION:
        details: list[str] = []
        if ver < REQUIRED_USAGE_SOURCES_SERIES_API_VERSION:
            details.append(f"版本为 {ver}，要求 >= {REQUIRED_USAGE_SOURCES_SERIES_API_VERSION}")
        if missing:
            details.append(f"缺少 {', '.join(missing)}")
        raise RuntimeError(
            f"usage_sources.py 版本不匹配（{target}：{'；'.join(details)}）；"
            "请先运行 scripts/dalton/seed_openclaw_data.sh --apply 同步技能目录，或从新版仓库直接运行。"
        )
    return mod


_SKILL_SCRIPTS_DIR = _skill_scripts_dir()
if str(_SKILL_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_SCRIPTS_DIR))
usage_sources = _load_required_usage_sources(_SKILL_SCRIPTS_DIR)
from usage_state import resolve_usage_state_dir  # noqa: E402

_ANTIGRAVITY_SERIES_MOD = usage_sources.load_antigravity_series_module()
REASONING_LEVELS: tuple[str, ...] = tuple(
    getattr(
        _ANTIGRAVITY_SERIES_MOD,
        "REASONING_LEVELS",
        ("none", "minimal", "low", "medium", "high", "xhigh", "max"),
    )
)
UNKNOWN_EFFORT_BUCKET = "unknown"
UNKNOWN_EFFORT_LABEL = "档位未知"
EFFORT_BUCKET_ORDER: tuple[str, ...] = (*REASONING_LEVELS, UNKNOWN_EFFORT_BUCKET)


def resolve_state_dir() -> Path:
    return resolve_usage_state_dir()


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
    if m.startswith("gpt-"):
        return "OpenAI"
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


def resolve_antigravity_series(raw_data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Resolve Antigravity series catalog from snapshot payload, synced JSON, or repo overlays."""
    if isinstance(raw_data, dict):
        embedded = raw_data.get("antigravity_series")
        if isinstance(embedded, list) and embedded:
            return [item for item in embedded if isinstance(item, dict)]
    env_file = (os.environ.get("DALTON_ANTIGRAVITY_SERIES_FILE") or "").strip()
    if env_file:
        return usage_sources.load_antigravity_series_catalog(env_file)
    ws_ref = resolve_workspace_dir() / "skills" / "model-usage-cost" / "references" / "antigravity_series.json"
    if ws_ref.is_file():
        return usage_sources.load_antigravity_series_catalog(ws_ref)
    sibling_ref = _sibling_skill_scripts_dir().parent / "references" / "antigravity_series.json"
    if sibling_ref.is_file():
        return usage_sources.load_antigravity_series_catalog(sibling_ref)
    return usage_sources.load_antigravity_series_catalog()


def _series_report_display_name(entry: dict[str, Any]) -> str:
    prefix = str(entry.get("series_prefix") or "").strip()
    if prefix.startswith("gemini-"):
        return str(entry.get("display_name") or prefix)
    return str(entry.get("qualified_display_name") or entry.get("display_name") or prefix)


def build_series_index(series_list: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Build lookup maps for Antigravity series grouping."""
    by_series_id: dict[str, dict[str, Any]] = {}
    by_variant_model: dict[str, tuple[dict[str, Any], str]] = {}
    by_prefix: dict[str, dict[str, Any]] = {}
    for raw in series_list or []:
        if not isinstance(raw, dict):
            continue
        series_id = str(raw.get("series_id") or "").strip()
        series_prefix = str(raw.get("series_prefix") or "").strip()
        if not series_id or not series_prefix:
            continue
        info = {
            "series_id": series_id,
            "series_prefix": series_prefix,
            "display_name": str(raw.get("display_name") or series_prefix),
            "qualified_display_name": str(raw.get("qualified_display_name") or raw.get("display_name") or series_prefix),
            "report_display_name": _series_report_display_name(raw),
            "variants": dict(raw.get("variants") or {}),
        }
        by_series_id[series_id] = info
        by_prefix[series_prefix] = info
        for effort, variant_key in info["variants"].items():
            if isinstance(variant_key, str) and variant_key.strip():
                eff_str = str(effort) if str(effort) in REASONING_LEVELS else UNKNOWN_EFFORT_BUCKET
                by_variant_model[variant_key.strip()] = (info, eff_str)
    return {
        "by_series_id": by_series_id,
        "by_variant_model": by_variant_model,
        "by_prefix": by_prefix,
    }


def _normalize_effort_bucket(effort: Any) -> str:
    if isinstance(effort, str):
        low = effort.strip().lower()
        if low in REASONING_LEVELS:
            return low
    return UNKNOWN_EFFORT_BUCKET


def _is_local_antigravity_row(row: dict[str, Any]) -> bool:
    provider = str(row.get("provider") or "").lower()
    providers = [str(p).lower() for p in (row.get("providers") or [])]
    source = str(row.get("source") or "").lower()
    sources_list = [str(s).lower() for s in (row.get("sources") or [])]
    if "antigravity" in provider or any("antigravity" in p for p in providers):
        return True
    if "agy" in source or any("agy" in s for s in sources_list):
        return True
    return False


def _match_antigravity_series(
    row: dict[str, Any],
    series_index: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(series_info, effort_bucket)`` if ``row`` belongs to an Antigravity series."""
    by_series_id: dict[str, dict[str, Any]] = series_index.get("by_series_id") or {}
    by_variant_model: dict[str, tuple[dict[str, Any], str]] = series_index.get("by_variant_model") or {}
    by_prefix: dict[str, dict[str, Any]] = series_index.get("by_prefix") or {}

    existing_sid = row.get("series_id")
    if isinstance(existing_sid, str) and existing_sid.strip():
        sid = existing_sid.strip()
        info = by_series_id.get(sid)
        if info is None:
            info = {
                "series_id": sid,
                "series_prefix": sid.split(":", 1)[-1],
                "provider": "antigravity-oauth",
                "report_display_name": str(row.get("model") or sid),
            }
        return info, None

    model = str(row.get("model") or "").strip()
    if not model:
        return None, None

    # 1. Exact Antigravity variant match (Dalton prod *-oauth suffixed rows)
    hit = by_variant_model.get(model)
    if hit is not None:
        return hit[0], hit[1]

    # 2. Suffixed *-oauth variant of a known Antigravity series (e.g. retired effort);
    # unsuffixed OAuth models such as Anthropic's `claude-opus-5-5-oauth` have effort=None
    # and MUST stay independent.
    if model.endswith("-oauth"):
        parsed = usage_sources._parse_antigravity_key(model)
        if parsed is not None and parsed[1] is not None:
            prefix, eff = parsed
            info = by_prefix.get(prefix)
            if info is not None:
                return info, _normalize_effort_bucket(eff)
        return None, None

    # 3. Local antigravity-cli-gateway / agy rows (already stripped to base prefix or with effort)
    if _is_local_antigravity_row(row):
        row_effort = row.get("effort")
        info = by_prefix.get(model)
        if info is not None:
            return info, _normalize_effort_bucket(row_effort)
        parsed = usage_sources._parse_antigravity_key(f"{model}-oauth")
        if parsed is not None:
            prefix, parsed_eff = parsed
            info = by_prefix.get(prefix)
            if info is not None:
                return info, _normalize_effort_bucket(row_effort or parsed_eff)
    return None, None


def _series_norm_provider(series_info: dict[str, Any]) -> str:
    """Unify display vendor for a recognized Antigravity series regardless of local/prod source."""
    return normalize_provider(
        str(series_info.get("provider") or "antigravity-oauth"),
        str(series_info.get("series_prefix") or ""),
    )


def _empty_effort_bucket(bucket_name: str) -> dict[str, Any]:
    return {
        "effort": bucket_name,
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
        "_cost_values": [],
        "unpriced_tokens": 0,
        "no_public_price": False,
        "reference_cost_usd": None,
        "_ref_cost_values": [],
        "reference_model": None,
        "price_note": None,
    }


def _accumulate_bucket_fields(
    bucket: dict[str, Any],
    src: dict[str, Any],
    *,
    records: int,
    total_tokens: int,
    prompt_tokens: int,
    unpriced_tokens: int,
) -> None:
    bucket["records"] += records
    bucket["calls"] += int(src.get("calls") or 0)
    bucket["runs"] += int(src.get("runs") or 0)
    bucket["fresh"] += int(src.get("fresh") or 0)
    bucket["cache_write"] += int(src.get("cache_write") or 0)
    bucket["cache_read"] += int(src.get("cache_read") or 0)
    bucket["output"] += int(src.get("output") or 0)
    bucket["total_tokens"] += total_tokens
    bucket["prompt_tokens"] += prompt_tokens
    bucket["unpriced_tokens"] += unpriced_tokens
    if src.get("cost_usd") is not None:
        bucket["_cost_values"].append(float(src["cost_usd"]))
    if src.get("no_public_price"):
        bucket["no_public_price"] = True
    if src.get("reference_cost_usd") is not None:
        bucket["_ref_cost_values"].append(float(src["reference_cost_usd"]))
    if src.get("reference_model") and not bucket["reference_model"]:
        bucket["reference_model"] = src["reference_model"]
    if src.get("price_note") and not bucket["price_note"]:
        bucket["price_note"] = src["price_note"]


def _finalize_bucket_costs(bucket: dict[str, Any]) -> dict[str, Any]:
    costs = bucket.pop("_cost_values", [])
    ref_costs = bucket.pop("_ref_cost_values", [])
    bucket["cost_usd"] = sum(costs) if costs else None
    bucket["reference_cost_usd"] = sum(ref_costs) if ref_costs else None
    return bucket


def _order_effort_breakdown(breakdown: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    ordered: dict[str, dict[str, Any]] = {}
    for key in EFFORT_BUCKET_ORDER:
        if key in breakdown:
            ordered[key] = _finalize_bucket_costs(breakdown[key])
    for key in sorted(breakdown):
        if key not in ordered:
            ordered[key] = _finalize_bucket_costs(breakdown[key])
    return ordered


def _legacy_merge_rows_by_model(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """原版按 `(norm_provider, model)` 合并路径，仅用于产出与旧版完全一致的合计数字。"""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["norm_provider"], r["model"])
        m = merged.get(key)
        if m is None:
            m = merged[key] = {
                "norm_provider": r["norm_provider"],
                "model": r["model"],
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
            }
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
    return list(merged.values())


def merge_rows_by_model(
    rows: list[dict[str, Any]],
    series_index: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """同一厂商同一模型合并为一行；Antigravity 系列按稳定 series_id 幂等合并并保留档位明细。"""
    if series_index is None:
        resolved_index = build_series_index(resolve_antigravity_series())
    elif isinstance(series_index, list):
        resolved_index = build_series_index(series_index)
    else:
        resolved_index = series_index

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        fresh = int(r.get("fresh") or 0)
        cw = int(r.get("cache_write") or 0)
        cr = int(r.get("cache_read") or 0)
        out = int(r.get("output") or 0)
        total_tokens = int(r["total_tokens"]) if "total_tokens" in r else (fresh + cw + cr + out)
        prompt_tokens = int(r["prompt_tokens"]) if "prompt_tokens" in r else (fresh + cw + cr)
        unpriced_tokens = int(
            r.get("unpriced_tokens", total_tokens if r.get("cost_usd") is None else 0)
        )
        rec_count = _row_record_count(r)

        series_info, matched_effort = _match_antigravity_series(r, resolved_index)
        if series_info is not None:
            norm_provider = _series_norm_provider(series_info)
            key = (norm_provider, series_info["series_id"])
            display_model = series_info["report_display_name"]
        else:
            norm_provider = r.get("norm_provider") or normalize_provider(
                str(r.get("provider") or ""), str(r.get("model") or "")
            )
            key = (norm_provider, str(r["model"]))
            display_model = str(r["model"])

        m = merged.get(key)
        if m is None:
            m = merged[key] = {
                "norm_provider": norm_provider,
                "model": display_model,
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
                "_cost_values": [],
                "unpriced_tokens": 0,
                "estimated_split_records": 0,
                "units": {},
                "no_public_price": False,
                "reference_cost_usd": None,
                "_ref_cost_values": [],
                "reference_model": None,
                "price_note": None,
            }
            if series_info is not None:
                m["series_id"] = series_info["series_id"]
                m["effort_breakdown"] = {}

        row_providers = (
            r["providers"]
            if isinstance(r.get("providers"), list) and r["providers"]
            else ([r["provider"]] if r.get("provider") else [])
        )
        for value in row_providers:
            if value and value not in m["providers"]:
                m["providers"].append(value)
        row_sources = (
            r["sources"]
            if isinstance(r.get("sources"), list) and r["sources"]
            else ([r["source"]] if r.get("source") else [])
        )
        for value in row_sources:
            if value and value not in m["sources"]:
                m["sources"].append(value)

        m["records"] += rec_count
        m["calls"] += int(r.get("calls") or 0)
        m["runs"] += int(r.get("runs") or 0)
        m["fresh"] += fresh
        m["cache_write"] += cw
        m["cache_read"] += cr
        m["output"] += out
        m["total_tokens"] += total_tokens
        m["prompt_tokens"] += prompt_tokens
        m["estimated_split_records"] += int(r.get("estimated_split_records") or 0)
        m["unpriced_tokens"] += unpriced_tokens
        if r.get("cost_usd") is not None:
            m["_cost_values"].append(float(r["cost_usd"]))
        if r.get("no_public_price"):
            m["no_public_price"] = True
        if r.get("reference_cost_usd") is not None:
            m["_ref_cost_values"].append(float(r["reference_cost_usd"]))
        if r.get("reference_model") and not m["reference_model"]:
            m["reference_model"] = r["reference_model"]
        if r.get("price_note") and not m["price_note"]:
            m["price_note"] = r["price_note"]
        for unit, count in (r.get("units") or {}).items():
            m["units"][unit] = m["units"].get(unit, 0) + count

        if series_info is not None:
            existing_bd = r.get("effort_breakdown")
            if isinstance(existing_bd, dict) and existing_bd:
                for raw_bk, raw_bv in existing_bd.items():
                    if not isinstance(raw_bv, dict):
                        continue
                    bk = _normalize_effort_bucket(raw_bk)
                    bucket = m["effort_breakdown"].setdefault(bk, _empty_effort_bucket(bk))
                    b_fresh = int(raw_bv.get("fresh") or 0)
                    b_cw = int(raw_bv.get("cache_write") or 0)
                    b_cr = int(raw_bv.get("cache_read") or 0)
                    b_out = int(raw_bv.get("output") or 0)
                    b_total = int(raw_bv["total_tokens"]) if "total_tokens" in raw_bv else (b_fresh + b_cw + b_cr + b_out)
                    b_prompt = int(raw_bv["prompt_tokens"]) if "prompt_tokens" in raw_bv else (b_fresh + b_cw + b_cr)
                    b_unpriced = int(
                        raw_bv.get("unpriced_tokens", b_total if raw_bv.get("cost_usd") is None else 0)
                    )
                    _accumulate_bucket_fields(
                        bucket,
                        raw_bv,
                        records=int(raw_bv.get("records") or 0),
                        total_tokens=b_total,
                        prompt_tokens=b_prompt,
                        unpriced_tokens=b_unpriced,
                    )
            else:
                bk = _normalize_effort_bucket(matched_effort if matched_effort is not None else r.get("effort"))
                bucket = m["effort_breakdown"].setdefault(bk, _empty_effort_bucket(bk))
                _accumulate_bucket_fields(
                    bucket,
                    r,
                    records=rec_count,
                    total_tokens=total_tokens,
                    prompt_tokens=prompt_tokens,
                    unpriced_tokens=unpriced_tokens,
                )

    for m in merged.values():
        m["provider"] = "+".join(m["providers"])
        m["source"] = "|".join(m["sources"])
        costs = m.pop("_cost_values", [])
        ref_costs = m.pop("_ref_cost_values", [])
        m["cost_usd"] = sum(costs) if costs else None
        m["reference_cost_usd"] = sum(ref_costs) if ref_costs else None
        if "effort_breakdown" in m:
            m["effort_breakdown"] = _order_effort_breakdown(m["effort_breakdown"])
    return list(merged.values())


def fetch_usage_data(days: int = 7, as_of: str | None = None) -> dict[str, Any]:
    """通过同目录 model-usage-cost skill 获取过去 N 天的汇总数据。"""
    sibling_dir = _sibling_skill_scripts_dir()
    cli_py = sibling_dir / "model_usage_cost_cli.py"
    run_sh = sibling_dir / "run.sh"

    if cli_py.is_file():
        cmd = [sys.executable, str(cli_py), "--json", "summary", "--days", str(days), "--by-model"]
    elif run_sh.is_file():
        cmd = ["bash", str(run_sh), "--json", "summary", "--days", str(days), "--by-model"]
    else:
        raise FileNotFoundError(f"找不到同目录的 model-usage-cost 统计脚本：{sibling_dir}")

    if as_of:
        cmd += ["--as-of", as_of]
    state_dir = resolve_state_dir()
    child_env = dict(os.environ)
    child_env["DALTON_USAGE_STATE_DIR"] = str(state_dir)
    res = subprocess.run(cmd, capture_output=True, text=True, check=True, env=child_env)
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
    raw_rows = [dict(r) for r in window["rows"]]

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

    # 所有合计字段严格沿用旧版未按系列合并的累加路径，保证与旧版逐字段完全相等；
    # 系列合并与档位明细仅用于展示分组，不回写合计。
    legacy_rows = _legacy_merge_rows_by_model(raw_rows)
    total_fresh = sum(r["fresh"] for r in legacy_rows)
    total_cw = sum(r["cache_write"] for r in legacy_rows)
    total_cr = sum(r["cache_read"] for r in legacy_rows)
    total_out = sum(r["output"] for r in legacy_rows)
    total_tokens = total_fresh + total_cw + total_cr + total_out
    total_prompt = total_fresh + total_cw + total_cr
    total_records = sum(r["records"] for r in legacy_rows)
    total_calls = sum(r["calls"] for r in legacy_rows)
    total_cost = sum((r["cost_usd"] or 0.0) for r in legacy_rows)
    unpriced_tokens = sum(r["unpriced_tokens"] for r in legacy_rows)
    estimated_split_records = sum(r["estimated_split_records"] for r in legacy_rows)
    cache_hit_rate = (total_cr / total_prompt * 100) if total_prompt > 0 else 0.0

    expected = window.get("totals")
    if expected:
        if total_tokens != expected["tokens"] or total_records != expected["records"]:
            raise ValueError("周报聚合与计量总表不一致")
        if expected.get("calls") is not None and total_calls != int(expected["calls"]):
            raise ValueError("周报调用次数与计量总表不一致")
        if abs(total_cost - expected["priced_cost_usd"]) > 0.0001 or unpriced_tokens != expected["unpriced_tokens"]:
            raise ValueError("周报价格聚合与计量总表不一致")

    series_index = build_series_index(resolve_antigravity_series(raw_data))
    rows = merge_rows_by_model(raw_rows, series_index)
    rows_by_tokens = sorted(rows, key=lambda x: -x["total_tokens"])

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


def _displayed_cents(val: float) -> int:
    """Return the exact integer cents rendered by ``format_usd(val)``."""
    rendered = format_usd(val).lstrip("$").replace(",", "")
    return int((Decimal(rendered) * 100).to_integral_exact())


def _allocate_display_cents(
    target_usd: float,
    ordered_buckets: list[tuple[str, dict[str, Any]]],
    rank_map: dict[str, int],
    *,
    cost_field: str = "cost_usd",
    require_unpriced: bool = False,
) -> dict[str, int]:
    """Allocate the series row's displayed cents across effort buckets so their sum equals the series row."""
    priced = [
        (
            bk,
            Decimal(str(float(bv[cost_field]))) * 100,
            int(bv.get("total_tokens") or 0),
            rank_map.get(bk, len(EFFORT_BUCKET_ORDER)),
        )
        for bk, bv in ordered_buckets
        if isinstance(bv, dict)
        and bv.get(cost_field) is not None
        and (not require_unpriced or bv.get("cost_usd") is None)
    ]
    if not priced:
        return {}
    target_cents = _displayed_cents(target_usd)
    floors = {bk: max(0, int(raw_c)) for bk, raw_c, _, _ in priced}
    diff = target_cents - sum(floors.values())
    if diff > 0:
        order = sorted(priced, key=lambda it: (-(it[1] - floors[it[0]]), -it[1], -it[2], it[3]))
        for idx in range(diff):
            floors[order[idx % len(order)][0]] += 1
    elif diff < 0:
        need = -diff
        while need > 0:
            pos = [it for it in priced if floors[it[0]] > 0]
            if not pos:
                break
            order = sorted(pos, key=lambda it: ((it[1] - floors[it[0]]), it[1], it[2], -it[3]))
            for it in order:
                if need <= 0:
                    break
                floors[it[0]] -= 1
                need -= 1
    return floors


def _format_effort_breakdown_line(
    effort_breakdown: dict[str, Any] | None,
    *,
    series_cost_usd: float | None = None,
    series_reference_cost_usd: float | None = None,
) -> str | None:
    """Format per-effort breakdown for Antigravity series rows on the Feishu card."""
    if not isinstance(effort_breakdown, dict) or not effort_breakdown:
        return None
    rank_map = {lvl: idx for idx, lvl in enumerate(EFFORT_BUCKET_ORDER)}

    def _sort_key(item: tuple[str, Any]) -> tuple[int, int, int]:
        bk, bv = item
        is_unknown = 1 if bk == UNKNOWN_EFFORT_BUCKET else 0
        toks = int(bv.get("total_tokens") or 0) if isinstance(bv, dict) else 0
        return (is_unknown, -toks, rank_map.get(bk, len(EFFORT_BUCKET_ORDER)))

    ordered_buckets = sorted(effort_breakdown.items(), key=_sort_key)
    allocated_cents = (
        _allocate_display_cents(series_cost_usd, ordered_buckets, rank_map)
        if series_cost_usd is not None
        else {}
    )
    ref_target_usd = series_reference_cost_usd if series_cost_usd is None else None
    if ref_target_usd is None:
        unpriced_refs = [
            float(bv["reference_cost_usd"])
            for _, bv in ordered_buckets
            if isinstance(bv, dict)
            and bv.get("cost_usd") is None
            and bv.get("reference_cost_usd") is not None
        ]
        if unpriced_refs:
            ref_target_usd = sum(unpriced_refs)
    allocated_ref_cents = (
        _allocate_display_cents(
            ref_target_usd,
            ordered_buckets,
            rank_map,
            cost_field="reference_cost_usd",
            require_unpriced=True,
        )
        if ref_target_usd is not None
        else {}
    )

    parts: list[str] = []
    for bk, bv in ordered_buckets:
        if not isinstance(bv, dict):
            continue
        label = UNKNOWN_EFFORT_LABEL if bk == UNKNOWN_EFFORT_BUCKET else bk
        tok_str = format_tokens(int(bv.get("total_tokens") or 0))
        calls = int(bv.get("calls") or 0)
        cost_val = bv.get("cost_usd")
        unpriced = int(bv.get("unpriced_tokens") or 0)
        ref_cost = bv.get("reference_cost_usd")
        if cost_val is not None:
            if bk in allocated_cents:
                cost_part = f"${Decimal(allocated_cents[bk]) / Decimal(100):,.2f}"
            else:
                cost_part = format_usd(float(cost_val))
            if unpriced > 0:
                cost_part += f"，另 {format_tokens(unpriced)} 无公开价"
        elif ref_cost is not None:
            if bk in allocated_ref_cents:
                ref_part = f"${Decimal(allocated_ref_cents[bk]) / Decimal(100):,.2f}"
            else:
                ref_part = format_usd(float(ref_cost))
            cost_part = f"无公开价，参考 {ref_part}"
        else:
            cost_part = "无公开价"
        parts.append(f"{label} {tok_str} ({calls:,} 次, {cost_part})")
    if not parts:
        return None
    return "· 档位明细: " + " ｜ ".join(parts)


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
        effort_line = _format_effort_breakdown_line(
            r.get("effort_breakdown"),
            series_cost_usd=cost_val,
            series_reference_cost_usd=r.get("reference_cost_usd"),
        )
        if effort_line:
            item_text += f"\n<font color='grey'>{effort_line}</font>"
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
            tail_line = (
                f"**{idx}. {model}** <font color='grey'>({prov})</font>: {tok_str} Tokens ｜ {cost_str} ｜ {r['calls']:,} 次调用"
            )
            effort_line = _format_effort_breakdown_line(
                r.get("effort_breakdown"),
                series_cost_usd=r["cost_usd"],
                series_reference_cost_usd=r.get("reference_cost_usd"),
            )
            if effort_line:
                tail_line += f"\n<font color='grey'>  {effort_line}</font>"
            tail_lines.append(tail_line)

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


def _split_card_chunks(card: dict[str, Any]) -> list[dict[str, Any]]:
    import importlib.util

    send_card_path = Path(__file__).resolve().with_name("feishu_send_card.py")
    if send_card_path.is_file():
        spec = importlib.util.spec_from_file_location("_dalton_feishu_send_card", send_card_path)
        if spec is not None and spec.loader is not None:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            splitter = getattr(mod, "split_feishu_card_to_chunks", None)
            if callable(splitter):
                chunks = splitter(card)
                if isinstance(chunks, list) and chunks:
                    return chunks
    return [card]


def write_outbox_card(outbox_path: str | Path, card: dict[str, Any]) -> None:
    """将 Feishu Card 2.0 写入 Dalton outbox JSON，超过网关上限时自动拆卡。"""
    target = Path(outbox_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    cards = _split_card_chunks(card)
    payload = {"feishu_cards": [{"card": c, "images": []} for c in cards]}
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
