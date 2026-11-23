"""System-generated Dalton Data Map builder and daily PostgreSQL cache coordinator.

Multi-worker rule:
- Never store health or rendered data map state in module-level variables across turns.
- Durable truth lives in PostgreSQL (`dalton_data_source_health` and `dalton_data_map_scoped_cache`).
- A `ContextVar` carries the turn's pre-fetched markdown from async graph construction into
  synchronous Jinja2 rendering within the same turn; when absent, on cache miss, or when
  PostgreSQL is unreachable/slow, rendering falls back to the static `"未体检"` data map
  from `sources.yaml` without rebuilding or writing to PostgreSQL on the turn path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import contextvars
from datetime import date, datetime
import logging
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from src.dalton.capabilities import (
    CapabilityId,
    capabilities_fingerprint as compute_capabilities_fingerprint,
    effective_capabilities,
    has_capability,
    is_bridge_route_enabled,
)
from src.dalton.database import data_map as db_data_map

logger = logging.getLogger(__name__)

SOURCES_YAML_PATH: Path = Path(__file__).resolve().parent / "sources.yaml"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
MAX_DATA_MAP_TOKENS = 1500
CACHE_READ_TIMEOUT_S = 2.0

_PAREN_NOTE_RE = re.compile(r"(\([^()\n]*\)|（[^（）\n]*）)")

# Per-turn execution context only (never cross-request module cache).
_TURN_DATA_MAP_VAR: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "dalton_turn_data_map", default=None
)


def today_cache_date() -> date:
    """Return today's calendar date in `Asia/Shanghai` for daily data map caching."""
    return datetime.now(SHANGHAI_TZ).date()


def load_sources_spec(path: Path | None = None) -> dict[str, Any]:
    """Load `sources.yaml` from disk."""
    target = path or SOURCES_YAML_PATH
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    return dict(raw) if isinstance(raw, Mapping) else {}


def estimate_data_map_tokens(markdown: str) -> int:
    """Estimate token count of `markdown` using `tiktoken` (`o200k_base`)."""
    try:
        import tiktoken

        return len(tiktoken.get_encoding("o200k_base").encode(markdown))
    except Exception:
        return max(1, len(markdown) // 2)


def _is_x_search_enabled(
    search_api: str | None = None,
    x_search_enabled: bool | None = None,
) -> bool:
    """Whether this turn mounts ``x_search``."""
    _ = search_api
    if x_search_enabled is not None:
        return bool(x_search_enabled)
    return False


def _apply_search_api_filter(
    markdown: str,
    search_api: str | None,
    x_search_enabled: bool | None = None,
) -> str:
    """Drop the ``x_search`` line when that tool is not mounted for the turn."""
    if _is_x_search_enabled(search_api, x_search_enabled):
        return markdown
    return "\n".join(
        line
        for line in markdown.splitlines()
        if not line.lstrip().startswith("- `x_search`")
    )


_TEAM_MEMORY_PRINCIPLE_CLAUSE = (
    "；`conversation_search` 仅在团队记忆开启时可用；当前是否开启以对话中的运行时说明为准"
    "——开启时问到公司或主题，先用 `conversation_search` 查团队以前的观点、假设和决定，"
    "并在答案里说明与之前看法相比有什么变化"
)


def _apply_team_memory_filter(markdown: str, team_memory_enabled: bool) -> str:
    """Filter `conversation_search` and its archive stats line when Team Memory is disabled for the turn."""
    if team_memory_enabled:
        return markdown
    filtered_lines: list[str] = []
    for line in markdown.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("- `conversation_search`") or stripped.startswith("- 归档统计："):
            continue
        if _TEAM_MEMORY_PRINCIPLE_CLAUSE in line:
            line = line.replace(_TEAM_MEMORY_PRINCIPLE_CLAUSE, "")
        filtered_lines.append(line)
    return "\n".join(filtered_lines)


def format_health_status(record: Mapping[str, Any] | None) -> str:
    """Format a source's health record into `可用` / `不可用（原因）` / `未体检`."""
    if not isinstance(record, Mapping):
        return "未体检"
    status = str(record.get("status") or "unprobed").strip().lower()
    if status == "available":
        return "可用"
    if status == "unavailable":
        reason = str(
            record.get("error_type")
            or (record.get("detail") or {}).get("reason")
            if isinstance(record.get("detail"), Mapping)
            else record.get("error_type") or ""
        ).strip()
        if not reason:
            reason = "调用失败"
        return f"不可用({reason[:32]})"
    unprobed_reason = str(
        (record.get("detail") or {}).get("reason")
        if isinstance(record.get("detail"), Mapping)
        else ""
    ).strip() or str(record.get("error_type") or "").strip()
    if unprobed_reason:
        return f"未体检({unprobed_reason[:32]})"
    return "未体检"


def _normalize_rules_text(rules: Any) -> str:
    if isinstance(rules, str):
        return "；".join(part.strip() for part in rules.splitlines() if part.strip())
    if isinstance(rules, Sequence) and not isinstance(rules, (bytes, bytearray)):
        parts = [str(item).strip() for item in rules if str(item).strip()]
        return "；".join(parts)
    return ""


def _parse_capability_requirement(raw: Any) -> tuple[CapabilityId, ...]:
    """Normalize a YAML ``capability`` / ``note_capability`` field to a tuple of ``CapabilityId``."""
    if raw is None:
        return ()
    if isinstance(raw, CapabilityId):
        return (raw,)
    if isinstance(raw, str):
        cleaned = raw.strip()
        return (CapabilityId(cleaned),) if cleaned else ()
    if isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        out: list[CapabilityId] = []
        for item in raw:
            if isinstance(item, CapabilityId):
                out.append(item)
            elif isinstance(item, str) and item.strip():
                out.append(CapabilityId(item.strip()))
        return tuple(out)
    return ()


def _capabilities_satisfied(
    raw_requirement: Any,
    active_caps: frozenset[CapabilityId],
) -> bool:
    req = _parse_capability_requirement(raw_requirement)
    return all(cap in active_caps for cap in req)


def _resolve_unavailable_source_note(
    item: Mapping[str, Any],
    active_caps: frozenset[CapabilityId],
) -> str:
    """Resolve an ``unavailable_sources`` note according to enabled capabilities."""
    note_sources = item.get("note_sources")
    if isinstance(note_sources, Sequence) and not isinstance(note_sources, (str, bytes)):
        enabled_source_ids: list[str] = []
        for src in note_sources:
            if not isinstance(src, Mapping):
                continue
            sid = str(src.get("id") or "").strip()
            if not sid:
                continue
            if _capabilities_satisfied(src.get("capability"), active_caps):
                enabled_source_ids.append(sid)
        if enabled_source_ids:
            template = str(item.get("note_template") or "").strip()
            if template:
                return template.format(sources="/".join(enabled_source_ids))
            return str(item.get("note") or "").strip()
        return str(item.get("fallback_note") or "").strip()

    if "note_capability" in item:
        if _capabilities_satisfied(item.get("note_capability"), active_caps):
            return str(item.get("note") or "").strip()
        return str(item.get("fallback_note") or "").strip()

    return str(item.get("note") or "").strip()


def _format_all_route_copy(
    yaml_entry: Mapping[str, Any],
    active_caps: frozenset[CapabilityId],
) -> tuple[str, str]:
    """Return ``(rules, description)`` for the aggregate ``all`` route.

    Preserves the exact Everflow copy when every sub-source behind ``all`` is
    enabled, and narrows the wording to enabled sub-sources when only a subset
    of capabilities is active.
    """
    full_all_caps = (
        CapabilityId.COMPANY_WIKI,
        CapabilityId.SALES_NOTES,
        CapabilityId.TMTB,
        CapabilityId.FUNDA_AI,
        CapabilityId.ALPHAENGINE,
        CapabilityId.GUIDEPOINT,
    )
    if all(cap in active_caps for cap in full_all_caps):
        rules = _normalize_rules_text(yaml_entry.get("rules"))
        desc = str(yaml_entry.get("description") or "").strip() or "未写说明"
        return rules, desc

    rule_parts: list[str] = []
    desc_parts: list[str] = []
    four_libs = (
        (CapabilityId.COMPANY_WIKI, "company-wiki", "公司资料库"),
        (CapabilityId.SALES_NOTES, "sales-notes", "销售邮件"),
        (CapabilityId.TMTB, "tmtb", "TMT总结"),
        (CapabilityId.FUNDA_AI, "funda-ai", "FUNDA AI"),
    )
    enabled_four = [item for item in four_libs if item[0] in active_caps]
    if len(enabled_four) == 4:
        rule_parts.append("四库")
        desc_parts.append("四库")
    else:
        for _cap, short_id, label in enabled_four:
            rule_parts.append(short_id)
            desc_parts.append(label)
    if CapabilityId.ALPHAENGINE in active_caps:
        rule_parts.append("AE")
        desc_parts.append("卖方研报")
    if CapabilityId.GUIDEPOINT in active_caps:
        rule_parts.append("GP")
        desc_parts.append("专家访谈")

    if not rule_parts:
        return "仅覆盖当前已启用的桥接子源", "跨已启用研究子源联搜，不确定资料在哪时初筛。"

    rules_prefix = "+".join(rule_parts)
    if CapabilityId.ALPHAENGINE in active_caps:
        rules_text = f"{rules_prefix},不含transcript"
    else:
        rules_text = rules_prefix
    desc_text = f"{'/'.join(desc_parts)}联搜，不确定资料在哪时初筛。"
    return rules_text, desc_text


def merge_catalog_routes(
    *,
    catalog_routes: Sequence[Mapping[str, Any]] | None = None,
    handoff_routes: set[str] | frozenset[str] | None = None,
    spec: Mapping[str, Any] | None = None,
    capabilities: frozenset[CapabilityId] | None = None,
) -> list[dict[str, Any]]:
    """Merge live `catalog()` routes with `sources.yaml` descriptions.

    - Routes in `handoff_routes` (or with `kind` in `('agent_tool', 'skill')`) are excluded.
    - Routes whose required capability (or ``researchdata_bridge``) is disabled are excluded.
    - Routes present in `catalog_routes` but absent from `sources.yaml` are still included
      (when ``researchdata_bridge`` is enabled) and marked with `description = "未写说明"`.
    """
    active_caps = effective_capabilities() if capabilities is None else capabilities
    if CapabilityId.RESEARCHDATA_BRIDGE not in active_caps:
        return []

    loaded_spec = dict(spec) if isinstance(spec, Mapping) else load_sources_spec()
    yaml_routes = (
        dict(loaded_spec.get("catalog_routes") or {})
        if isinstance(loaded_spec.get("catalog_routes"), Mapping)
        else {}
    )
    default_handoff = {
        str(r).strip()
        for r in (loaded_spec.get("handoff_filtered_routes") or [])
        if str(r).strip()
    }
    effective_handoff = (
        {str(r).strip() for r in handoff_routes if str(r).strip()}
        if handoff_routes is not None
        else default_handoff
    )

    merged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    if catalog_routes is not None:
        for raw_route in catalog_routes:
            if not isinstance(raw_route, Mapping):
                continue
            route_id = str(raw_route.get("id") or "").strip()
            if not route_id or route_id in effective_handoff:
                continue
            kind = str(raw_route.get("kind") or "").strip().lower()
            if kind in ("agent_tool", "skill") and route_id not in (
                "xueqiu",
                "employee",
                "last30days",
            ):
                continue
            if not is_bridge_route_enabled(route_id, capabilities=active_caps):
                continue
            yaml_entry = yaml_routes.get(route_id)
            if isinstance(yaml_entry, Mapping) and not _capabilities_satisfied(
                yaml_entry.get("capability"), active_caps
            ):
                continue
            seen_ids.add(route_id)
            if isinstance(yaml_entry, Mapping):
                category = str(
                    raw_route.get("category") or yaml_entry.get("category") or "research"
                ).strip()
                title = str(
                    yaml_entry.get("title") or raw_route.get("title") or route_id
                ).strip()
                call_hint = str(yaml_entry.get("call") or "").strip()
                cat_rules = _normalize_rules_text(raw_route.get("rules"))[:60]
                if route_id == "all":
                    yaml_rules, desc = _format_all_route_copy(yaml_entry, active_caps)
                else:
                    yaml_rules = _normalize_rules_text(yaml_entry.get("rules"))
                    desc = str(yaml_entry.get("description") or "").strip() or "未写说明"
                rules_text = yaml_rules or cat_rules
            else:
                category = str(raw_route.get("category") or "research").strip()
                title = str(raw_route.get("title") or route_id).strip()
                call_hint = f'{category}(route="{route_id}", ...)'
                rules_text = _normalize_rules_text(raw_route.get("rules"))[:60]
                desc = "未写说明"
            merged.append(
                {
                    "id": route_id,
                    "category": category,
                    "title": title,
                    "call": call_hint,
                    "rules": rules_text,
                    "description": desc,
                }
            )
    else:
        for route_id, raw_entry in yaml_routes.items():
            rid = str(route_id).strip()
            if not rid or rid in effective_handoff or not isinstance(raw_entry, Mapping):
                continue
            if not is_bridge_route_enabled(rid, capabilities=active_caps):
                continue
            if not _capabilities_satisfied(raw_entry.get("capability"), active_caps):
                continue
            seen_ids.add(rid)
            if rid == "all":
                rules_text, desc_text = _format_all_route_copy(raw_entry, active_caps)
            else:
                rules_text = _normalize_rules_text(raw_entry.get("rules"))
                desc_text = str(raw_entry.get("description") or "").strip() or "未写说明"
            merged.append(
                {
                    "id": rid,
                    "category": str(raw_entry.get("category") or "research").strip(),
                    "title": str(raw_entry.get("title") or rid).strip(),
                    "call": str(raw_entry.get("call") or "").strip(),
                    "rules": rules_text,
                    "description": desc_text,
                }
            )

    return merged


def _format_conversation_stats_line(
    conversation_stats: Mapping[str, Any] | None,
    *,
    include_top_subjects: bool = True,
) -> str:
    """Format `dalton_conversation_cards` statistics into a compact summary line."""
    if not isinstance(conversation_stats, Mapping):
        return "归档统计：未统计（仅在团队记忆开启时可用 `conversation_search` 查 fact/estimate/model_assumption/opinion/question/decision/todo 卡片）。"

    total = int(conversation_stats.get("total_cards") or 0)
    min_date = str(conversation_stats.get("min_date") or "").strip()
    max_date = str(conversation_stats.get("max_date") or "").strip()
    date_span = f"{min_date}~{max_date}" if (min_date and max_date) else "暂无"

    by_type_raw = conversation_stats.get("by_type")
    by_type = dict(by_type_raw) if isinstance(by_type_raw, Mapping) else {}
    type_parts = [
        f"{t}:{int(by_type.get(t) or 0)}"
        for t in db_data_map.CARD_TYPES_ORDER
    ]
    type_str = " ".join(type_parts)

    if not include_top_subjects:
        return f"归档统计：覆盖 `{date_span}` | 共 **{total}** 张（{type_str}）。"

    top_raw = conversation_stats.get("top_subjects")
    top_items: list[str] = []
    if isinstance(top_raw, Sequence) and not isinstance(top_raw, (str, bytes)):
        for item in list(top_raw)[:10]:
            if isinstance(item, Mapping):
                lbl = str(item.get("label") or "").strip()
                cnt = int(item.get("count") or 0)
                if lbl:
                    top_items.append(f"{lbl}({cnt})")
            elif isinstance(item, (tuple, list)) and len(item) >= 2:
                top_items.append(f"{item[0]}({item[1]})")
    top_str = "、".join(top_items) if top_items else "暂无"

    return (
        f"归档统计：覆盖 `{date_span}` | 共 **{total}** 张"
        f"（{type_str}）| Top10：{top_str}。"
    )


def _strip_line_parenthetical_remarks(line: str) -> str:
    """Strip parenthetical remarks `(…)` / `（…）` from the description/remark part of a bullet line."""
    if not line.lstrip().startswith("- "):
        return line
    if ":" in line:
        head, tail = line.split(":", 1)
        cleaned_tail = _PAREN_NOTE_RE.sub("", tail).strip()
        return f"{head}:{cleaned_tail}"
    return _PAREN_NOTE_RE.sub("", line).strip()


def _build_data_map_lines(
    *,
    routes: list[dict[str, Any]],
    direct_tools: Mapping[str, Any],
    unavailable_list: Sequence[Any],
    health_lookup: Mapping[str, Mapping[str, Any]],
    conversation_stats: Mapping[str, Any] | None,
    include_x_search: bool,
    include_top_subjects: bool,
    active_caps: frozenset[CapabilityId],
) -> list[str]:
    has_conv_cap = CapabilityId.CONVERSATION_ARCHIVE in active_caps
    section1_heading = (
        "### 1. 直接工具与对话归档" if has_conv_cap else "### 1. 直接工具"
    )
    lines: list[str] = [
        "## Dalton 数据地图",
        section1_heading,
    ]

    for tool_id, raw_tool in direct_tools.items():
        tid = str(tool_id).strip()
        if not tid or not isinstance(raw_tool, Mapping):
            continue
        if tid == "x_search" and not include_x_search:
            continue
        if not _capabilities_satisfied(raw_tool.get("capability"), active_caps):
            continue
        display_id = str(raw_tool.get("display_id") or tid).strip()
        title = str(raw_tool.get("title") or tid).strip()
        call_hint = str(raw_tool.get("call") or display_id).strip()
        desc = str(raw_tool.get("description") or "未写说明").strip()
        st_label = format_health_status(health_lookup.get(tid))
        call_part = f"|`{call_hint}`" if (call_hint and call_hint != display_id) else ""
        lines.append(f"- `{display_id}`({title}|{st_label}{call_part}):{desc}")
        if tid == "conversation_search":
            lines.append(
                f"  - {_format_conversation_stats_line(conversation_stats, include_top_subjects=include_top_subjects)}"
            )

    if routes:
        lines.append("")
        lines.append(
            "### 2. 研究库与金融/宏观/社区路由（经 `ExecuteCode` 调用 `researchdata` 的 `research`、`finance`、`macro` 或 `community`）"
        )

        for r in routes:
            rid = r["id"]
            cat = r["category"]
            title = r["title"]
            rules_text = r["rules"]
            desc = r["description"]
            st_label = format_health_status(health_lookup.get(rid) or health_lookup.get(f"researchdata:{rid}"))
            rules_part = f"({rules_text})" if rules_text else ""
            lines.append(f"- `{rid}`[{cat}/{title}|{st_label}]:{desc}{rules_part}")

    unavail_idx = 3 if routes else 2
    lines.append("")
    lines.append(f"### {unavail_idx}. 明确没有的数据（切勿以为查过）")
    for item in unavailable_list:
        if not isinstance(item, Mapping):
            continue
        if not _capabilities_satisfied(item.get("capability"), active_caps):
            continue
        uid = str(item.get("id") or "").strip()
        utitle = str(item.get("title") or uid).strip()
        unote = _resolve_unavailable_source_note(item, active_caps)
        if utitle:
            lines.append(f"- {utitle}:{unote}")

    return lines


def render_data_map_markdown(
    *,
    catalog_routes: Sequence[Mapping[str, Any]] | None = None,
    handoff_routes: set[str] | frozenset[str] | None = None,
    health_map: Mapping[str, Mapping[str, Any]] | None = None,
    conversation_stats: Mapping[str, Any] | None = None,
    spec: Mapping[str, Any] | None = None,
    as_of_date: date | str | None = None,
    search_api: str | None = None,
    x_search_enabled: bool | None = None,
    max_tokens: int = MAX_DATA_MAP_TOKENS,
    capabilities: frozenset[CapabilityId] | None = None,
) -> str:
    """Render the compact Dalton Data Map Markdown block (<= `max_tokens`).

    When the rendered output exceeds `max_tokens`, progressively drops:
    1) Top 10 subjects in the conversation archive stats line;
    2) Parenthetical remarks/rules on each bullet line until `<= max_tokens`.
    """
    _ = as_of_date  # Title is date-free to keep the static prompt prefix stable.
    active_caps = effective_capabilities() if capabilities is None else capabilities
    loaded_spec = dict(spec) if isinstance(spec, Mapping) else load_sources_spec()
    health_lookup = dict(health_map) if isinstance(health_map, Mapping) else {}
    include_x = _is_x_search_enabled(search_api, x_search_enabled)

    routes = merge_catalog_routes(
        catalog_routes=catalog_routes,
        handoff_routes=handoff_routes,
        spec=loaded_spec,
        capabilities=active_caps,
    )
    direct_tools = (
        dict(loaded_spec.get("direct_tools") or {})
        if isinstance(loaded_spec.get("direct_tools"), Mapping)
        else {}
    )
    unavailable_list = (
        list(loaded_spec.get("unavailable_sources") or [])
        if isinstance(loaded_spec.get("unavailable_sources"), Sequence)
        else []
    )

    lines = _build_data_map_lines(
        routes=routes,
        direct_tools=direct_tools,
        unavailable_list=unavailable_list,
        health_lookup=health_lookup,
        conversation_stats=conversation_stats,
        include_x_search=include_x,
        include_top_subjects=True,
        active_caps=active_caps,
    )
    md = "\n".join(lines).strip()
    if estimate_data_map_tokens(md) <= max_tokens:
        return md

    # Step 1: drop Top 10 subjects from conversation stats
    lines = _build_data_map_lines(
        routes=routes,
        direct_tools=direct_tools,
        unavailable_list=unavailable_list,
        health_lookup=health_lookup,
        conversation_stats=conversation_stats,
        include_x_search=include_x,
        include_top_subjects=False,
        active_caps=active_caps,
    )
    md = "\n".join(lines).strip()
    if estimate_data_map_tokens(md) <= max_tokens:
        return md

    # Step 2: progressively drop parenthetical remarks on each line until within budget
    mutable_lines = list(lines)
    for idx in range(len(mutable_lines)):
        updated = _strip_line_parenthetical_remarks(mutable_lines[idx])
        if updated != mutable_lines[idx]:
            mutable_lines[idx] = updated
            md = "\n".join(mutable_lines).strip()
            if estimate_data_map_tokens(md) <= max_tokens:
                return md

    # Step 3: truncate Section 2 (researchdata routes) lines from the end and append overflow notice
    overflow_line = "…其余路由见 `catalog()`"
    sec2_header_idx = next(
        (i for i, ln in enumerate(mutable_lines) if ln.startswith("### 2. ")),
        None,
    )
    sec3_header_idx = next(
        (i for i, ln in enumerate(mutable_lines) if ln.startswith("### 3. ")),
        None,
    )
    if sec2_header_idx is not None and sec3_header_idx is not None and sec3_header_idx > sec2_header_idx:
        sec2_end_idx = (
            sec3_header_idx - 1
            if mutable_lines[sec3_header_idx - 1] == ""
            else sec3_header_idx
        )
        prefix_lines = mutable_lines[: sec2_header_idx + 1]
        sec2_route_lines = list(mutable_lines[sec2_header_idx + 1 : sec2_end_idx])
        suffix_lines = mutable_lines[sec2_end_idx:]
        while sec2_route_lines:
            sec2_route_lines.pop()
            candidate_lines = [
                *prefix_lines,
                *sec2_route_lines,
                overflow_line,
                *suffix_lines,
            ]
            md = "\n".join(candidate_lines).strip()
            if estimate_data_map_tokens(md) <= max_tokens:
                return md

    return md


def render_static_data_map_markdown(
    *,
    spec: Mapping[str, Any] | None = None,
    search_api: str | None = None,
    x_search_enabled: bool | None = None,
    team_memory_enabled: bool = False,
    capabilities: frozenset[CapabilityId] | None = None,
) -> str:
    """Render the static fallback data map with all sources marked `未体检` (never touches DB or network)."""
    md = render_data_map_markdown(
        catalog_routes=None,
        handoff_routes=None,
        health_map={},
        conversation_stats=None,
        spec=spec,
        search_api=search_api,
        x_search_enabled=x_search_enabled,
        capabilities=capabilities,
    )
    return _apply_team_memory_filter(md, team_memory_enabled)


async def refresh_daily_data_map(
    *,
    cache_date: date | None = None,
    catalog_routes: Sequence[Mapping[str, Any]] | None = None,
    handoff_routes: set[str] | frozenset[str] | None = None,
    capabilities_fingerprint: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Build today's data map from Postgres health + conversation stats and save it to `dalton_data_map_scoped_cache`.

    Called only by the health check task (startup or daily CLI), never on a user turn path.
    """
    target_date = cache_date or today_cache_date()
    active_caps = effective_capabilities()
    fp = (
        capabilities_fingerprint.strip()
        if isinstance(capabilities_fingerprint, str) and capabilities_fingerprint.strip()
        else compute_capabilities_fingerprint(active_caps)
    )
    health_map = await db_data_map.fetch_all_source_health(conn=conn)
    conv_stats: dict[str, Any] | None = None
    if CapabilityId.CONVERSATION_ARCHIVE in active_caps:
        try:
            conv_stats = await db_data_map.fetch_conversation_archive_stats(conn=conn)
        except Exception as exc:
            logger.warning("Failed to fetch conversation archive stats for data map: %s", type(exc).__name__)
            conv_stats = None

    rendered = render_data_map_markdown(
        catalog_routes=catalog_routes,
        handoff_routes=handoff_routes,
        health_map=health_map,
        conversation_stats=conv_stats,
        x_search_enabled=True,
        capabilities=active_caps,
    )
    token_est = estimate_data_map_tokens(rendered)
    payload: dict[str, Any] = {
        "cache_date": target_date.isoformat(),
        "capabilities_fingerprint": fp,
        "token_estimate": token_est,
        "health_count": len(health_map),
        "conversation_stats": conv_stats,
    }
    if catalog_routes is not None:
        payload["catalog_routes"] = [dict(r) for r in catalog_routes if isinstance(r, Mapping)]
    if handoff_routes is not None:
        payload["handoff_routes"] = sorted(str(r) for r in handoff_routes)

    if capabilities_fingerprint is not None:
        await db_data_map.upsert_cached_data_map(
            target_date,
            rendered,
            payload,
            capabilities_fingerprint=fp,
            conn=conn,
        )
    else:
        await db_data_map.upsert_cached_data_map(
            target_date,
            rendered,
            payload,
            conn=conn,
        )
    return {
        "cache_date": target_date.isoformat(),
        "capabilities_fingerprint": fp,
        "rendered_markdown": rendered,
        "token_estimate": token_est,
        "payload": payload,
    }


async def get_or_build_data_map_markdown(
    *,
    cache_date: date | None = None,
    conn: Any = None,
    search_api: str | None = None,
    x_search_enabled: bool | None = None,
    timeout_s: float = CACHE_READ_TIMEOUT_S,
    team_memory_enabled: bool = False,
    capabilities_fingerprint: str | None = None,
) -> str:
    """Read today's cached data map from Postgres within `timeout_s` (2.0s).

    On cache miss, timeout, or any DB error, returns the static `未体检` map without
    rebuilding, querying archive statistics, or writing to `dalton_data_map_scoped_cache`.
    `asyncio.CancelledError` always propagates.
    """
    target_date = cache_date or today_cache_date()
    try:
        read_coro = (
            db_data_map.get_cached_data_map(
                target_date,
                capabilities_fingerprint=capabilities_fingerprint,
                conn=conn,
            )
            if capabilities_fingerprint is not None
            else db_data_map.get_cached_data_map(target_date, conn=conn)
        )
        cached = await asyncio.wait_for(read_coro, timeout=timeout_s)
        if cached and cached.get("rendered_markdown"):
            md = _apply_search_api_filter(
                str(cached["rendered_markdown"]), search_api, x_search_enabled
            )
            return _apply_team_memory_filter(md, team_memory_enabled)
        return render_static_data_map_markdown(
            search_api=search_api,
            x_search_enabled=x_search_enabled,
            team_memory_enabled=team_memory_enabled,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "Data map Postgres cache read failed or timed out (%s); falling back to static unprobed data map",
            type(exc).__name__,
        )
        return render_static_data_map_markdown(
            search_api=search_api,
            x_search_enabled=x_search_enabled,
            team_memory_enabled=team_memory_enabled,
        )


def set_turn_data_map(markdown: str | None) -> None:
    """Bind the pre-fetched data map markdown for the current async turn task."""
    _TURN_DATA_MAP_VAR.set(markdown)


async def warm_turn_data_map(
    *,
    search_api: str | None = None,
    x_search_enabled: bool | None = None,
    cache_date: date | None = None,
    conn: Any = None,
    team_memory_enabled: bool = False,
) -> str:
    """Read today's cached data map (or static fallback) and bind it to the current turn context."""
    md = await get_or_build_data_map_markdown(
        cache_date=cache_date,
        conn=conn,
        search_api=search_api,
        x_search_enabled=x_search_enabled,
        team_memory_enabled=team_memory_enabled,
    )
    set_turn_data_map(md)
    return md


def _configured_x_search_enabled() -> bool:
    """Deployment ``x_search.enabled``. Only a real boolean turns the line on."""
    from src.config.tool_settings import _get_tool_config

    value = _get_tool_config("x_search.enabled", False)
    return value if isinstance(value, bool) else False


def resolve_prompt_data_map(*, team_memory_enabled: bool | None = None) -> str:
    """Return the current turn's pre-warmed data map, or the static `未体检` fallback if unset.

    The static fallback's ``x_search`` line follows ``x_search.enabled``.
    It does not follow ``search_api``.
    """
    current = _TURN_DATA_MAP_VAR.get()
    if current and current.strip():
        if team_memory_enabled is not None:
            return _apply_team_memory_filter(current, team_memory_enabled)
        return current
    if team_memory_enabled is None:
        effective = has_capability(CapabilityId.CONVERSATION_ARCHIVE)
    else:
        effective = bool(team_memory_enabled)
    return render_static_data_map_markdown(
        search_api=None,
        x_search_enabled=_configured_x_search_enabled(),
        team_memory_enabled=effective,
    )
