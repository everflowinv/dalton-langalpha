"""System-generated Dalton Data Map builder and daily PostgreSQL cache coordinator.

Multi-worker rule:
- Never store health or rendered data map state in module-level variables across turns.
- Durable truth lives in PostgreSQL (`dalton_data_source_health` and `dalton_data_map_cache`).
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


def _is_x_search_enabled(search_api: str | None) -> bool:
    """Return True iff `search_api` binds `x_search` (matches `PTCAgent.create_agent` in `agent.py`)."""
    return str(search_api or "").strip().lower() == "openclaw"


def _apply_search_api_filter(markdown: str, search_api: str | None) -> str:
    """Filter `- \\`x_search\\`...` from `markdown` when `search_api != 'openclaw'`."""
    if _is_x_search_enabled(search_api):
        return markdown
    return "\n".join(
        line
        for line in markdown.splitlines()
        if not line.lstrip().startswith("- `x_search`")
    )


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
    return "未体检"


def _normalize_rules_text(rules: Any) -> str:
    if isinstance(rules, str):
        return "；".join(part.strip() for part in rules.splitlines() if part.strip())
    if isinstance(rules, Sequence) and not isinstance(rules, (bytes, bytearray)):
        parts = [str(item).strip() for item in rules if str(item).strip()]
        return "；".join(parts)
    return ""


def merge_catalog_routes(
    *,
    catalog_routes: Sequence[Mapping[str, Any]] | None = None,
    handoff_routes: set[str] | frozenset[str] | None = None,
    spec: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Merge live `catalog()` routes with `sources.yaml` descriptions.

    - Routes in `handoff_routes` (or with `kind` in `('agent_tool', 'skill')`) are excluded.
    - Routes present in `catalog_routes` but absent from `sources.yaml` are still included
      and marked with `description = "未写说明"`.
    """
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
            if kind in ("agent_tool", "skill"):
                continue
            seen_ids.add(route_id)
            yaml_entry = yaml_routes.get(route_id)
            if isinstance(yaml_entry, Mapping):
                category = str(
                    raw_route.get("category") or yaml_entry.get("category") or "research"
                ).strip()
                title = str(
                    yaml_entry.get("title") or raw_route.get("title") or route_id
                ).strip()
                call_hint = str(yaml_entry.get("call") or "").strip()
                cat_rules = _normalize_rules_text(raw_route.get("rules"))[:60]
                yaml_rules = _normalize_rules_text(yaml_entry.get("rules"))
                rules_text = yaml_rules or cat_rules
                desc = str(yaml_entry.get("description") or "").strip() or "未写说明"
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
            seen_ids.add(rid)
            merged.append(
                {
                    "id": rid,
                    "category": str(raw_entry.get("category") or "research").strip(),
                    "title": str(raw_entry.get("title") or rid).strip(),
                    "call": str(raw_entry.get("call") or "").strip(),
                    "rules": _normalize_rules_text(raw_entry.get("rules")),
                    "description": str(raw_entry.get("description") or "").strip() or "未写说明",
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
        return "归档统计：未统计（可直接调 `conversation_search` 查 fact/estimate/model_assumption/opinion/question/decision/todo 卡片）。"

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
    unavailable_list: Sequence[ Any],
    health_lookup: Mapping[str, Mapping[str, Any]],
    conversation_stats: Mapping[str, Any] | None,
    include_x_search: bool,
    include_top_subjects: bool,
) -> list[str]:
    lines: list[str] = [
        "## Dalton 数据地图",
        "**下结论前先对照地图**：说“证据不足/无法确定”前，先对照本图看还有哪些相关来源没查；问到公司或主题时，先用 `conversation_search` 查团队以前的观点、假设和决定，并在答案里说明与之前看法相比有什么变化。",
        "### 1. 直接工具与对话归档",
    ]

    for tool_id, raw_tool in direct_tools.items():
        tid = str(tool_id).strip()
        if not tid or not isinstance(raw_tool, Mapping):
            continue
        if tid == "x_search" and not include_x_search:
            continue
        title = str(raw_tool.get("title") or tid).strip()
        call_hint = str(raw_tool.get("call") or tid).strip()
        desc = str(raw_tool.get("description") or "未写说明").strip()
        st_label = format_health_status(health_lookup.get(tid))
        call_part = f"|`{call_hint}`" if (call_hint and call_hint != tid) else ""
        lines.append(f"- `{tid}`({title}|{st_label}{call_part}):{desc}")
        if tid == "conversation_search":
            lines.append(
                f"  - {_format_conversation_stats_line(conversation_stats, include_top_subjects=include_top_subjects)}"
            )

    lines.append("")
    lines.append("### 2. 研究库与金融/宏观路由（`researchdata`，经 `ExecuteCode` 调 `research`/`finance`/`macro`）")

    for r in routes:
        rid = r["id"]
        cat = r["category"]
        title = r["title"]
        rules_text = r["rules"]
        desc = r["description"]
        st_label = format_health_status(health_lookup.get(rid) or health_lookup.get(f"researchdata:{rid}"))
        rules_part = f"({rules_text})" if rules_text else ""
        lines.append(f"- `{rid}`[{cat}/{title}|{st_label}]:{desc}{rules_part}")

    lines.append("")
    lines.append("### 3. 明确没有的数据（切勿以为查过）")
    for item in unavailable_list:
        if not isinstance(item, Mapping):
            continue
        uid = str(item.get("id") or "").strip()
        utitle = str(item.get("title") or uid).strip()
        unote = str(item.get("note") or "").strip()
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
    search_api: str | None = "openclaw",
    max_tokens: int = MAX_DATA_MAP_TOKENS,
) -> str:
    """Render the compact Dalton Data Map Markdown block (<= `max_tokens`).

    When the rendered output exceeds `max_tokens`, progressively drops:
    1) Top 10 subjects in the conversation archive stats line;
    2) Parenthetical remarks/rules on each bullet line until `<= max_tokens`.
    """
    _ = as_of_date  # Title is date-free to keep the static prompt prefix stable.
    loaded_spec = dict(spec) if isinstance(spec, Mapping) else load_sources_spec()
    health_lookup = dict(health_map) if isinstance(health_map, Mapping) else {}
    include_x = _is_x_search_enabled(search_api)

    routes = merge_catalog_routes(
        catalog_routes=catalog_routes,
        handoff_routes=handoff_routes,
        spec=loaded_spec,
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
    overflow_line = '…其余路由见 `research(route="all")` / `catalog()`'
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
    search_api: str | None = "openclaw",
) -> str:
    """Render the static fallback data map with all sources marked `未体检` (never touches DB or network)."""
    return render_data_map_markdown(
        catalog_routes=None,
        handoff_routes=None,
        health_map={},
        conversation_stats=None,
        spec=spec,
        search_api=search_api,
    )


async def refresh_daily_data_map(
    *,
    cache_date: date | None = None,
    catalog_routes: Sequence[Mapping[str, Any]] | None = None,
    handoff_routes: set[str] | frozenset[str] | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Build today's data map from Postgres health + conversation stats and save it to `dalton_data_map_cache`.

    Called only by the health check task (startup or daily CLI), never on a user turn path.
    """
    target_date = cache_date or today_cache_date()
    health_map = await db_data_map.fetch_all_source_health(conn=conn)
    try:
        conv_stats: dict[str, Any] | None = await db_data_map.fetch_conversation_archive_stats(conn=conn)
    except Exception as exc:
        logger.warning("Failed to fetch conversation archive stats for data map: %s", type(exc).__name__)
        conv_stats = None

    rendered = render_data_map_markdown(
        catalog_routes=catalog_routes,
        handoff_routes=handoff_routes,
        health_map=health_map,
        conversation_stats=conv_stats,
        search_api="openclaw",
    )
    token_est = estimate_data_map_tokens(rendered)
    payload: dict[str, Any] = {
        "cache_date": target_date.isoformat(),
        "token_estimate": token_est,
        "health_count": len(health_map),
        "conversation_stats": conv_stats,
    }
    if catalog_routes is not None:
        payload["catalog_routes"] = [dict(r) for r in catalog_routes if isinstance(r, Mapping)]
    if handoff_routes is not None:
        payload["handoff_routes"] = sorted(str(r) for r in handoff_routes)

    await db_data_map.upsert_cached_data_map(
        target_date,
        rendered,
        payload,
        conn=conn,
    )
    return {
        "cache_date": target_date.isoformat(),
        "rendered_markdown": rendered,
        "token_estimate": token_est,
        "payload": payload,
    }


async def get_or_build_data_map_markdown(
    *,
    cache_date: date | None = None,
    conn: Any = None,
    search_api: str | None = "openclaw",
    timeout_s: float = CACHE_READ_TIMEOUT_S,
) -> str:
    """Read today's cached data map from Postgres within `timeout_s` (2.0s).

    On cache miss, timeout, or any DB error, returns the static `未体检` map without
    rebuilding, querying archive statistics, or writing to `dalton_data_map_cache`.
    `asyncio.CancelledError` always propagates.
    """
    target_date = cache_date or today_cache_date()
    try:
        cached = await asyncio.wait_for(
            db_data_map.get_cached_data_map(target_date, conn=conn),
            timeout=timeout_s,
        )
        if cached and cached.get("rendered_markdown"):
            return _apply_search_api_filter(str(cached["rendered_markdown"]), search_api)
        return render_static_data_map_markdown(search_api=search_api)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "Data map Postgres cache read failed or timed out (%s); falling back to static unprobed data map",
            type(exc).__name__,
        )
        return render_static_data_map_markdown(search_api=search_api)


def set_turn_data_map(markdown: str | None) -> None:
    """Bind the pre-fetched data map markdown for the current async turn task."""
    _TURN_DATA_MAP_VAR.set(markdown)


async def warm_turn_data_map(
    *,
    search_api: str | None = "openclaw",
    cache_date: date | None = None,
    conn: Any = None,
) -> str:
    """Read today's cached data map (or static fallback) and bind it to the current turn context."""
    md = await get_or_build_data_map_markdown(
        cache_date=cache_date,
        conn=conn,
        search_api=search_api,
    )
    set_turn_data_map(md)
    return md


def resolve_prompt_data_map() -> str:
    """Return the current turn's pre-warmed data map, or the static `未体检` fallback if unset."""
    current = _TURN_DATA_MAP_VAR.get()
    if current and current.strip():
        return current
    return render_static_data_map_markdown()
