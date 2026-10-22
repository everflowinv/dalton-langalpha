"""Container-side conversation segmentation, LLM card extraction, quote verification, and deduplication.

Follows Dalton's LLM contract (`assist_json` -> `dalton_complete` -> `LLMService.complete`,
never `create_llm()`), defaulting to Antigravity's `gemini-4-argon-medium-oauth`.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.config.env import LOCAL_DEV_USER_ID
from src.dalton.database.conversations import build_card_search_text
from src.dalton.llm.assist import AssistResult, _normalize_schema_enums, assist_json
from src.dalton.llm.complete import resolve_model_billing_and_provider
from src.dalton.settings import get_dalton_settings

logger = logging.getLogger(__name__)

VALID_SEGMENT_CATEGORIES: frozenset[str] = frozenset({"research", "ops", "private"})

VALID_CARD_TYPES: frozenset[str] = frozenset(
    {
        "fact",
        "estimate",
        "model_assumption",
        "opinion",
        "question",
        "decision",
        "todo",
    }
)

# Private / unlisted companies must stay in `entities[]` rather than `tickers[]`
PRIVATE_COMPANIES: frozenset[str] = frozenset(
    {
        "OPENAI",
        "ANTHROPIC",
        "XAI",
        "SPACEX",
        "STRIPE",
        "DATABRICKS",
        "BYTEDANCE",
        "TIKTOK",
        "SHEIN",
        "ANDURIL",
        "SCALEAI",
        "SCALE AI",
        "PERPLEXITY",
        "CURSOR",
        "ANYSHERE",
        "MISTRAL",
        "SSI",
        "DEEPSEEK",
        "MOONSHOT",
        "KIMI",
        "ZHIPU",
        "UNITREE",
        "宇树",
        "宇树科技",
        "月之暗面",
        "智谱",
        "字节",
        "字节跳动",
        "小红书",
        "华为",
    }
)

EXTRACTION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": ["research", "ops", "private"],
        },
        "topic_label": {
            "type": "string",
        },
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "fact",
                            "estimate",
                            "model_assumption",
                            "opinion",
                            "question",
                            "decision",
                            "todo",
                        ],
                    },
                    "topic_label": {"type": "string"},
                    "claim": {"type": "string"},
                    "tickers": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "entities": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "sectors": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "figures": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "metric": {"type": "string"},
                                "value": {"type": ["number", "string"]},
                                "unit": {"type": "string"},
                                "period": {"type": "string"},
                            },
                            "required": ["metric", "value", "unit", "period"],
                        },
                    },
                    "quote": {"type": "string"},
                    "quote_event_id": {"type": "string"},
                    "cited_sources": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "confidence": {"type": "number"},
                },
                "required": [
                    "type",
                    "claim",
                    "quote",
                    "quote_event_id",
                ],
            },
        },
    },
    "required": ["category", "topic_label", "cards"],
}

EXTRACTION_SYSTEM_INSTRUCTIONS = """你是 Dalton 投研团队的对话历史整理器，负责把 OpenClaw 人类投研对话段整理成可追溯的知识卡片（来源类型固定为 `conversation`）。

步骤与硬性规则：
1. **话题分类 (`category`)**：
   - `"research"`：与公司基本面、财务指标、估值、行业趋势、商业模式、投资决策、研究待办相关的讨论。
   - `"ops"`：系统运维、授权批准、配对码、cron 定时任务、模型切换、环境配置。
   - `"private"`：私人生活、家庭、个人隐私、非投研闲聊。
   - 若为 `"ops"` 或 `"private"`，`cards` 必须返回空数组 `[]`。
2. **原文逐字引用 (`quote` & `quote_event_id`)**：
   - 每张卡的 `quote` **必须逐字摘自输入事件 `text` 的原话子串**，绝不能改写、概括、拼接或编造！
   - `quote_event_id` 必须填写该句原话所在事件的 `event_id`。
3. **说话人与来源 (`cited_sources`)**：
   - 若原话来自 `agent`（如 Eve），只有当它在原话中明确给出了具体资料来源时才可标为 `"fact"` 并填入 `cited_sources`；若无明确引用来源，一律归为 `"opinion"`（agent 观点），不当作事实。
4. **上市公司 vs 未上市私企**：
   - 上市公司股票代码（如 `META`, `NVDA`, `AMZN`, `GOOGL`, `MSFT`, `BABA`, `PDD`, `TSM`）放入 `tickers`（大写）。
   - 未上市私企（如 `OpenAI`, `Anthropic`, `ByteDance`, `SpaceX`, `Stripe`, `Databricks`, `DeepSeek`, `Unitree`）放入 `entities`，**禁止**放入 `tickers`。
5. **结构化指标 (`figures`)**：
   - 当原话包含明确数值指标时，提取 `{metric, value, unit, period}`（例如 `{"metric": "capex", "value": 65, "unit": "USD_bn", "period": "FY2026"}`）。
"""


@dataclass
class SegmentExtractionOutcome:
    """Outcome of extracting cards from a single conversation segment."""

    ok: bool = True
    category: str = "research"
    cards: list[dict[str, Any]] = field(default_factory=list)
    cards_raw_count: int = 0
    quotes_rejected: int = 0
    discarded_segment: bool = False
    model_used: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    rate_limited: bool = False
    permanent_quota_error: bool = False
    status: str = ""
    error_kind: str | None = None
    in_flight: bool = False
    transient_error: bool = False
    cached: bool = False
    replayed: bool = False
    error: str | None = None


def normalize_card_enum_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip whitespace and lowercase enum fields (`category`, `cards[].type`), rejecting invalid values."""
    _normalize_schema_enums(payload, EXTRACTION_RESPONSE_SCHEMA)
    if "category" in payload and payload["category"] is not None:
        cat = str(payload["category"]).strip().lower()
        payload["category"] = cat
        if cat not in VALID_SEGMENT_CATEGORIES:
            allowed_cats = list(EXTRACTION_RESPONSE_SCHEMA["properties"]["category"]["enum"])
            raise ValueError(f"ValidationError: {cat!r} is not one of {allowed_cats}")
    raw_cards = payload.get("cards")
    if isinstance(raw_cards, list):
        allowed_types = list(
            EXTRACTION_RESPONSE_SCHEMA["properties"]["cards"]["items"]["properties"]["type"]["enum"]
        )
        for card in raw_cards:
            if isinstance(card, dict) and "type" in card and card["type"] is not None:
                c_type = str(card["type"]).strip().lower()
                card["type"] = c_type
                if c_type not in VALID_CARD_TYPES:
                    raise ValueError(f"ValidationError: {c_type!r} is not one of {allowed_types}")
    return payload


def normalize_whitespace(text: str) -> str:
    """Collapse all whitespace sequences into a single space for verbatim quote matching."""
    return re.sub(r"\s+", " ", text or "").strip()


def compute_content_hash(quote: str, quote_event_id: str) -> str:
    """Compute `sha256(normalized_quote + event_id)` for idempotent deduplication."""
    norm_q = normalize_whitespace(quote)
    ev_id = str(quote_event_id or "").strip()
    return hashlib.sha256(f"{norm_q}{ev_id}".encode("utf-8")).hexdigest()


def verify_verbatim_quote(
    quote: str,
    quote_event_id: str,
    segment_events: list[dict[str, Any]],
) -> tuple[bool, dict[str, Any] | None]:
    """Verify that `quote` appears verbatim (after whitespace normalization) in `segment_events`.

    Checks the event matching `quote_event_id` first, then falls back to scanning other events
    in the same segment (updating `quote_event_id` to the matched event).
    """
    norm_quote = normalize_whitespace(quote)
    if not norm_quote:
        return False, None

    by_id = {str(ev.get("event_id") or ""): ev for ev in segment_events}
    target_ev = by_id.get(str(quote_event_id or ""))
    if target_ev is not None:
        ev_norm_text = normalize_whitespace(str(target_ev.get("text") or ""))
        if norm_quote in ev_norm_text:
            return True, target_ev

    for ev in segment_events:
        ev_norm_text = normalize_whitespace(str(ev.get("text") or ""))
        if norm_quote in ev_norm_text:
            return True, ev

    return False, None


def normalize_tickers_and_entities(
    raw_tickers: list[Any] | None,
    raw_entities: list[Any] | None,
) -> tuple[list[str], list[str]]:
    """Route known private companies (OpenAI, Anthropic, etc.) into `entities[]` and normalize tickers."""
    tickers: list[str] = []
    entities: list[str] = []

    for e in raw_entities or []:
        es = str(e or "").strip()
        if es and es not in entities:
            entities.append(es)

    for t in raw_tickers or []:
        ts = str(t or "").strip()
        if not ts:
            continue
        upper_t = ts.upper()
        if upper_t in PRIVATE_COMPANIES or ts in PRIVATE_COMPANIES:
            if ts not in entities and upper_t not in {x.upper() for x in entities}:
                entities.append(ts)
        else:
            if upper_t not in tickers:
                tickers.append(upper_t)

    return tickers, entities


def enforce_speaker_and_agent_citation(
    card_type: str,
    cited_sources: list[Any] | None,
    source_event: dict[str, Any],
    *,
    segment_events: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], str, list[str]]:
    """Populate speaker from `source_event` and downgrade uncited or unverified agent claims to `'opinion'`."""
    ev_speaker = source_event.get("speaker")
    if isinstance(ev_speaker, dict):
        role = str(ev_speaker.get("role") or "participant").lower()
        if role not in ("owner", "participant", "agent"):
            role = "owner" if ev_speaker.get("is_owner") else "participant"
        speaker = {
            "sender_id_hash": str(ev_speaker.get("sender_id_hash") or "unknown"),
            "name": str(ev_speaker.get("name") or ("Owner" if role == "owner" else "Participant")),
            "is_owner": bool(ev_speaker.get("is_owner", role == "owner")),
            "role": role,
        }
    else:
        msg_role = str(source_event.get("message_role") or "user")
        role = "agent" if msg_role == "assistant" else "owner"
        speaker = {
            "sender_id_hash": "agent:main" if role == "agent" else "owner",
            "name": "Eve" if role == "agent" else "Owner",
            "is_owner": role == "owner",
            "role": role,
        }

    cleaned_sources = [
        str(s).strip() for s in (cited_sources or []) if s is not None and str(s).strip()
    ]
    eff_type = str(card_type if card_type is not None else "opinion").strip().lower()
    if eff_type not in VALID_CARD_TYPES:
        allowed_types = list(
            EXTRACTION_RESPONSE_SCHEMA["properties"]["cards"]["items"]["properties"]["type"]["enum"]
        )
        raise ValueError(f"ValidationError: {eff_type!r} is not one of {allowed_types}")

    # Rule: agent (Eve) fact/estimate/model_assumption requires non-empty cited_sources
    # where every cited source is present as a substring in the segment's raw event text.
    if speaker["role"] == "agent" and eff_type in ("fact", "estimate", "model_assumption"):
        events_for_check = segment_events if segment_events else [source_event]
        combined_raw = "\n".join(str(ev.get("text") or "") for ev in events_for_check)
        combined_norm = normalize_whitespace(combined_raw)
        if not cleaned_sources:
            eff_type = "opinion"
        else:
            all_verified = all(
                (src in combined_raw) or (normalize_whitespace(src) in combined_norm)
                for src in cleaned_sources
            )
            if not all_verified:
                eff_type = "opinion"

    return speaker, eff_type, cleaned_sources


def _split_oversized_event(
    ev: dict[str, Any],
    max_chars: int,
    *,
    persisted_plan: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Split a single event whose `text` exceeds `max_chars` into chunks `<= max_chars` with stable `chunk_idx` and `chunk_spans`."""
    if ev.get("_chunk_expanded") is True and persisted_plan is None:
        return [ev]

    text = str(ev.get("text") or "")
    if isinstance(persisted_plan, dict):
        plan_limit = max(1, int(persisted_plan.get("chunk_max_chars") or max_chars))
        raw_spans = persisted_plan.get("chunk_spans")
        if isinstance(raw_spans, list) and raw_spans:
            valid_spans: list[list[int]] = []
            for sp in raw_spans:
                if isinstance(sp, (list, tuple)) and len(sp) == 2:
                    s = max(0, min(len(text), int(sp[0])))
                    e = max(s, min(len(text), int(sp[1])))
                    valid_spans.append([s, e])
            if valid_spans:
                return [
                    {
                        **ev,
                        "text": text[s:e],
                        "chunk_idx": idx,
                        "chunk_count": len(valid_spans),
                        "chunk_max_chars": plan_limit,
                        "chunk_spans": valid_spans,
                        "_chunk_expanded": True,
                    }
                    for idx, (s, e) in enumerate(valid_spans)
                    if text[s:e] or len(valid_spans) == 1
                ]
        limit = plan_limit
    else:
        limit = max(1, int(max_chars))

    if len(text) <= limit:
        spans = [[0, len(text)]]
        ev["chunk_idx"] = int(ev.get("chunk_idx", 0))
        ev["chunk_count"] = 1
        ev["chunk_max_chars"] = limit
        ev["chunk_spans"] = spans
        ev["_chunk_expanded"] = True
        return [ev]

    spans = []
    pos = 0
    rem_end = len(text)
    while (rem_end - pos) > limit:
        window = text[pos : pos + limit]
        split_at = -1
        for sep in ("\n\n", "\n", "。", "！", "？", ". ", "; ", "，", " "):
            found_pos = window.rfind(sep)
            if found_pos >= limit // 2:
                split_at = found_pos + len(sep)
                break
        if split_at <= 0:
            split_at = limit

        c_start = pos
        c_end = pos + split_at
        while c_start < c_end and text[c_start].isspace():
            c_start += 1
        while c_end > c_start and text[c_end - 1].isspace():
            c_end -= 1
        if c_end > c_start:
            spans.append([c_start, c_end])

        pos = pos + split_at
        rem_end = len(text.rstrip())
        while pos < rem_end and text[pos].isspace():
            pos += 1

    if pos < rem_end:
        c_start = pos
        c_end = rem_end
        while c_start < c_end and text[c_start].isspace():
            c_start += 1
        while c_end > c_start and text[c_end - 1].isspace():
            c_end -= 1
        if c_end > c_start:
            spans.append([c_start, c_end])

    return [
        {
            **ev,
            "text": text[s:e],
            "chunk_idx": idx,
            "chunk_count": len(spans),
            "chunk_max_chars": limit,
            "chunk_spans": spans,
            "_chunk_expanded": True,
        }
        for idx, (s, e) in enumerate(spans)
    ]


def expand_event_chunks(
    events: list[dict[str, Any]],
    *,
    max_segment_chars: int = 6000,
    persisted_plans: dict[tuple[str, str, str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Expand raw events into deterministic per-event chunks (`chunk_idx = 0..N-1`), reusing `persisted_plans` when present."""
    eff_max_chars = max(1, int(max_segment_chars))
    expanded: list[dict[str, Any]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        ev_key = (
            str(ev.get("agent") or "main"),
            str(ev.get("session_id") or ""),
            str(ev.get("event_id") or f"seq:{int(ev.get('seq', 0))}"),
        )
        plan = (persisted_plans or {}).get(ev_key)
        expanded.extend(
            _split_oversized_event(ev, eff_max_chars, persisted_plan=plan)
        )
    return expanded


def segment_events(
    events: list[dict[str, Any]],
    *,
    max_gap_minutes: int = 30,
    max_segment_chars: int = 6000,
    max_segment_events: int = 25,
) -> list[list[dict[str, Any]]]:
    """Group events by `(agent, session_id)` and split into time-adjacent topic segments."""
    by_session: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        key = (str(ev.get("agent") or "main"), str(ev.get("session_id") or ""))
        by_session.setdefault(key, []).append(ev)

    segments: list[list[dict[str, Any]]] = []
    max_gap_ms = max(1, max_gap_minutes) * 60 * 1000
    eff_max_chars = max(1, int(max_segment_chars))

    for _, sess_events in sorted(by_session.items()):
        ordered_raw = sorted(
            sess_events,
            key=lambda x: (
                int(x.get("seq", 0)),
                int(x.get("created_at_ms", 0)),
                int(x.get("chunk_idx", 0)),
            ),
        )
        ordered: list[dict[str, Any]] = []
        for raw_ev in ordered_raw:
            ordered.extend(_split_oversized_event(raw_ev, eff_max_chars))

        current_seg: list[dict[str, Any]] = []
        current_chars = 0
        last_ms: int | None = None

        for ev in ordered:
            ev_ms = int(ev.get("created_at_ms") or 0)
            ev_chars = len(str(ev.get("text") or ""))
            gap_exceeded = (
                last_ms is not None
                and ev_ms > 0
                and last_ms > 0
                and (ev_ms - last_ms) > max_gap_ms
            )
            size_exceeded = (
                len(current_seg) >= max_segment_events
                or (current_chars + ev_chars > eff_max_chars and len(current_seg) > 0)
            )
            if current_seg and (gap_exceeded or size_exceeded):
                segments.append(current_seg)
                current_seg = []
                current_chars = 0

            current_seg.append(ev)
            current_chars += ev_chars
            if ev_ms > 0:
                last_ms = ev_ms

        if current_seg:
            segments.append(current_seg)

    segments.sort(
        key=lambda seg: (
            int(seg[0].get("created_at_ms") or 0),
            str(seg[0].get("ts") or ""),
            str(seg[0].get("agent") or ""),
            str(seg[0].get("session_id") or ""),
            int(seg[0].get("seq") or 0),
            int(seg[0].get("chunk_idx") or 0),
        )
    )
    return segments


def _normalize_figure_key(metric: Any, period: Any) -> tuple[str, str]:
    m = re.sub(r"[\s_-]+", "", str(metric or "").strip().lower())
    p = re.sub(r"[\s_-]+", "", str(period or "").strip().upper())
    return m, p


def _normalize_figure_value(val: Any) -> str:
    if isinstance(val, (int, float)):
        return f"{float(val):.6g}"
    s = str(val or "").strip()
    try:
        return f"{float(s):.6g}"
    except ValueError:
        return s.lower()


def _parse_card_ts_epoch(card: dict[str, Any]) -> float:
    ts_val = card.get("ts")
    if isinstance(ts_val, datetime):
        dt = ts_val if ts_val.tzinfo else ts_val.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(ts_val, str) and ts_val.strip():
        s = ts_val.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass
    return 0.0


def _card_max_seq(card: dict[str, Any]) -> int:
    seq_range = card.get("seq_range")
    if isinstance(seq_range, list) and seq_range:
        try:
            return max(int(x) for x in seq_range)
        except Exception:
            return 0
    return 0


def _card_event_sort_key(card: dict[str, Any]) -> float:
    """Return strict event timestamp epoch for `supersedes` eligibility comparison."""
    return _parse_card_ts_epoch(card)


def _card_display_sort_key(card: dict[str, Any]) -> tuple[float, int, str]:
    """Return `(ts_epoch, max_seq, card_id)` for deterministic display ordering (`seq` never breaks ties for `supersedes`)."""
    return (_parse_card_ts_epoch(card), _card_max_seq(card), str(card.get("card_id") or ""))


def deduplicate_candidate_cards(
    candidate_cards: list[dict[str, Any]],
    *,
    existing_hashes: set[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Deduplicate `candidate_cards` by `content_hash` and initialize all new cards with `supersedes=None`."""
    seen_hashes: set[str] = set(existing_hashes or set())
    accepted: list[dict[str, Any]] = []
    deduped_count = 0

    for card in candidate_cards:
        c_hash = str(card.get("content_hash") or "")
        if not c_hash:
            c_hash = compute_content_hash(
                str(card.get("quote") or ""), str(card.get("quote_event_id") or "")
            )
            card["content_hash"] = c_hash

        if c_hash in seen_hashes:
            deduped_count += 1
            continue

        seen_hashes.add(c_hash)
        card["status"] = "active"
        card["supersedes"] = None
        card["superseded_card_ids"] = []
        card["superseded_by_card_id"] = None
        accepted.append(card)

    return accepted, deduped_count


def compute_batch_supersedes(
    new_cards: list[dict[str, Any]],
    *,
    existing_active_cards: list[dict[str, Any]] | None = None,
) -> int:
    """Compute `(ticker, metric, period)` `supersedes` relationships after all `new_cards` exist.

    Eligibility strictly compares event timestamps (`_card_event_sort_key`): cards with identical
    event timestamps never supersede one another, and all active cards in the same single-ticker
    `(ticker, metric, period)` group with a strictly earlier event timestamp are marked
    `status='superseded'` regardless of whether their numeric value changed.
    Returns `superseded_count`.
    """
    active_pool: list[dict[str, Any]] = list(existing_active_cards or [])
    superseded_count = 0

    indexed_new = list(enumerate(new_cards))
    indexed_new.sort(key=lambda pair: (_card_event_sort_key(pair[1]), pair[0]))

    for _, card in indexed_new:
        card.setdefault("status", "active")
        card.setdefault("supersedes", None)
        card.setdefault("superseded_card_ids", [])
        card.setdefault("superseded_by_card_id", None)

        card_tickers = [str(t).upper() for t in (card.get("tickers") or []) if str(t).strip()]
        card_figures = [f for f in (card.get("figures") or []) if isinstance(f, dict)]

        if len(card_tickers) == 1 and card_figures:
            single_ticker = card_tickers[0]
            card_ts = _card_event_sort_key(card)
            superseded_targets: list[dict[str, Any]] = []
            superseded_seen_ids: set[str] = set()
            newer_superseding_cards: list[dict[str, Any]] = []
            newer_seen_ids: set[str] = set()

            pool_ordered = sorted(
                active_pool,
                key=_card_display_sort_key,
                reverse=True,
            )
            for fig in card_figures:
                m_key, p_key = _normalize_figure_key(fig.get("metric"), fig.get("period"))
                if not m_key or not p_key:
                    continue

                for prev in pool_ordered:
                    if prev.get("status") != "active":
                        continue
                    prev_tickers = [
                        str(t).upper() for t in (prev.get("tickers") or []) if str(t).strip()
                    ]
                    if len(prev_tickers) != 1 or prev_tickers[0] != single_ticker:
                        continue
                    prev_ts = _card_event_sort_key(prev)

                    prev_figs = [pf for pf in (prev.get("figures") or []) if isinstance(pf, dict)]
                    for pf in prev_figs:
                        pm_key, pp_key = _normalize_figure_key(pf.get("metric"), pf.get("period"))
                        if (pm_key, pp_key) == (m_key, p_key):
                            pid = str(prev.get("card_id") or "")
                            if card_ts > prev_ts:
                                if pid and pid not in superseded_seen_ids:
                                    superseded_seen_ids.add(pid)
                                    superseded_targets.append(prev)
                            elif card_ts < prev_ts:
                                if pid and pid not in newer_seen_ids:
                                    newer_seen_ids.add(pid)
                                    newer_superseding_cards.append(prev)
                            break

            if superseded_targets:
                card["supersedes"] = str(superseded_targets[0]["card_id"])
                card["superseded_card_ids"] = [str(t["card_id"]) for t in superseded_targets]
                for target in superseded_targets:
                    target["status"] = "superseded"
                    superseded_count += 1

            if newer_superseding_cards and card.get("status") == "active":
                card["status"] = "superseded"
                card["supersedes"] = None
                card["superseded_card_ids"] = []
                newer_ordered_asc = sorted(newer_superseding_cards, key=_card_display_sort_key)
                earliest_newer = newer_ordered_asc[0]
                card["superseded_by_card_id"] = str(earliest_newer["card_id"])
                if not earliest_newer.get("supersedes"):
                    earliest_newer["supersedes"] = str(card["card_id"])
                superseded_count += 1

        active_pool.append(card)

    return superseded_count


def resolve_dedup_and_supersedes(
    candidate_cards: list[dict[str, Any]],
    *,
    existing_hashes: set[str] | None = None,
    existing_active_cards: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Deduplicate cards by `content_hash` first, then compute `(ticker, metric, period)` `supersedes`."""
    accepted, deduped_count = deduplicate_candidate_cards(
        candidate_cards,
        existing_hashes=existing_hashes,
    )
    superseded_count = compute_batch_supersedes(
        accepted,
        existing_active_cards=existing_active_cards,
    )
    return accepted, deduped_count, superseded_count


def _extract_error_Inspection(
    res: AssistResult | SegmentExtractionOutcome | BaseException | str,
) -> tuple[bool, int | None, str, str, str, str]:
    """Normalize `AssistResult`, `SegmentExtractionOutcome`, exception, or error string into `(is_ok, status_code, cls_name, status, error_kind, err_text)`."""
    if isinstance(res, AssistResult):
        err_text = str(res.error or "").lower()
        m = re.search(r"(?:http_|status[=_:\s]*)?([45]\d{2})\b", err_text, re.IGNORECASE)
        code = int(m.group(1)) if m else None
        return (
            bool(res.ok),
            code,
            "assistresult",
            str(res.status or "").lower(),
            str(res.error_kind or "").lower(),
            err_text,
        )
    if isinstance(res, SegmentExtractionOutcome):
        err_text = str(res.error or "").lower()
        m = re.search(r"(?:http_|status[=_:\s]*)?([45]\d{2})\b", err_text, re.IGNORECASE)
        code = int(m.group(1)) if m else None
        return (
            bool(res.ok),
            code,
            "segmentextractionoutcome",
            str(res.status or "").lower(),
            str(res.error_kind or "").lower(),
            err_text,
        )
    if isinstance(res, BaseException):
        status_code = getattr(res, "status_code", None) or getattr(
            getattr(res, "response", None), "status_code", None
        )
        if not isinstance(status_code, int):
            m = re.search(
                r"(?:http_|status[=_:\s]*)?([45]\d{2})\b",
                str(res or ""),
                re.IGNORECASE,
            )
            status_code = int(m.group(1)) if m else None
        cls_name = type(res).__name__.lower()
        status = str(getattr(res, "status", "") or "").lower()
        ek = str(getattr(res, "error_kind", "") or "").lower()
        err = f"{cls_name} {res}".lower()
        return False, status_code, cls_name, status, ek, err
    err_str = str(res or "").lower()
    m = re.search(r"(?:http_|status[=_:\s]*)?([45]\d{2})\b", err_str, re.IGNORECASE)
    code = int(m.group(1)) if m else None
    return False, code, "str", "", "", err_str


def is_in_flight_error(
    res: AssistResult | SegmentExtractionOutcome | BaseException | str,
) -> bool:
    """Return True if an extraction result or exception indicates a journal `in_flight` collision."""
    if isinstance(res, SegmentExtractionOutcome) and res.in_flight:
        return True
    is_ok, _status_code, cls_name, status, ek, err = _extract_error_Inspection(res)
    if is_ok:
        return False
    if status == "in_flight" or ek == "in_flight" or cls_name == "journalinflight":
        return True
    for marker in (
        "in_flight",
        "journal state is in_flight",
        "正在由另一个进程执行中",
    ):
        if marker in err:
            return True
    return False


def is_transient_network_error(
    res: AssistResult | SegmentExtractionOutcome | BaseException | str,
) -> bool:
    """Return True for transient network/upstream errors (`RemoteProtocolError`, `ReadError`, `ConnectError`, `ReadTimeout`, HTTP 502/503/504)."""
    if isinstance(res, SegmentExtractionOutcome) and res.transient_error:
        return True
    is_ok, status_code, cls_name, status, ek, err = _extract_error_Inspection(res)
    if (
        is_ok
        or is_permanent_quota_error(res)
        or is_rate_limit_or_quota_error(res)
        or is_in_flight_error(res)
    ):
        return False
    if status == "indeterminate" and ek == "indeterminate":
        return False
    if status_code in (502, 503, 504):
        return True
    if ek in ("stream_cut", "timeout", "http_502", "http_503", "http_504"):
        return True
    if cls_name in (
        "remoteprotocolerror",
        "readerror",
        "connecterror",
        "readtimeout",
        "connecttimeout",
        "writeerror",
        "writetimeout",
        "pooltimeout",
        "protocolerror",
        "timeouterror",
        "connectionerror",
        "connectionreseterror",
    ):
        return True
    for marker in (
        "remoteprotocolerror",
        "peer closed connection",
        "incomplete chunked read",
        "server disconnected",
        "readerror",
        "connecterror",
        "readtimeout",
        "connecttimeout",
        "writetimeout",
        "pooltimeout",
        "timed out",
        "http_502",
        "http_503",
        "http_504",
        "502 bad gateway",
        "503 service unavailable",
        "503 upstream",
        "504 gateway timeout",
    ):
        if marker in err:
            return True
    return False


def is_permanent_quota_error(res: AssistResult | BaseException | str) -> bool:
    """Return True if an `AssistResult` or exception failed due to a permanent/hard quota or billing limit."""
    is_ok, status_code, cls_name, status, ek, err = _extract_error_Inspection(res)
    if is_ok:
        return False
    if cls_name in ("permanentquotaerror", "budgetexhaustederror"):
        return True
    if status == "budget_exhausted" or ek in (
        "budget_exhausted",
        "task_cap_exhausted",
        "budget_db_error",
        "permanent_quota",
    ):
        return True
    if status_code == 402:
        return True
    for marker in (
        "permanent_quota",
        "billing_not_active",
        "billing disabled",
        "account_deactivated",
        "exceeded your current quota, please check your plan and billing",
        "monthly_spend_limit",
        "daily_budget_exhausted",
        "今日现金额度已用完",
    ):
        if marker in err:
            return True
    return False


def is_rate_limit_or_quota_error(res: AssistResult | BaseException | str) -> bool:
    """Return True if an `AssistResult` or exception failed due to transient 429 / rate-limit exhaustion."""
    is_ok, status_code, cls_name, _status, ek, err = _extract_error_Inspection(res)
    if is_ok or is_permanent_quota_error(res):
        return False
    if status_code == 429 or ek == "http_429" or "429" in ek:
        return True
    if any(
        k in cls_name
        for k in ("ratelimit", "rate_limit", "resourceexhausted", "toomanyrequests")
    ):
        return True
    for marker in (
        "429",
        "resource_exhausted",
        "quota",
        "rate limit",
        "rate_limit",
        "too many requests",
        "tokens per",
    ):
        if marker in err:
            return True
    return False


async def extract_segment_cards(
    segment: list[dict[str, Any]],
    *,
    extractor_model: str | None = None,
    prompt_version: str | None = None,
    user_id: str = LOCAL_DEV_USER_ID,
    llm_service: Any = None,
    resend_indeterminate: bool = False,
    persist_raw: bool = False,
    skip_journal: bool = False,
) -> SegmentExtractionOutcome:
    """Extract and validate knowledge cards from one conversation segment via `assist_json`."""
    cfg = get_dalton_settings().conversations
    eff_model = (extractor_model or cfg.extractor_model).strip()
    eff_prompt_ver = (prompt_version or cfg.prompt_version).strip()

    if not segment:
        return SegmentExtractionOutcome(ok=True, model_used=eff_model)

    first_ev = segment[0]
    last_ev = segment[-1]
    agent = str(first_ev.get("agent") or "main")
    session_id = str(first_ev.get("session_id") or "")
    session_key = str(first_ev.get("session_key") or session_id)
    generation = str(first_ev.get("generation") or "")
    channel = str(first_ev.get("channel") or "")
    chat_type = str(first_ev.get("chat_type") or "")
    seq_range = [int(first_ev.get("seq", 0)), int(last_ev.get("seq", 0))]
    all_event_ids = list(
        dict.fromkeys(str(ev.get("event_id") or "") for ev in segment if ev.get("event_id"))
    )

    payload = {
        "agent": agent,
        "session_id": session_id,
        "channel": channel,
        "seq_range": seq_range,
        "events": [
            {
                "event_id": str(ev.get("event_id") or ""),
                "seq": int(ev.get("seq", 0)),
                "ts": str(ev.get("ts") or ""),
                "speaker": ev.get("speaker") or {},
                "text": str(ev.get("text") or ""),
            }
            for ev in segment
        ],
    }

    seg_digest = hashlib.sha256(
        f"{agent}:{session_id}:{seq_range[0]}:{seq_range[1]}:{eff_prompt_ver}".encode("utf-8")
    ).hexdigest()[:20]
    scope_ref = f"conv_extract:{user_id}:{seg_digest}"

    billing_type, _ = resolve_model_billing_and_provider(eff_model)
    eff_resend_indeterminate = bool(resend_indeterminate and billing_type == "oauth")

    res = await assist_json(
        task="conversation.extract_cards",
        instructions=EXTRACTION_SYSTEM_INSTRUCTIONS,
        payload=payload,
        schema=EXTRACTION_RESPONSE_SCHEMA,
        user_id=user_id,
        role="conversation_extract",
        scope_ref=scope_ref,
        timeout_s=180.0,
        model=eff_model,
        llm_service=llm_service,
        resend_indeterminate=eff_resend_indeterminate,
        persist_raw=persist_raw,
        skip_journal=skip_journal,
    )

    usage = res.usage or {}
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    tot_tok = int(usage.get("total_tokens") or (in_tok + out_tok))
    used_model = str(res.model or eff_model)
    is_cached = bool(getattr(res, "cached", False) or getattr(res, "replayed", False))
    is_replayed = bool(getattr(res, "replayed", False) or getattr(res, "cached", False))

    if not res.ok or not isinstance(res.data, dict):
        return SegmentExtractionOutcome(
            ok=False,
            model_used=used_model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=tot_tok,
            rate_limited=is_rate_limit_or_quota_error(res),
            permanent_quota_error=is_permanent_quota_error(res),
            status=str(res.status or ""),
            error_kind=res.error_kind,
            in_flight=is_in_flight_error(res),
            transient_error=is_transient_network_error(res),
            cached=is_cached,
            replayed=is_replayed,
            error=res.error or res.status,
        )

    try:
        normalize_card_enum_fields(res.data)
    except ValueError as exc:
        return SegmentExtractionOutcome(
            ok=False,
            model_used=used_model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=tot_tok,
            status="error",
            error_kind="ValidationError",
            cached=is_cached,
            replayed=is_replayed,
            error=str(exc),
        )

    category = str(res.data.get("category") or "research").strip().lower()
    seg_topic_label = str(res.data.get("topic_label") or "").strip()
    raw_cards = res.data.get("cards") if isinstance(res.data.get("cards"), list) else []

    if category in ("ops", "private"):
        return SegmentExtractionOutcome(
            ok=True,
            category=category,
            cards=[],
            cards_raw_count=len(raw_cards),
            discarded_segment=True,
            model_used=used_model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=tot_tok,
            cached=is_cached,
            replayed=is_replayed,
        )

    valid_cards: list[dict[str, Any]] = []
    quotes_rejected = 0

    for raw_c in raw_cards:
        if not isinstance(raw_c, dict):
            continue
        raw_quote = str(raw_c.get("quote") or "").strip()
        raw_ev_id = str(raw_c.get("quote_event_id") or "").strip()

        quote_ok, matched_ev = verify_verbatim_quote(raw_quote, raw_ev_id, segment)
        if not quote_ok or matched_ev is None:
            quotes_rejected += 1
            continue

        verified_ev_id = str(matched_ev.get("event_id") or raw_ev_id)
        c_hash = compute_content_hash(raw_quote, verified_ev_id)
        card_id = f"cc_{c_hash[:24]}"

        speaker, eff_type, cited_sources = enforce_speaker_and_agent_citation(
            str(raw_c.get("type") or "opinion"),
            raw_c.get("cited_sources") if isinstance(raw_c.get("cited_sources"), list) else [],
            matched_ev,
            segment_events=segment,
        )
        tickers, entities = normalize_tickers_and_entities(
            raw_c.get("tickers") if isinstance(raw_c.get("tickers"), list) else [],
            raw_c.get("entities") if isinstance(raw_c.get("entities"), list) else [],
        )
        sectors = [
            str(s).strip()
            for s in (raw_c.get("sectors") or [])
            if s is not None and str(s).strip()
        ]
        figures: list[dict[str, Any]] = []
        for f in raw_c.get("figures") or []:
            if isinstance(f, dict) and f.get("metric") is not None and f.get("value") is not None:
                figures.append(
                    {
                        "metric": str(f.get("metric") or "").strip(),
                        "value": f.get("value"),
                        "unit": str(f.get("unit") or "").strip(),
                        "period": str(f.get("period") or "").strip(),
                    }
                )

        conf = float(raw_c.get("confidence", 0.8))
        conf = max(0.0, min(1.0, conf))

        ev_ts = str(matched_ev.get("ts") or first_ev.get("ts") or datetime.now(timezone.utc).isoformat())
        ev_date_et = str(matched_ev.get("date_et") or first_ev.get("date_et") or ev_ts[:10])
        topic_label = str(raw_c.get("topic_label") or seg_topic_label or "").strip()
        claim = str(raw_c.get("claim") or "").strip()
        if not claim:
            continue

        card_dict: dict[str, Any] = {
            "card_id": card_id,
            "source_type": "conversation",
            "agent": agent,
            "session_id": session_id,
            "session_key": session_key,
            "generation": str(matched_ev.get("generation") or generation),
            "channel": str(matched_ev.get("channel") or channel),
            "chat_type": str(matched_ev.get("chat_type") or chat_type),
            "event_ids": all_event_ids,
            "seq_range": seq_range,
            "ts": ev_ts,
            "date_et": ev_date_et,
            "speaker": speaker,
            "speaker_role": speaker["role"],
            "tickers": tickers,
            "entities": entities,
            "sectors": sectors,
            "topic_label": topic_label,
            "type": eff_type,
            "claim": claim,
            "figures": figures,
            "quote": raw_quote,
            "quote_event_id": verified_ev_id,
            "cited_sources": cited_sources,
            "confidence": conf,
            "status": "active",
            "supersedes": None,
            "superseded_card_ids": [],
            "extractor_model": used_model,
            "prompt_version": eff_prompt_ver,
            "content_hash": c_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        card_dict["search_text"] = build_card_search_text(card_dict)
        valid_cards.append(card_dict)

    return SegmentExtractionOutcome(
        ok=True,
        category=category,
        cards=valid_cards,
        cards_raw_count=len(raw_cards),
        quotes_rejected=quotes_rejected,
        discarded_segment=False,
        model_used=used_model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        total_tokens=tot_tok,
        cached=is_cached,
        replayed=is_replayed,
    )
