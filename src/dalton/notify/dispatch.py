"""Dispatch personal Feishu Card 2.0 / Discord DM notifications on Dalton automation completion."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import inspect
import logging
import re
import time
from typing import Any
import uuid
from zoneinfo import ZoneInfo

import httpx
from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.dalton.database.verdicts import get_verdict_for_execution
from src.dalton.llm.model_prefs import (
    resolve_dalton_notify_enabled,
    resolve_dalton_notify_overrides,
    resolve_dalton_notify_targets,
)
from src.dalton.settings import (
    get_dalton_bridge_base_url,
    get_dalton_bridge_admin_token,
    get_dalton_frontend_url,
    get_notify_http_timeout_s,
)
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

MAX_CARD_SECTION_CHARS = 320
MAX_CARD_TOTAL_BODY_CHARS = 3000

_EARN_AUTO_RE = re.compile(
    r"^dalton-(?P<ticker>[a-z0-9.]+)-earn-(?P<period>[a-z0-9._-]+)-(?P<stage>t0|tp1|tm3)$",
    re.IGNORECASE,
)
_MOVE_AUTO_RE = re.compile(
    r"^dalton-(?P<ticker>[a-z0-9.]+)-move-(?P<direction>up|down)$",
    re.IGNORECASE,
)
_EXCLUDED_AUTO_RE = re.compile(
    r"^dalton-(?:[a-z0-9.]+-(?:watch|delta)|director-daily|data-health)$",
    re.IGNORECASE,
)

_SENTENCE_SPLIT_RE = re.compile(
    r"[^。！？!?；;\n]+(?:[。！？!?；;]+|\.(?=\s|$)|$)"
)
_MD_HEADING_RE = re.compile(r"^\s*#{1,6}\s*")
_MD_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
_SECTION_HEADING_RE = re.compile(r"^\s*#{2,4}\s+(.+?)\s*$")

VERDICT_TEMPLATE_MAP: dict[str, str] = {
    "THESIS_STRENGTHENED": "green",
    "BULLISH_CONFIRMED": "green",
    "THESIS_WEAKENED": "red",
    "THESIS_BROKEN": "red",
    "THESIS_CHALLENGED": "red",
    "BEARISH_CONFIRMED": "red",
    "NO_CHANGE": "blue",
    "THESIS_INTACT": "blue",
    "NEW_THESIS": "purple",
}

CATEGORY_BUSINESS_TAG_MAP: dict[str, tuple[str, str]] = {
    "earnings_t0": ("财报", "blue"),
    "earnings_tp1": ("财报", "blue"),
    "price_move": ("异动", "orange"),
    "earnings_tm3": ("前瞻", "purple"),
    "director_weekly": ("周报", "indigo"),
    "earnings_calendar": ("周报", "indigo"),
}


@dataclass(frozen=True)
class NotifyClassification:
    """Classification result for an automation eligible for personal DM push."""

    category: str
    label_zh: str
    scope: str
    requires_non_no_change_verdict: bool = False


def _resolve_ticker_label(automation: Mapping[str, Any], fallback_slug: str) -> str:
    meta = automation.get("metadata")
    if isinstance(meta, Mapping):
        raw_ticker = str(meta.get("ticker") or "").strip().upper()
        if raw_ticker:
            return raw_ticker
    return fallback_slug.strip().upper()


def classify_automation_for_notify(
    automation: Mapping[str, Any] | None,
) -> NotifyClassification | None:
    """Classify ``automation`` into one of the 4 push categories, or ``None`` if excluded."""
    if not isinstance(automation, Mapping):
        return None

    name = str(automation.get("name") or "").strip()
    lowered_name = name.lower()

    if lowered_name and _EXCLUDED_AUTO_RE.match(lowered_name):
        return None

    m_earn = _EARN_AUTO_RE.match(lowered_name)
    if m_earn:
        ticker = _resolve_ticker_label(automation, m_earn.group("ticker"))
        stage = m_earn.group("stage").lower()
        if stage == "t0":
            return NotifyClassification(
                category="earnings_t0",
                label_zh="财报 T0 快讯",
                scope=ticker,
            )
        if stage == "tp1":
            return NotifyClassification(
                category="earnings_tp1",
                label_zh="财报 T+1 深度",
                scope=ticker,
            )
        if stage == "tm3":
            return NotifyClassification(
                category="earnings_tm3",
                label_zh="财报 T-3 前瞻",
                scope=ticker,
            )

    m_move = _MOVE_AUTO_RE.match(lowered_name)
    if m_move:
        ticker = _resolve_ticker_label(automation, m_move.group("ticker"))
        return NotifyClassification(
            category="price_move",
            label_zh="重大股价异动",
            scope=ticker,
            requires_non_no_change_verdict=True,
        )

    if lowered_name == "dalton-director-weekly":
        return NotifyClassification(
            category="director_weekly",
            label_zh="每周复盘",
            scope="Home",
        )

    if lowered_name == "dalton-earnings-calendar":
        return NotifyClassification(
            category="earnings_calendar",
            label_zh="财报日历周报",
            scope="Home",
        )

    meta = automation.get("metadata")
    if isinstance(meta, Mapping):
        kind = str(meta.get("dalton_kind") or "").strip().lower()
        ticker = _resolve_ticker_label(automation, "Home")
        if kind == "earnings_t0":
            return NotifyClassification("earnings_t0", "财报 T0 快讯", ticker)
        if kind == "earnings_tp1":
            return NotifyClassification("earnings_tp1", "财报 T+1 深度", ticker)
        if kind == "earnings_tm3":
            return NotifyClassification("earnings_tm3", "财报 T-3 前瞻", ticker)
        if kind == "price_move":
            return NotifyClassification(
                "price_move",
                "重大股价异动",
                ticker,
                requires_non_no_change_verdict=True,
            )
        if kind == "director_weekly":
            return NotifyClassification("director_weekly", "每周复盘", "Home")
        if kind == "calendar":
            return NotifyClassification("earnings_calendar", "财报日历周报", "Home")

    return None


def resolve_automation_notify_state(
    automation: Mapping[str, Any] | None,
    overrides: Mapping[str, bool] | None = None,
) -> tuple[str | None, bool, str | None]:
    """Return ``(category, effective_enabled, source)`` using unified key precedence.

    Precedence:
    1. Exact task name ``name`` -> ``source="task"``
    2. Lowercase task name ``name.lower()`` -> ``source="task"``
    3. Category key ``f"category:{category}"`` -> ``source="category"``
    4. Bare category key ``category`` -> ``source="category"``
    5. Default ``True`` for the 4 pushable categories -> ``source="default"``
    """
    classification = classify_automation_for_notify(automation)
    if classification is None:
        return (None, False, None)
    if isinstance(overrides, Mapping) and isinstance(automation, Mapping):
        name = str(automation.get("name") or "").strip()
        for candidate_key, source in (
            (name, "task"),
            (name.lower(), "task"),
            (f"category:{classification.category}", "category"),
            (classification.category, "category"),
        ):
            if candidate_key and candidate_key in overrides:
                val = overrides[candidate_key]
                if isinstance(val, bool):
                    return (classification.category, val, source)
    return (classification.category, True, "default")


def is_automation_notify_enabled(
    automation: Mapping[str, Any] | None,
    overrides: Mapping[str, bool] | None = None,
) -> bool:
    """Resolve whether per-automation notification is enabled."""
    return resolve_automation_notify_state(automation, overrides)[1]


async def populate_automation_notify_state(
    user_id: str | None,
    responses: Sequence[Any],
    *,
    resolve_overrides_fn: Callable[[str | None], Awaitable[dict[str, bool]]] = resolve_dalton_notify_overrides,
) -> None:
    """Populate ``dalton_notify_category``, ``dalton_notify_effective``, and ``dalton_notify_source`` on response models."""
    if not responses:
        return
    try:
        overrides = await resolve_overrides_fn(user_id) if user_id else {}
    except Exception:
        overrides = {}
    for resp in responses:
        name = getattr(resp, "name", None)
        metadata = getattr(resp, "metadata", None)
        cat, eff, src = resolve_automation_notify_state(
            {"name": name, "metadata": metadata},
            overrides,
        )
        setattr(resp, "dalton_notify_category", cat)
        setattr(resp, "dalton_notify_effective", eff if cat is not None else None)
        setattr(resp, "dalton_notify_source", src)


def _clean_plain_line(raw: str) -> str:
    line = _MD_HEADING_RE.sub("", raw.strip())
    line = _MD_BULLET_RE.sub("", line)
    line = line.replace("**", "").replace("__", "").replace("`", "")
    return re.sub(r"\s+", " ", line).strip()


def _ends_with_sentence_punct(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped:
        return False
    return stripped[-1] in ("。", "！", "？", "!", "?", "；", ";", ".")


def extract_summary_lines(
    text: str | None,
    *,
    min_lines: int = 3,
    max_lines: int = 6,
    max_chars: int = MAX_CARD_SECTION_CHARS,
) -> list[str]:
    """Extract 3-6 summary lines from the start of ``text``, truncated at sentence boundaries."""
    _ = min_lines
    if not text or not str(text).strip():
        return []

    raw_lines = str(text).splitlines()
    sentences: list[str] = []
    for raw_line in raw_lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("```") or re.match(r"^\|?[-:\s|]+\|?$", stripped):
            continue
        cleaned_line = _clean_plain_line(stripped)
        if not cleaned_line:
            continue
        parts = [p.strip() for p in _SENTENCE_SPLIT_RE.findall(cleaned_line) if p.strip()]
        if not parts:
            continue
        for idx, part in enumerate(parts):
            is_last_in_line = idx == len(parts) - 1
            if is_last_in_line and not _ends_with_sentence_punct(part) and len(parts) > 1:
                continue
            sentences.append(part)

    if not sentences:
        return []

    selected: list[str] = []
    total_chars = 0
    for sent in sentences:
        if len(selected) >= max_lines:
            break
        if selected and total_chars + len(sent) > max_chars:
            break
        selected.append(sent)
        total_chars += len(sent)

    return selected[:max_lines]


def _truncate_section_body(
    raw_body: str,
    *,
    max_chars: int = MAX_CARD_SECTION_CHARS,
) -> str:
    """Truncate a structured ``##`` section body to ``max_chars`` at sentence/line boundaries."""
    body = raw_body.strip()
    if not body or max_chars <= 0:
        return ""
    if len(body) <= max_chars:
        return body

    kept_lines: list[str] = []
    used = 0
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        sep = 1 if kept_lines else 0
        if used + sep + len(line) <= max_chars:
            kept_lines.append(line)
            used += sep + len(line)
            continue

        # Truncate within this line at sentence boundaries
        parts = [p.strip() for p in _SENTENCE_SPLIT_RE.findall(line) if p.strip()]
        line_chunks: list[str] = []
        for part in parts:
            candidate = "".join(line_chunks) + part
            if used + sep + len(candidate) <= max_chars:
                line_chunks.append(part)
            else:
                break
        if line_chunks:
            joined = "".join(line_chunks)
            kept_lines.append(joined)
            used += sep + len(joined)
        elif not kept_lines:
            kept_lines.append(line[:max_chars].rstrip())
        break

    return "\n".join(kept_lines).strip()


def format_notify_message(
    classification: NotifyClassification,
    *,
    verdict_word: str | None = None,
    final_text: str | None = None,
    thread_id: str | None = None,
    frontend_base_url: str | None = None,
    max_summary_chars: int = MAX_CARD_SECTION_CHARS,
) -> str:
    """Format the Chinese plain-text notification message (used for Discord DMs)."""
    lines: list[str] = [f"{classification.scope} · {classification.label_zh}"]

    cleaned_word = (verdict_word or "").strip()
    if cleaned_word:
        lines.append(f"结论：{cleaned_word}")

    summary_lines = extract_summary_lines(
        final_text,
        min_lines=3,
        max_lines=6,
        max_chars=max_summary_chars,
    )
    lines.extend(summary_lines)

    resolved_base = (
        get_dalton_frontend_url()
        if frontend_base_url is None
        else str(frontend_base_url).strip().rstrip("/")
    )
    cleaned_tid = (thread_id or "").strip()
    if resolved_base and cleaned_tid:
        lines.append(f"{resolved_base}/chat/t/{cleaned_tid}")

    return "\n".join(lines)


def map_verdict_to_header_template(verdict_word: str | None) -> str:
    """Map verdict word to Feishu Card 2.0 header template color."""
    cleaned = (verdict_word or "").strip().upper()
    if not cleaned:
        return "indigo"
    return VERDICT_TEMPLATE_MAP.get(cleaned, "indigo")


def map_category_to_business_tag(category: str) -> tuple[str, str]:
    """Return ``(tag_label, tag_color)`` for a pushable automation category."""
    return CATEGORY_BUSINESS_TAG_MAP.get(category, ("研究", "indigo"))


def _normalize_card_section_name(raw_heading: str) -> str | None:
    cleaned = re.sub(r"^[📝📌🔍💡\s]+", "", raw_heading.strip())
    if cleaned in ("结论", "核心结论", "总结"):
        return "结论"
    if cleaned in ("要点", "核心要点", "关键变化"):
        return "要点"
    if cleaned in ("下一步验证", "后续验证", "待验证", "下一步观察"):
        return "下一步验证"
    if cleaned in ("来源", "信源", "数据源", "信源与数据口径"):
        return "来源"
    return None


def extract_card_sections(
    final_text: str | None,
    *,
    classification: NotifyClassification | None = None,
    max_summary_chars: int = MAX_CARD_SECTION_CHARS,
    max_total_chars: int = MAX_CARD_TOTAL_BODY_CHARS,
) -> list[tuple[str, str]]:
    """Extract structured ``(section_title, section_body)`` pairs for Feishu Card 2.0 body.

    Only uses sentences that actually appear in ``final_text``—never fabricates
    fallback sentences or rewrites sources. Each section is capped at ``max_summary_chars``
    (default 320) and total body text across the card is capped at ``max_total_chars``
    (default 3000).
    """
    _ = classification
    if not final_text or not str(final_text).strip():
        return []

    raw_lines = str(final_text).strip().splitlines()
    buckets: dict[str, list[str]] = {}
    current_key: str | None = None
    for line in raw_lines:
        m = _SECTION_HEADING_RE.match(line)
        if m:
            norm = _normalize_card_section_name(m.group(1))
            current_key = norm
            if norm and norm not in buckets:
                buckets[norm] = []
            continue
        if current_key is not None:
            buckets[current_key].append(line)

    canonical_order = ("结论", "要点", "下一步验证", "来源")
    if any(buckets.get(k) for k in canonical_order):
        result: list[tuple[str, str]] = []
        total_used = 0
        for key in canonical_order:
            remaining = max_total_chars - total_used
            if remaining <= 0:
                break
            raw_sec = "\n".join(buckets.get(key) or []).strip()
            if not raw_sec:
                continue
            sec_cap = min(max_summary_chars, remaining)
            truncated = _truncate_section_body(raw_sec, max_chars=sec_cap)
            if truncated:
                result.append((key, truncated))
                total_used += len(truncated)
        return result

    summary_lines = extract_summary_lines(
        final_text,
        min_lines=3,
        max_lines=6,
        max_chars=min(max_summary_chars, max_total_chars),
    )
    if not summary_lines:
        return []

    unstructured: list[tuple[str, str]] = [
        ("结论", _truncate_section_body(summary_lines[0], max_chars=max_summary_chars))
    ]
    if len(summary_lines) == 2:
        pts = _truncate_section_body(f"- {summary_lines[1]}", max_chars=max_summary_chars)
        if pts:
            unstructured.append(("要点", pts))
    elif len(summary_lines) >= 3:
        pts_raw = "\n".join(f"- {pt}" for pt in summary_lines[1:-1])
        pts = _truncate_section_body(pts_raw, max_chars=max_summary_chars)
        if pts:
            unstructured.append(("要点", pts))
        nxt = _truncate_section_body(summary_lines[-1], max_chars=max_summary_chars)
        if nxt:
            unstructured.append(("下一步验证", nxt))

    bounded: list[tuple[str, str]] = []
    total_used = 0
    for key, body in unstructured:
        if not body:
            continue
        remaining = max_total_chars - total_used
        if remaining <= 0:
            break
        trimmed = body if len(body) <= remaining else _truncate_section_body(body, max_chars=remaining)
        if trimmed:
            bounded.append((key, trimmed))
            total_used += len(trimmed)
    return bounded


def build_feishu_notify_card(
    classification: NotifyClassification,
    *,
    verdict_word: str | None = None,
    final_text: str | None = None,
    thread_id: str | None = None,
    frontend_base_url: str | None = None,
    timestamp_str: str | None = None,
    max_summary_chars: int = MAX_CARD_SECTION_CHARS,
    max_total_chars: int = MAX_CARD_TOTAL_BODY_CHARS,
) -> dict[str, Any]:
    """Build Feishu Card Schema 2.0 dictionary for a Dalton automation notification."""
    cleaned_word = (verdict_word or "").strip()
    ts_label = (timestamp_str or "").strip()
    if not ts_label:
        ts_label = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")

    subtitle_text = (
        f"{ts_label} · 结论：{cleaned_word}" if cleaned_word else ts_label
    )
    tag_text, tag_color = map_category_to_business_tag(classification.category)

    header: dict[str, Any] = {
        "title": {
            "tag": "plain_text",
            "content": f"{classification.scope} · {classification.label_zh}",
        },
        "subtitle": {
            "tag": "plain_text",
            "content": subtitle_text,
        },
        "template": map_verdict_to_header_template(cleaned_word),
        "text_tag_list": [
            {
                "tag": "text_tag",
                "text": {"tag": "plain_text", "content": tag_text},
                "color": tag_color,
            }
        ],
    }

    sections = extract_card_sections(
        final_text,
        classification=classification,
        max_summary_chars=max_summary_chars,
        max_total_chars=max_total_chars,
    )
    elements: list[dict[str, Any]] = []
    for sec_title, sec_body in sections:
        if sec_title == "来源":
            if elements and elements[-1].get("tag") != "hr":
                elements.append({"tag": "hr"})
            notation_lines = [
                f"<font color='grey'>{line}</font>" if line.strip() else ""
                for line in f"**来源**\n{sec_body}".splitlines()
            ]
            elements.append(
                {
                    "tag": "markdown",
                    "text_size": "notation",
                    "content": "\n".join(notation_lines),
                }
            )
        else:
            if elements and elements[-1].get("tag") != "hr":
                elements.append({"tag": "hr"})
            elements.append(
                {
                    "tag": "markdown",
                    "content": f"**{sec_title}**\n{sec_body}",
                }
            )

    resolved_base = (
        get_dalton_frontend_url()
        if frontend_base_url is None
        else str(frontend_base_url).strip().rstrip("/")
    )
    cleaned_tid = (thread_id or "").strip()
    if resolved_base and cleaned_tid:
        if elements and elements[-1].get("tag") != "hr":
            elements.append({"tag": "hr"})
        target_url = f"{resolved_base}/chat/t/{cleaned_tid}"
        elements.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "在 Dalton 中打开"},
                "type": "primary",
                "behaviors": [{"type": "open_url", "default_url": target_url}],
            }
        )

    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
            "update_multi": True,
        },
        "header": header,
        "body": {
            "direction": "vertical",
            "vertical_spacing": "8px",
            "elements": elements,
        },
    }


def _split_target_channel(raw_target: str) -> tuple[str, str]:
    cleaned = (raw_target or "").strip()
    if ":" in cleaned:
        channel = cleaned.split(":", 1)[0].strip().lower()
        return (channel or "unknown", cleaned)
    return ("unknown", cleaned)


async def evaluate_user_notify_eligibility(
    user_id: str | None,
    automation: Mapping[str, Any] | None,
    *,
    target: str | None = None,
    resolve_enabled_fn: Callable[[str | None], Awaitable[bool]] = resolve_dalton_notify_enabled,
    resolve_overrides_fn: Callable[[str | None], Awaitable[dict[str, bool]]] = resolve_dalton_notify_overrides,
    resolve_targets_fn: Callable[[str | None], Awaitable[list[str]]] = resolve_dalton_notify_targets,
) -> tuple[bool, str | None, list[str]]:
    """Evaluate master switch, per-task override, and target configuration for ``user_id``.

    Shared by both initial settlement dispatch and retry so both paths enforce
    identical rules. Returns ``(allowed, skip_reason, configured_targets)`` where
    ``skip_reason`` is ``"toggle_off"`` or ``"target_removed"`` when ``allowed`` is ``False``.
    """
    cleaned_uid = str(user_id or "").strip()
    if not cleaned_uid:
        return (False, "toggle_off", [])

    if not await resolve_enabled_fn(cleaned_uid):
        return (False, "toggle_off", [])

    overrides = await resolve_overrides_fn(cleaned_uid)
    if (
        isinstance(automation, Mapping)
        and classify_automation_for_notify(automation) is not None
        and not is_automation_notify_enabled(automation, overrides)
    ):
        return (False, "toggle_off", [])

    targets = await resolve_targets_fn(cleaned_uid)
    if not targets:
        return (False, "target_removed", [])

    normalized_targets = [
        _split_target_channel(t)[1] for t in targets if str(t).strip()
    ]
    if not normalized_targets:
        return (False, "target_removed", [])

    if target is not None:
        cleaned_target = _split_target_channel(target)[1]
        if cleaned_target not in normalized_targets:
            return (False, "target_removed", normalized_targets)

    return (True, None, normalized_targets)


async def claim_target_delivery(
    execution_id: str,
    channel: str,
    target: str,
    *,
    user_id: str = "",
    automation_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
    stale_pending_minutes: int = 10,
) -> bool:
    """Atomically claim or retry a ``(execution_id, channel, target)`` row in ``dalton_notify_deliveries``.

    Returns ``True`` when the row is newly inserted as ``pending`` or transitioned from
    ``failed`` (or stale ``pending`` older than ``stale_pending_minutes``) with
    ``attempt_count < 3`` to ``pending``.
    """
    exec_id = (execution_id or "").strip()
    ch = (channel or "").strip().lower()
    tgt = (target or "").strip()
    if not (exec_id and ch and tgt):
        return False

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_notify_deliveries (
                    delivery_id, execution_id, automation_id, user_id,
                    channel, target, status, attempt_count, payload_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', 1, %s)
                ON CONFLICT (execution_id, channel, target) DO UPDATE
                SET status = 'pending',
                    attempt_count = dalton_notify_deliveries.attempt_count + 1,
                    payload_json = EXCLUDED.payload_json,
                    updated_at = NOW()
                WHERE (
                    dalton_notify_deliveries.status = 'failed'
                    OR (
                        dalton_notify_deliveries.status = 'pending'
                        AND dalton_notify_deliveries.updated_at
                            <= NOW() - make_interval(mins => %s)
                    )
                )
                  AND dalton_notify_deliveries.attempt_count < 3
                RETURNING delivery_id
                """,
                (
                    str(uuid.uuid4()),
                    exec_id,
                    automation_id,
                    user_id,
                    ch,
                    tgt,
                    Json(dict(payload or {})),
                    stale_pending_minutes,
                ),
            )
            row = await cur.fetchone()
            return row is not None


async def mark_target_delivery(
    execution_id: str,
    channel: str,
    target: str,
    *,
    status: str,
    error_code: str | None = None,
    reason: str | None = None,
    expected_status: str | Sequence[str] | None = None,
) -> bool:
    """Update delivery status (``pending``, ``sent``, ``failed``, or ``skipped``) in ``dalton_notify_deliveries``.

    When ``expected_status`` is provided as a single status (e.g. ``'pending'``) or a
    sequence of statuses (e.g. ``('failed', 'pending')``), the ``UPDATE`` includes
    ``AND status = %s`` or ``AND status IN (...)`` so a completed ``sent`` row is never
    overwritten. Returns ``True`` iff a row was updated.
    """
    exec_id = (execution_id or "").strip()
    ch = (channel or "").strip().lower()
    tgt = (target or "").strip()
    if not (exec_id and ch and tgt) or status not in (
        "pending",
        "sent",
        "failed",
        "skipped",
    ):
        return False

    effective_code = reason if reason is not None else error_code
    exp_statuses: tuple[str, ...] = ()
    if isinstance(expected_status, str):
        cleaned = expected_status.strip().lower()
        if cleaned:
            exp_statuses = (cleaned,)
    elif isinstance(expected_status, Sequence):
        exp_statuses = tuple(
            str(s).strip().lower() for s in expected_status if str(s).strip()
        )

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if len(exp_statuses) == 1:
                await cur.execute(
                    """
                    UPDATE dalton_notify_deliveries
                    SET status = %s,
                        error_code = %s,
                        updated_at = NOW()
                    WHERE execution_id = %s
                      AND channel = %s
                      AND target = %s
                      AND status = %s
                    """,
                    (status, effective_code, exec_id, ch, tgt, exp_statuses[0]),
                )
            elif len(exp_statuses) > 1:
                placeholders = ", ".join(["%s"] * len(exp_statuses))
                await cur.execute(
                    f"""
                    UPDATE dalton_notify_deliveries
                    SET status = %s,
                        error_code = %s,
                        updated_at = NOW()
                    WHERE execution_id = %s
                      AND channel = %s
                      AND target = %s
                      AND status IN ({placeholders})
                    """,
                    (status, effective_code, exec_id, ch, tgt, *exp_statuses),
                )
            else:
                await cur.execute(
                    """
                    UPDATE dalton_notify_deliveries
                    SET status = %s,
                        error_code = %s,
                        updated_at = NOW()
                    WHERE execution_id = %s
                      AND channel = %s
                      AND target = %s
                    """,
                    (status, effective_code, exec_id, ch, tgt),
                )
            updated_rows = (
                cur.rowcount if isinstance(cur.rowcount, int) else 0
            )
            return updated_rows > 0


async def _invoke_mark_delivery(
    mark_fn: Callable[..., Awaitable[Any]],
    execution_id: str,
    channel: str,
    target: str,
    *,
    status: str,
    error_code: str | None = None,
    reason: str | None = None,
    expected_status: str | Sequence[str] | None = None,
) -> bool:
    effective_code = reason if reason is not None else error_code
    kwargs: dict[str, Any] = {
        "status": status,
        "error_code": effective_code,
    }
    try:
        sig = inspect.signature(mark_fn)
        has_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in sig.parameters.values()
        )
        if reason is not None and ("reason" in sig.parameters or has_var_kw):
            kwargs["reason"] = reason
        if expected_status is not None and (
            "expected_status" in sig.parameters or has_var_kw
        ):
            kwargs["expected_status"] = expected_status
    except (ValueError, TypeError):
        pass
    res = await mark_fn(
        execution_id,
        channel,
        target,
        **kwargs,
    )
    return res if isinstance(res, bool) else True


async def _shielded_mark_failed(
    mark_fn: Callable[..., Awaitable[Any]],
    execution_id: str,
    channel: str,
    target: str,
    *,
    error_code: str = "CancelledError",
    reason: str | None = None,
    expected_status: str | Sequence[str] | None = "pending",
    timeout_s: float = 3.0,
) -> None:
    """Mark a claimed row ``failed`` under ``asyncio.shield`` with a ``timeout_s`` deadline."""
    try:
        await asyncio.wait_for(
            asyncio.shield(
                _invoke_mark_delivery(
                    mark_fn,
                    execution_id,
                    channel,
                    target,
                    status="failed",
                    error_code=error_code,
                    reason=reason,
                    expected_status=expected_status,
                )
            ),
            timeout=timeout_s,
        )
    except (Exception, asyncio.CancelledError):
        pass


async def claim_execution_notify(
    execution_id: str,
    channel: str = "all",
    target: str = "all",
) -> bool:
    """Claim delivery for ``(execution_id, channel, target)`` in ``dalton_notify_deliveries``."""
    return await claim_target_delivery(execution_id, channel, target)


async def post_bridge_notify(
    targets: list[str],
    text: str,
    card: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call the host bridge ``POST /notify`` endpoint using configured bridge URL and token.

    HTTP timeout is derived as ``get_feishu_send_card_timeout_s() + 5.0`` (``get_notify_http_timeout_s()``).
    Never logs response bodies or bridge tokens; returns structured status on non-2xx or invalid JSON.
    """
    base_url = get_dalton_bridge_base_url().rstrip("/")
    token = get_dalton_bridge_admin_token()  # DALTON-HOOK(bridge-admin-token): /notify is an admin route
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Dalton-Bridge-Token"] = token

    body: dict[str, Any] = {"targets": list(targets), "text": text}
    if card is not None:
        body["card"] = card

    http_timeout = get_notify_http_timeout_s()
    async with httpx.AsyncClient(
        timeout=http_timeout, trust_env=False
    ) as client:
        resp = await client.post(
            f"{base_url}/notify",
            json=body,
            headers=headers,
        )
        if not resp.is_success:
            return {
                "ok": False,
                "status_code": resp.status_code,
                "error_code": f"http_{resp.status_code}",
                "results": [],
            }
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            return {
                "ok": False,
                "status_code": resp.status_code,
                "error_code": "invalid_json",
                "results": [],
            }
        if not isinstance(data, dict):
            return {
                "ok": False,
                "status_code": resp.status_code,
                "error_code": "invalid_shape",
                "results": [],
            }
        return data


async def _invoke_post_notify(
    post_notify_fn: Callable[..., Awaitable[dict[str, Any]]],
    targets: list[str],
    text: str,
    card: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        sig = inspect.signature(post_notify_fn)
        if "card" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        ):
            return await post_notify_fn(targets, text, card=card)
    except (ValueError, TypeError):
        pass
    return await post_notify_fn(targets, text)


def _evaluate_target_results(
    claimed_targets: Sequence[str],
    bridge_res: Any,
) -> tuple[dict[str, bool], str | None]:
    """Evaluate per-target delivery outcome from ``bridge_res``.

    If ``bridge_res`` is not a mapping, has non-2xx ``status_code``, or lacks a valid
    ``results`` list, every claimed target is marked ``False``.
    """
    outcome: dict[str, bool] = {t: False for t in claimed_targets}
    if not isinstance(bridge_res, Mapping):
        return outcome, "invalid_response"

    status_code = bridge_res.get("status_code")
    if isinstance(status_code, int) and status_code >= 400:
        return outcome, f"http_{status_code}"

    results = bridge_res.get("results")
    if not isinstance(results, list) or not results:
        err_code = str(bridge_res.get("error_code") or "missing_results")
        return outcome, err_code

    by_target: dict[str, bool] = {}
    for item in results:
        if isinstance(item, Mapping):
            t_str = str(item.get("target") or "").strip()
            if t_str:
                by_target[t_str] = item.get("ok") is True

    for t in claimed_targets:
        outcome[t] = by_target.get(t, False)

    return outcome, None


def _reconstruct_automation_from_delivery_row(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    auto_name = (
        str(row.get("automation_name") or "").strip()
        or str(payload.get("automation_name") or "").strip()
    )
    raw_meta = row.get("automation_metadata")
    meta: dict[str, Any] = dict(raw_meta) if isinstance(raw_meta, Mapping) else {}
    if not meta and payload.get("category"):
        meta = {"dalton_kind": str(payload.get("category"))}
    if auto_name or meta:
        return {"name": auto_name, "metadata": meta}
    return None


async def retry_failed_notify_deliveries(
    *,
    post_notify_fn: Callable[..., Awaitable[dict[str, Any]]] = post_bridge_notify,
    mark_delivery_fn: Callable[..., Awaitable[Any]] = mark_target_delivery,
    resolve_enabled_fn: Callable[[str | None], Awaitable[bool]] = resolve_dalton_notify_enabled,
    resolve_overrides_fn: Callable[[str | None], Awaitable[dict[str, bool]]] = resolve_dalton_notify_overrides,
    resolve_targets_fn: Callable[[str | None], Awaitable[list[str]]] = resolve_dalton_notify_targets,
    max_attempts: int = 3,
    lookback_hours: int = 24,
    stale_pending_minutes: int = 10,
    limit: int = 5,
    max_duration_s: float = 30.0,
) -> int:
    """Scan ``dalton_notify_deliveries`` for ``failed`` or stale ``pending`` (>10m) rows and retry them.

    Re-checks user master switch, per-task override, and target configuration before
    re-sending; marks rows whose conditions no longer hold as ``skipped`` with
    ``reason`` = ``toggle_off`` or ``target_removed``. Processes at most ``limit``
    rows within ``max_duration_s`` seconds. 会传播 ``CancelledError``，其他异常不抛出。
    """
    sent_count = 0
    started_at = time.monotonic()
    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT d.execution_id, d.automation_id, d.user_id,
                           d.channel, d.target, d.status, d.attempt_count,
                           d.payload_json,
                           a.name AS automation_name,
                           a.metadata AS automation_metadata
                    FROM dalton_notify_deliveries d
                    LEFT JOIN automations a
                      ON a.automation_id::text = d.automation_id
                    WHERE (
                        (d.status = 'failed' AND d.attempt_count < %s)
                        OR (
                            d.status = 'pending'
                            AND d.updated_at <= NOW() - make_interval(mins => %s)
                        )
                    )
                      AND d.created_at >= NOW() - make_interval(hours => %s)
                    ORDER BY d.created_at ASC
                    LIMIT %s
                    """,
                    (max_attempts, stale_pending_minutes, lookback_hours, limit),
                )
                candidates = await cur.fetchall()

        for row in candidates or []:
            if time.monotonic() - started_at >= max_duration_s:
                break

            exec_id = str(row.get("execution_id") or "").strip()
            ch = str(row.get("channel") or "").strip().lower()
            tgt = str(row.get("target") or "").strip()
            user_id = str(row.get("user_id") or "").strip()
            try:
                attempt_count = int(row.get("attempt_count") or 0)
            except (TypeError, ValueError):
                attempt_count = 0
            payload = (
                row.get("payload_json")
                if isinstance(row.get("payload_json"), Mapping)
                else {}
            )
            if not (exec_id and ch and tgt):
                continue

            row_claimed = False
            try:
                if attempt_count >= max_attempts:
                    async with get_db_connection() as conn:
                        async with conn.cursor(row_factory=dict_row) as cur:
                            await cur.execute(
                                """
                                UPDATE dalton_notify_deliveries
                                SET status = 'failed',
                                    error_code = 'attempts_exhausted',
                                    updated_at = NOW()
                                WHERE execution_id = %s
                                  AND channel = %s
                                  AND target = %s
                                  AND status = 'pending'
                                  AND attempt_count >= %s
                                  AND updated_at <= NOW() - make_interval(mins => %s)
                                """,
                                (
                                    exec_id,
                                    ch,
                                    tgt,
                                    max_attempts,
                                    stale_pending_minutes,
                                ),
                            )
                            updated_rows = (
                                cur.rowcount
                                if isinstance(cur.rowcount, int)
                                else 0
                            )
                    if (
                        updated_rows > 0
                        and mark_delivery_fn is not mark_target_delivery
                    ):
                        await _invoke_mark_delivery(
                            mark_delivery_fn,
                            exec_id,
                            ch,
                            tgt,
                            status="failed",
                            reason="attempts_exhausted",
                            error_code="attempts_exhausted",
                            expected_status="pending",
                        )
                    continue

                automation_info = _reconstruct_automation_from_delivery_row(
                    row, payload
                )
                allowed, skip_reason, _ = await evaluate_user_notify_eligibility(
                    user_id,
                    automation_info,
                    target=tgt,
                    resolve_enabled_fn=resolve_enabled_fn,
                    resolve_overrides_fn=resolve_overrides_fn,
                    resolve_targets_fn=resolve_targets_fn,
                )
                if not allowed:
                    skipped_updated = await _invoke_mark_delivery(
                        mark_delivery_fn,
                        exec_id,
                        ch,
                        tgt,
                        status="skipped",
                        reason=skip_reason or "toggle_off",
                        error_code=skip_reason or "toggle_off",
                        expected_status=("failed", "pending"),
                    )
                    if skipped_updated:
                        logger.info(
                            "[DALTON_NOTIFY] Skipped retry delivery for execution_id=%s (reason=%s)",
                            exec_id,
                            skip_reason or "toggle_off",
                        )
                    continue

                async with get_db_connection() as conn:
                    async with conn.cursor(row_factory=dict_row) as cur:
                        await cur.execute(
                            """
                            UPDATE dalton_notify_deliveries
                            SET status = 'pending',
                                attempt_count = attempt_count + 1,
                                updated_at = NOW()
                            WHERE execution_id = %s
                              AND channel = %s
                              AND target = %s
                              AND (
                                  status = 'failed'
                                  OR (
                                      status = 'pending'
                                      AND updated_at <= NOW() - make_interval(mins => %s)
                                  )
                              )
                              AND attempt_count < %s
                            RETURNING attempt_count
                            """,
                            (exec_id, ch, tgt, stale_pending_minutes, max_attempts),
                        )
                        claimed_row = await cur.fetchone()
                        if claimed_row is not None:
                            row_claimed = True
                if not row_claimed:
                    continue

                text = str(payload.get("text") or "")
                card = (
                    payload.get("card")
                    if isinstance(payload.get("card"), dict)
                    else None
                )
                remaining_s = max(0.1, max_duration_s - (time.monotonic() - started_at))
                bridge_res = await asyncio.wait_for(
                    _invoke_post_notify(post_notify_fn, [tgt], text, card),
                    timeout=remaining_s,
                )
                outcomes, batch_err = _evaluate_target_results([tgt], bridge_res)
                if outcomes.get(tgt) is True:
                    await _invoke_mark_delivery(
                        mark_delivery_fn,
                        exec_id,
                        ch,
                        tgt,
                        status="sent",
                        error_code=None,
                        expected_status="pending",
                    )
                    sent_count += 1
                else:
                    await _invoke_mark_delivery(
                        mark_delivery_fn,
                        exec_id,
                        ch,
                        tgt,
                        status="failed",
                        error_code=batch_err or "target_failed",
                        expected_status="pending",
                    )
                row_claimed = False
            except asyncio.CancelledError:
                if row_claimed:
                    await _shielded_mark_failed(
                        mark_delivery_fn,
                        exec_id,
                        ch,
                        tgt,
                        error_code="CancelledError",
                        expected_status="pending",
                    )
                    row_claimed = False
                raise
            except Exception as retry_exc:
                status_code = getattr(
                    getattr(retry_exc, "response", None), "status_code", None
                )
                err_code = (
                    f"http_{status_code}"
                    if status_code
                    else type(retry_exc).__name__
                )
                if row_claimed:
                    try:
                        await _invoke_mark_delivery(
                            mark_delivery_fn,
                            exec_id,
                            ch,
                            tgt,
                            status="failed",
                            error_code=err_code,
                            expected_status="pending",
                        )
                    except asyncio.CancelledError:
                        await _shielded_mark_failed(
                            mark_delivery_fn,
                            exec_id,
                            ch,
                            tgt,
                            error_code=err_code,
                            expected_status="pending",
                        )
                        row_claimed = False
                        raise
                    except Exception:
                        pass
                    row_claimed = False
                logger.warning(
                    "[DALTON_NOTIFY] Retry row failed for execution_id=%s: error_type=%s",
                    exec_id,
                    type(retry_exc).__name__,
                )
    except Exception as scan_exc:
        logger.warning(
            "[DALTON_NOTIFY] Retry scan failed: error_type=%s",
            type(scan_exc).__name__,
        )
    return sent_count


async def on_automation_settled(
    *,
    automation: Mapping[str, Any],
    execution_id: str,
    thread_id: str | None = None,
    run_id: str | None = None,
    workspace_id: str | None = None,
    final_text: str | None = None,
    resolve_enabled_fn: Callable[[str | None], Awaitable[bool]] = resolve_dalton_notify_enabled,
    resolve_overrides_fn: Callable[[str | None], Awaitable[dict[str, bool]]] = resolve_dalton_notify_overrides,
    resolve_targets_fn: Callable[[str | None], Awaitable[list[str]]] = resolve_dalton_notify_targets,
    get_verdict_fn: Callable[..., Awaitable[dict[str, Any] | None]] = get_verdict_for_execution,
    claim_target_fn: Callable[..., Awaitable[bool]] = claim_target_delivery,
    mark_target_fn: Callable[..., Awaitable[None]] = mark_target_delivery,
    claim_execution_fn: Callable[[str], Awaitable[bool]] | None = None,
    post_notify_fn: Callable[..., Awaitable[dict[str, Any]]] = post_bridge_notify,
    frontend_url_fn: Callable[[], str] = get_dalton_frontend_url,
) -> bool:
    """Send personal Feishu Card 2.0 / Discord DM notification when an eligible Dalton automation completes.

    Dispatches one bridge ``/notify`` request per target and records per-target status in
    ``dalton_notify_deliveries`` without touching ``automation_executions.delivery_result``.
    Any failure after claiming a target marks all unfinalized claimed targets as ``failed``
    so no row remains stuck in ``pending``. Returns ``True`` iff at least one target was
    delivered successfully. Never raises.
    """
    _ = workspace_id
    pending_targets: list[tuple[str, str]] = []
    try:
        classification = classify_automation_for_notify(automation)
        if classification is None:
            return False

        user_id = str(automation.get("user_id") or "").strip()
        allowed, _, targets = await evaluate_user_notify_eligibility(
            user_id,
            automation,
            resolve_enabled_fn=resolve_enabled_fn,
            resolve_overrides_fn=resolve_overrides_fn,
            resolve_targets_fn=resolve_targets_fn,
        )
        if not allowed or not targets:
            return False

        verdict_row = await get_verdict_fn(
            automation_execution_id=execution_id,
            run_id=run_id,
        )
        verdict_word = (
            str(verdict_row.get("word") or "").strip()
            if isinstance(verdict_row, Mapping)
            else ""
        )

        if classification.requires_non_no_change_verdict:
            if not verdict_word or verdict_word.upper() == "NO_CHANGE":
                logger.info(
                    "[DALTON_NOTIFY] Skipping price_move notify for execution_id=%s (verdict=%r)",
                    execution_id,
                    verdict_word or None,
                )
                return False

        resolved_frontend_url = frontend_url_fn()
        message_text = format_notify_message(
            classification,
            verdict_word=verdict_word or None,
            final_text=final_text,
            thread_id=thread_id,
            frontend_base_url=resolved_frontend_url,
        )
        card_dict = build_feishu_notify_card(
            classification,
            verdict_word=verdict_word or None,
            final_text=final_text,
            thread_id=thread_id,
            frontend_base_url=resolved_frontend_url,
        )
        payload_record = {
            "text": message_text,
            "card": card_dict,
            "automation_name": str(automation.get("name") or "").strip(),
            "category": classification.category,
        }
        automation_id_str = (
            str(automation.get("automation_id") or "").strip() or None
        )

        claimed_targets: list[tuple[str, str]] = []
        if claim_execution_fn is not None:
            if await claim_execution_fn(execution_id):
                for tgt in targets:
                    claimed_targets.append(_split_target_channel(tgt))
        else:
            for tgt in targets:
                ch, cleaned_tgt = _split_target_channel(tgt)
                if await claim_target_fn(
                    execution_id,
                    ch,
                    cleaned_tgt,
                    user_id=user_id,
                    automation_id=automation_id_str,
                    payload=payload_record,
                ):
                    claimed_targets.append((ch, cleaned_tgt))
                    pending_targets.append((ch, cleaned_tgt))

        if not claimed_targets:
            logger.info(
                "[DALTON_NOTIFY] Skipping duplicate notify for execution_id=%s",
                execution_id,
            )
            return False

        any_sent = False
        for ch, cleaned_tgt in claimed_targets:
            try:
                bridge_res = await _invoke_post_notify(
                    post_notify_fn,
                    [cleaned_tgt],
                    message_text,
                    card_dict,
                )
            except Exception as post_exc:
                status_code = getattr(
                    getattr(post_exc, "response", None), "status_code", None
                )
                err_code = (
                    f"http_{status_code}"
                    if status_code
                    else type(post_exc).__name__
                )
                if claim_execution_fn is None:
                    try:
                        await _invoke_mark_delivery(
                            mark_target_fn,
                            execution_id,
                            ch,
                            cleaned_tgt,
                            status="failed",
                            error_code=err_code,
                            expected_status="pending",
                        )
                    except asyncio.CancelledError:
                        for rem_ch, rem_tgt in list(pending_targets):
                            await _shielded_mark_failed(
                                mark_target_fn,
                                execution_id,
                                rem_ch,
                                rem_tgt,
                                error_code=err_code,
                                expected_status="pending",
                            )
                        pending_targets.clear()
                        raise
                    except Exception:
                        pass
                    if (ch, cleaned_tgt) in pending_targets:
                        pending_targets.remove((ch, cleaned_tgt))
                logger.error(
                    "[DALTON_NOTIFY] Notification dispatch failed for execution_id=%s: error_type=%s status_code=%s",
                    execution_id,
                    type(post_exc).__name__,
                    status_code,
                )
                continue

            outcomes, batch_err = _evaluate_target_results(
                [cleaned_tgt], bridge_res
            )
            ok = outcomes.get(cleaned_tgt, False)
            if ok:
                any_sent = True
            if claim_execution_fn is None:
                try:
                    await _invoke_mark_delivery(
                        mark_target_fn,
                        execution_id,
                        ch,
                        cleaned_tgt,
                        status="sent" if ok else "failed",
                        error_code=None if ok else (batch_err or "target_failed"),
                    )
                except Exception as mark_exc:
                    logger.warning(
                        "[DALTON_NOTIFY] Failed marking delivery status for execution_id=%s: error_type=%s",
                        execution_id,
                        type(mark_exc).__name__,
                    )
                if (ch, cleaned_tgt) in pending_targets:
                    pending_targets.remove((ch, cleaned_tgt))

        return any_sent
    except asyncio.CancelledError:
        if claim_execution_fn is None and pending_targets:
            for ch, cleaned_tgt in list(pending_targets):
                await _shielded_mark_failed(
                    mark_target_fn,
                    execution_id,
                    ch,
                    cleaned_tgt,
                    error_code="CancelledError",
                    expected_status="pending",
                )
            pending_targets.clear()
        raise
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        err_code = f"http_{status_code}" if status_code else type(exc).__name__
        if claim_execution_fn is None and pending_targets:
            try:
                for ch, cleaned_tgt in list(pending_targets):
                    try:
                        await _invoke_mark_delivery(
                            mark_target_fn,
                            execution_id,
                            ch,
                            cleaned_tgt,
                            status="failed",
                            error_code=err_code,
                            expected_status="pending",
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                    if (ch, cleaned_tgt) in pending_targets:
                        pending_targets.remove((ch, cleaned_tgt))
            except asyncio.CancelledError:
                for rem_ch, rem_tgt in list(pending_targets):
                    await _shielded_mark_failed(
                        mark_target_fn,
                        execution_id,
                        rem_ch,
                        rem_tgt,
                        error_code=err_code,
                        expected_status="pending",
                    )
                pending_targets.clear()
                raise
        logger.error(
            "[DALTON_NOTIFY] Notification dispatch failed for execution_id=%s: error_type=%s status_code=%s",
            execution_id,
            type(exc).__name__,
            status_code,
        )
        return False
