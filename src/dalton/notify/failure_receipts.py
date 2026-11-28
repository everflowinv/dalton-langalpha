"""Durable failure-notice receipt ledger, atomic dedup, leased claim, and reconciliation.

Covers four unattended failure subject kinds (``personal_automation``,
``company_automation``, ``data_task``, ``wakeup``) using Dalton-owned tables
introduced in migration ``d062_failure_notice_receipts``:
- ``dalton_failure_notices`` (with partial unique index ``uq_dalton_failure_notices_dedup_key``)
- ``dalton_failure_notice_streaks`` (monotonic ``recovery_epoch`` bumped on success)
- ``dalton_failure_notice_targets`` (leased with ``claim_token`` + ``claim_expires_at``)

Multi-worker invariant:
- Truth lives strictly in PostgreSQL. In-process task handle ``_dispatch_wake_task``
  is purely a local wakeup nudge and never holds durable state.
- Tokens, prompt bodies, and user message bodies are never logged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
import json
import logging
import re
import time
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from src.dalton.capabilities import (
    CapabilityId,
    has_capability,
    is_channel_target_enabled,
)
from src.dalton.company_identity import is_company_system_user
from src.dalton.notify.channel_transport import (
    is_uncertain_notify_error_code,
    post_failure_notice_gateway_notify,
)
from src.dalton.settings import (
    is_failure_notice_enabled,
    redact_managed_tokens,
)
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

FAILURE_NOTICE_SUBJECT_KINDS: frozenset[str] = frozenset(
    {
        "personal_automation",
        "company_automation",
        "data_task",
        "wakeup",
    }
)

FAILURE_NOTICE_AGGREGATE_STATUSES: frozenset[str] = frozenset(
    {
        "not_applicable",
        "unconfigured",
        "suppressed",
        "retrying",
        "delivered",
        "partial_failed",
        "failed",
    }
)

FAILURE_NOTICE_TARGET_STATUSES: frozenset[str] = frozenset(
    {
        "pending",
        "sending",
        "uncertain",
        "retryable_failed",
        "sent",
        "terminal_failed",
    }
)

# §3.3 Decision 2.2: deterministic non-retryable rejection codes
FAILURE_NOTICE_TERMINAL_ERROR_CODES: frozenset[str] = frozenset(
    {
        "not_allowed",
        "not_gateway_notification",
        "http_400",
        "http_401",
        "http_403",
        "http_404",
        "invalid_target",
        "dm_binding_missing",
        "recipient_not_found",
        "bot_not_in_chat",
        "weixin_context_token_stale",
        "weixin_session_expired",
        "weixin_account_inactive",
        "card_limit_exceeded",
        "attempts_exhausted",
        "delivery_outcome_unknown_timeout",
        "task_disabled",
        "task_notify_disabled",
        "toggle_off",
        "target_removed",
        "target_not_routed",
        "unknown_task",
        "covered_by_existing_delivery",
    }
)

FAILURE_NOTICE_CLAIM_TTL_SECONDS = 60
FAILURE_NOTICE_UNKNOWN_TIMEOUT_MINUTES = 15
FAILURE_NOTICE_UNCERTAIN_RECONCILE_BACKOFF_SECONDS = 60
FAILURE_NOTICE_MAX_ATTEMPTS = 3
FAILURE_NOTICE_RETRY_BACKOFF_SECONDS: tuple[int, ...] = (60, 300)
FAILURE_NOTICE_SWEEP_INTERVAL_SECONDS = 30.0

PENDING_SETTLEMENT_KEY = "pending_settlement"

_STABLE_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SAFE_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_DATE_OR_TIMESTAMP_RE = re.compile(r"\d{4}[-/]\d{2}[-/]\d{2}")
_TWO_PART_STABLE_PREFIXES: frozenset[str] = frozenset(
    {
        "t5_missing_top_key",
        "t5_top_step_error",
        "sentiment_missing_shared_asset",
        "sentiment_missing_or_empty_panel",
        "sentiment_stale_panel_not_updated",
    }
)

_FEISHU_USER_RE = re.compile(r"^feishu:user:(ou_[A-Za-z0-9_-]+)$")
_FEISHU_CHAT_RE = re.compile(r"^feishu:chat:(oc_[A-Za-z0-9_-]+)$")
_DISCORD_USER_RE = re.compile(r"^discord:user:([0-9]+)$")
_DISCORD_CHANNEL_RE = re.compile(r"^discord:channel:([0-9]{5,32})$")
_WEIXIN_USER_RE = re.compile(r"^weixin:user:([^\s:]{1,255})$")

_REASON_LABELS_ZH: dict[str, str] = {
    "usage_limit": "模型使用额度已达上限",
    "claude_plan": "Claude 订阅计划额度已用尽",
    "plan_limit": "订阅模型配额已用尽",
    "subscription_quota_exhausted": "订阅模型配额已耗尽",
    "oauth_auth_unavailable": "订阅模型授权已失效，需要重新登录授权",
    "provider_auth": "模型服务商认证被拒绝，请检查凭据",
    "server_error": "服务端或上游模型临时异常",
    "interrupted": "执行因服务重启中断",
    "timeout": "执行超时",
}

_SUBJECT_LABELS_ZH: dict[str, str] = {
    "personal_automation": "定时任务",
    "company_automation": "公司定时任务",
    "data_task": "数据任务",
    "wakeup": "定时唤醒",
}

# Process-local wakeup task (execution nudge only — durable truth lives in PostgreSQL).
_dispatch_wake_task: asyncio.Task[Any] | None = None


def build_pending_settlement_record(
    *,
    outcome: str,
    failure_reason: str | None,
    is_quota_limit: bool,
    schedule_policy: str | None,
    schedule_action: str | None,
    error: str | None = None,
    excerpt: str | None = None,
    workspace_id: str | None = None,
    thread_id: str | None = None,
    release_pre_start_signals: bool = False,
) -> dict[str, Any]:
    """Build durable JSON payload preserving pre-run failure settlement parameters."""
    rec: dict[str, Any] = {
        "outcome": str(outcome or "").strip().lower(),
        "failure_reason": str(failure_reason).strip().lower() if failure_reason else None,
        "is_quota_limit": bool(is_quota_limit),
        "schedule_policy": str(schedule_policy).strip() if schedule_policy else None,
        "schedule_action": str(schedule_action).strip() if schedule_action else None,
        "release_pre_start_signals": bool(release_pre_start_signals),
    }
    if error is not None:
        rec["error"] = str(error)
    if excerpt is not None:
        rec["excerpt"] = str(excerpt)
    if workspace_id is not None and str(workspace_id).strip():
        rec["workspace_id"] = str(workspace_id).strip()
    if thread_id is not None and str(thread_id).strip():
        rec["thread_id"] = str(thread_id).strip()
    return rec


def read_pending_settlement(exec_row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Read a persisted ``pending_settlement`` record from ``automation_executions.delivery``."""
    if not isinstance(exec_row, Mapping):
        return None
    delivery = exec_row.get("delivery")
    if isinstance(delivery, str) and delivery.strip():
        try:
            delivery = json.loads(delivery)
        except json.JSONDecodeError:
            return None
    if not isinstance(delivery, Mapping):
        return None
    raw = delivery.get(PENDING_SETTLEMENT_KEY)
    if not isinstance(raw, Mapping):
        return None
    if not (raw.get("outcome") or raw.get("failure_reason") or raw.get("is_quota_limit") is True):
        return None
    return dict(raw)


async def persist_pending_settlement(
    execution_id: str,
    record: Mapping[str, Any],
    *,
    conn: Any = None,
) -> bool:
    """Persist ``pending_settlement`` into ``automation_executions.delivery`` on rollback.

    Best-effort: logs and returns False on DB error so it never masks the caller's
    original settlement rollback exception.
    """
    exec_uuid = _normalize_uuid(execution_id)
    if exec_uuid is None or not isinstance(record, Mapping):
        return False
    payload_json = json.dumps(dict(record), ensure_ascii=False)
    try:
        async with get_db_connection(conn) as owned:
            async with owned.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE automation_executions
                    SET delivery = jsonb_set(
                        COALESCE(delivery, '{}'::jsonb),
                        '{pending_settlement}',
                        %s::jsonb,
                        true
                    )
                    WHERE automation_execution_id = %s::uuid
                      AND status IN ('pending', 'waiting', 'running')
                    """,
                    (payload_json, exec_uuid),
                )
                return cur.rowcount > 0
    except Exception as exc:
        logger.warning(
            "[FAILURE_NOTICE] Failed to persist pending_settlement for execution_id=%s (%s)",
            exec_uuid,
            type(exc).__name__,
        )
        return False


def failure_notice_delivery_key(target_receipt_id: str) -> str:
    """Return canonical ``dalton_channel_deliveries.delivery_key`` for one target receipt."""
    return f"failure_notice:{str(target_receipt_id).strip().lower()}:item:0"


def is_failure_notice_terminal_error_code(error_code: str | None) -> bool:
    """Return True when ``error_code`` is a deterministic non-retryable rejection."""
    cleaned = str(error_code or "").strip().lower()
    return cleaned in FAILURE_NOTICE_TERMINAL_ERROR_CODES


def _normalize_uuid(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return str(UUID(text))
    except ValueError:
        return None


def _sanitize_text(raw: Any, *, max_chars: int = 300) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    cleaned = redact_managed_tokens(text)
    if len(cleaned) > max_chars:
        return cleaned[: max_chars - 1].rstrip() + "…"
    return cleaned


def _retry_backoff_seconds(attempt_count: int) -> int:
    idx = max(0, min(int(attempt_count) - 1, len(FAILURE_NOTICE_RETRY_BACKOFF_SECONDS) - 1))
    return FAILURE_NOTICE_RETRY_BACKOFF_SECONDS[idx]


def normalize_channel_target(raw_target: str | None) -> tuple[str, str] | None:
    """Parse a channel target into ``(channel, canonical_target)`` or ``None``."""
    cleaned = str(raw_target or "").strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if lowered in ("feishu:@me", "discord:@me", "weixin:@me", "wechat:@me"):
        ch = "weixin" if lowered.startswith("wechat:") else lowered.split(":", 1)[0]
        return ch, f"{ch}:@me"
    if _FEISHU_USER_RE.match(cleaned) or _FEISHU_CHAT_RE.match(cleaned):
        return "feishu", cleaned
    if _DISCORD_USER_RE.match(cleaned) or _DISCORD_CHANNEL_RE.match(cleaned):
        return "discord", cleaned
    if _WEIXIN_USER_RE.match(cleaned):
        return "weixin", cleaned
    if lowered.startswith("wechat:user:"):
        suffix = cleaned.split(":", 2)[2].strip()
        if suffix and _WEIXIN_USER_RE.match(f"weixin:user:{suffix}"):
            return "weixin", f"weixin:user:{suffix}"
    return None


def _is_dm_target(target: str) -> bool:
    return (
        bool(_FEISHU_USER_RE.match(target))
        or bool(_DISCORD_USER_RE.match(target))
        or bool(_WEIXIN_USER_RE.match(target))
    )


def extract_held_delivery_covered_targets(delivery: Any) -> set[tuple[str, str]]:
    """Extract normalized ``(channel, canonical_target)`` pairs already covered by ``automation_finish``.

    When ``delivery.held`` is True, any resolved ``ok=True`` target will already receive
    the ``hook_outbox(automation_finish)`` failure notice. DM targets also record
    ``(channel, f"{channel}:@me")`` so ``feishu:@me`` deduplicates ``feishu:user:ou_*``.
    """
    covered: set[tuple[str, str]] = set()
    if delivery is None:
        return covered

    if isinstance(delivery, Mapping):
        raw_targets = delivery.get("targets") or ()
        held = bool(delivery.get("held")) or any(
            isinstance(t, Mapping) and t.get("ok") is True for t in raw_targets
        )
    else:
        held = bool(getattr(delivery, "held", False))
        raw_targets = getattr(delivery, "targets", ()) or ()

    if not held:
        return covered

    for entry in raw_targets:
        if isinstance(entry, Mapping):
            ok = entry.get("ok") is True
            addr = entry.get("address")
        else:
            ok = getattr(entry, "ok", False) is True
            addr = getattr(entry, "address", None)
        if not ok or not addr:
            continue
        parsed = normalize_channel_target(str(addr))
        if parsed is None:
            cleaned_addr = str(addr).strip()
            low_addr = cleaned_addr.lower()
            if cleaned_addr.startswith("feishu:ou_"):
                parsed = ("feishu", f"feishu:user:{cleaned_addr[len('feishu:'):]}")
            elif low_addr.startswith("weixin:dm:"):
                parsed = ("weixin", "weixin:@me")
        if parsed is None:
            continue
        ch, canon = parsed
        covered.add((ch, canon))
        if canon == f"{ch}:@me" or _is_dm_target(canon):
            covered.add((ch, f"{ch}:@me"))
    return covered


def _target_is_covered(
    channel: str,
    target: str,
    covered: set[tuple[str, str]],
) -> bool:
    if (channel, target) in covered:
        return True
    if _is_dm_target(target) and (channel, f"{channel}:@me") in covered:
        return True
    return False


def normalize_data_task_failure_code(
    failure_reason: str | None,
    *,
    terminal_status: str = "failed",
    explicit_code: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> str:
    """Derive a stable failure-type code (free of dates and volatile error prose) for data-task dedup."""
    fallback = str(terminal_status or "failed").strip().lower()
    if not _STABLE_CODE_RE.match(fallback):
        fallback = "failed"

    if explicit_code:
        cleaned_explicit = str(explicit_code).strip().lower()
        if (
            cleaned_explicit
            and _STABLE_CODE_RE.match(cleaned_explicit)
            and not _DATE_OR_TIMESTAMP_RE.search(cleaned_explicit)
        ):
            return cleaned_explicit

    if isinstance(details, Mapping):
        for key in ("failure_code", "report_prep_code", "timeout_kind"):
            val = details.get(key)
            if key == "timeout_kind" and val in ("total", "no_output"):
                return f"{val}_timed_out"
            if isinstance(val, str):
                cleaned_val = val.strip().lower()
                if (
                    cleaned_val
                    and _STABLE_CODE_RE.match(cleaned_val)
                    and not _DATE_OR_TIMESTAMP_RE.search(cleaned_val)
                ):
                    return cleaned_val

    raw = str(failure_reason or "").strip()
    if not raw:
        return fallback

    # Drop trailing "| 数据日期: ..." or similar pipe-delimited dynamic context.
    head = raw.split("|", 1)[0].strip()
    if not head:
        return fallback

    # Handle structured colon-delimited evaluator reasons like
    # "t5_stale_unwind_date:2026-11-25:ref=..." or "report_preparation_failed: ...".
    if ":" in head:
        parts = [p.strip().lower() for p in head.split(":")]
        prefix = parts[0]
        if _STABLE_CODE_RE.match(prefix) and not _DATE_OR_TIMESTAMP_RE.search(prefix):
            if (
                prefix in _TWO_PART_STABLE_PREFIXES
                and len(parts) >= 2
                and _SAFE_SEGMENT_RE.match(parts[1])
                and not _DATE_OR_TIMESTAMP_RE.search(parts[1])
            ):
                return f"{prefix}:{parts[1]}"
            return prefix
        return fallback

    lowered = head.lower()
    if _STABLE_CODE_RE.match(lowered) and not _DATE_OR_TIMESTAMP_RE.search(lowered):
        return lowered
    return fallback


async def evaluate_data_task_failure_notice_eligibility(
    conn: Any,
    *,
    job_key: str,
    recipient_user_id: str,
    run_id: str | None = None,
    task_notify_enabled: bool | None = None,
    notify_status: str | None = None,
    evaluate_eligibility_fn: Callable[..., Awaitable[tuple[bool, str | None, list[str]]]] | None = None,
) -> tuple[bool, str | None, list[tuple[str, str]], set[tuple[str, str]]]:
    """Evaluate data-task push settings, DM targets, and ``notify_on_failure`` overlap.

    Reuses the same push-permission checks as ``dispatch_data_task_run_notification``:
    - ``dalton_data_task_defs.enabled`` and ``dalton_data_task_defs.notify_enabled``
    - ``evaluate_user_notify_eligibility(recipient_user_id, build_data_task_notify_automation(job_key))``
    - Excludes every target already handled by ``notify_status IN ('pending_dispatch', 'dispatched')``.

    Never falls back to ``load_bound_dm_targets`` when targets are empty or disabled.
    """
    cleaned_job_key = str(job_key or "").strip()
    cleaned_uid = str(recipient_user_id or "").strip()
    if not cleaned_job_key or not cleaned_uid:
        return (False, "toggle_off", [], set())

    if task_notify_enabled is False:
        return (False, "task_notify_disabled", [], set())

    async with conn.cursor(row_factory=dict_row) as cur:
        if task_notify_enabled is None:
            await cur.execute(
                """
                SELECT enabled, notify_enabled
                FROM dalton_data_task_defs
                WHERE job_key = %s
                """,
                (cleaned_job_key,),
            )
            def_row = await cur.fetchone()
            if not isinstance(def_row, Mapping):
                return (False, "unknown_task", [], set())
            if not bool(def_row.get("enabled")):
                return (False, "task_disabled", [], set())
            if not bool(def_row.get("notify_enabled")):
                return (False, "task_notify_disabled", [], set())

        eff_notify_status = str(notify_status or "").strip().lower()
        if not eff_notify_status and run_id:
            norm_rid = _normalize_uuid(run_id)
            if norm_rid is not None:
                await cur.execute(
                    """
                    SELECT notify_status
                    FROM dalton_data_task_runs
                    WHERE run_id = %s::uuid
                    """,
                    (norm_rid,),
                )
                run_row = await cur.fetchone()
                if isinstance(run_row, Mapping):
                    eff_notify_status = str(run_row.get("notify_status") or "").strip().lower()

    from src.dalton.data_jobs.scheduler import build_data_task_notify_automation
    from src.dalton.notify.dispatch import evaluate_user_notify_eligibility

    automation = build_data_task_notify_automation(cleaned_job_key)
    if evaluate_eligibility_fn is not None:
        allowed, skip_reason, effective_targets = await evaluate_eligibility_fn(
            cleaned_uid, automation
        )
    else:
        allowed, skip_reason, effective_targets = await evaluate_user_notify_eligibility(
            cleaned_uid,
            automation,
            conn=conn,
            cached=False,
        )
    if not allowed:
        return (False, skip_reason or "toggle_off", [], set())

    dm_targets: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    covered: set[tuple[str, str]] = set()
    existing_delivery_active = eff_notify_status in ("pending_dispatch", "dispatched")

    for raw in effective_targets or ():
        parsed = normalize_channel_target(str(raw))
        if parsed is None:
            continue
        ch, canon = parsed
        if existing_delivery_active:
            covered.add((ch, canon))
            if canon == f"{ch}:@me" or _is_dm_target(canon):
                covered.add((ch, f"{ch}:@me"))
        if _is_dm_target(canon) and is_channel_target_enabled(canon):
            pair = (ch, canon)
            if pair not in seen:
                seen.add(pair)
                dm_targets.append(pair)

    return (True, None, dm_targets, covered)


async def _resolve_dm_targets_in_tx(
    conn: Any,
    recipient_user_id: str,
    *,
    explicit_targets: Sequence[str] | None = None,
    allow_bound_fallback: bool = True,
) -> list[tuple[str, str]]:
    """Resolve enabled DM targets for ``recipient_user_id`` inside the active transaction."""
    candidates: list[str] = []
    has_explicit_pref_list = False
    if explicit_targets is not None:
        candidates.extend(str(t).strip() for t in explicit_targets if str(t or "").strip())
    elif recipient_user_id:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT model_preference, other_preference
                FROM user_preferences
                WHERE user_id = %s
                """,
                (recipient_user_id,),
            )
            pref_row = await cur.fetchone()
        if isinstance(pref_row, Mapping):
            for bag_key in ("model_preference", "other_preference"):
                bag = pref_row.get(bag_key)
                if isinstance(bag, Mapping) and "dalton_notify_targets" in bag:
                    raw_list = bag.get("dalton_notify_targets")
                    if isinstance(raw_list, list):
                        has_explicit_pref_list = True
                        for item in raw_list:
                            s = str(item or "").strip()
                            if s:
                                candidates.append(s)
                        break
        if not candidates and not has_explicit_pref_list and allow_bound_fallback:
            from src.dalton.llm.model_prefs import load_bound_dm_targets

            bound = await load_bound_dm_targets(recipient_user_id, conn=conn)
            candidates.extend(sorted(bound))

    resolved: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in candidates:
        parsed = normalize_channel_target(raw)
        if parsed is None:
            continue
        ch, canon = parsed
        if not _is_dm_target(canon):
            continue
        if not is_channel_target_enabled(canon):
            continue
        pair = (ch, canon)
        if pair not in seen:
            seen.add(pair)
            resolved.append(pair)
    return resolved


def _build_failure_notice_payload(
    *,
    subject_kind: str,
    subject_id: str,
    source_key: str,
    failure_reason: str,
    title: str | None,
    detail: str | None,
    metadata: Mapping[str, Any] | None,
    channel: str,
) -> dict[str, Any]:
    """Build sanitized Feishu Card 2.0 or plain-text payload for one failure notice target."""
    subject_label = _SUBJECT_LABELS_ZH.get(subject_kind, "任务")
    reason_key = str(failure_reason or "failed").strip().lower()
    reason_zh = _REASON_LABELS_ZH.get(reason_key, f"执行失败（{reason_key}）")
    safe_title = _sanitize_text(title, max_chars=120) if title else f"{subject_label}执行失败提醒"
    safe_detail = _sanitize_text(detail, max_chars=260) if detail else ""
    safe_meta: dict[str, Any] = {}
    if isinstance(metadata, Mapping):
        for k in ("automation_name", "job_key", "thread_id", "workspace_id", "reset_at", "cooldown_key"):
            val = metadata.get(k)
            if val is not None and str(val).strip():
                safe_meta[k] = _sanitize_text(val, max_chars=160)

    lines = [f"【{safe_title}】", f"原因：{reason_zh}"]
    if safe_meta.get("automation_name"):
        lines.insert(1, f"任务：{safe_meta['automation_name']}")
    elif safe_meta.get("job_key"):
        lines.insert(1, f"任务：{safe_meta['job_key']}")
    if safe_meta.get("reset_at"):
        lines.append(f"预计恢复：{safe_meta['reset_at']}")
    if safe_detail and safe_detail != reason_zh:
        lines.append(f"详情：{safe_detail}")
    plain_text = "\n".join(lines)

    payload: dict[str, Any] = {
        "source": "failure_notice",
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "source_key": source_key,
        "failure_reason": reason_key,
        "text": plain_text,
    }
    if safe_meta:
        payload["metadata"] = safe_meta
    if channel == "feishu":
        md_lines = [f"**原因**：{reason_zh}"]
        if safe_meta.get("automation_name"):
            md_lines.insert(0, f"**任务**：{safe_meta['automation_name']}")
        elif safe_meta.get("job_key"):
            md_lines.insert(0, f"**任务**：{safe_meta['job_key']}")
        if safe_meta.get("reset_at"):
            md_lines.append(f"**预计恢复**：{safe_meta['reset_at']}")
        if safe_detail and safe_detail != reason_zh:
            md_lines.append(f"**详情**：{safe_detail}")
        payload["card"] = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": safe_title},
                "template": "red",
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": "\n".join(md_lines),
                    }
                ]
            },
        }
    return payload


async def bump_failure_notice_recovery_epoch_in_tx(
    conn: Any,
    *,
    subject_kind: str,
    source_key: str,
) -> int | None:
    """Increment ``dalton_failure_notice_streaks.recovery_epoch`` inside the caller's tx."""
    if not is_failure_notice_enabled():
        return None
    kind = str(subject_kind or "").strip()
    src_key = str(source_key or "").strip()
    if kind not in FAILURE_NOTICE_SUBJECT_KINDS or not src_key:
        return None

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO dalton_failure_notice_streaks (
                subject_kind, source_key, recovery_epoch, updated_at
            )
            VALUES (%s, %s, 1, NOW())
            ON CONFLICT (subject_kind, source_key) DO UPDATE SET
                recovery_epoch = dalton_failure_notice_streaks.recovery_epoch + 1,
                updated_at = NOW()
            RETURNING recovery_epoch
            """,
            (kind, src_key),
        )
        row = await cur.fetchone()
    if isinstance(row, Mapping) and row.get("recovery_epoch") is not None:
        return int(row["recovery_epoch"])
    return None


async def enqueue_failure_notice_in_tx(
    conn: Any,
    *,
    subject_kind: str,
    subject_id: str,
    source_key: str,
    recipient_user_id: str | None = None,
    failure_reason: str,
    title: str | None = None,
    detail: str | None = None,
    dedup_window_key: str | None = None,
    cooldown_window_key: str | None = None,
    covered_targets: Iterable[str | tuple[str, str]] | None = None,
    delivery: Any = None,
    targets: Sequence[str] | None = None,
    metadata: Mapping[str, Any] | None = None,
    max_attempts: int = FAILURE_NOTICE_MAX_ATTEMPTS,
    data_task_notify_enabled: bool | None = None,
    data_task_notify_status: str | None = None,
    data_task_failure_code: str | None = None,
) -> dict[str, Any] | None:
    """Atomically enqueue one failure notice + per-target receipts inside the caller's tx.

    Shared entry point for:
    - Personal & company automations (``automation_settlement.py`` ``leave_finish``)
    - Data tasks (``data_jobs/scheduler.py`` ``finalize_task_run``)
    - Unattended wakeup quota exhaustion (Item 2, ``subject_kind='wakeup'``)

    Uses ``uq_dalton_failure_notices_dedup_key`` for atomic cross-worker anti-spam dedup
    and strips targets already covered by ``hook_outbox(automation_finish)`` or
    data-task ``notify_on_failure`` delivery.
    """
    if not is_failure_notice_enabled():
        return None
    if not has_capability(CapabilityId.CHANNEL_GATEWAY):
        return None

    kind = str(subject_kind or "").strip()
    subj_id = str(subject_id or "").strip()
    src_key = str(source_key or "").strip()
    raw_reason = str(failure_reason or "failed").strip()
    if kind == "data_task":
        reason = normalize_data_task_failure_code(
            raw_reason,
            explicit_code=data_task_failure_code,
            details=metadata if isinstance(metadata, Mapping) else None,
        )
    else:
        reason = raw_reason.lower() or "failed"
    if kind not in FAILURE_NOTICE_SUBJECT_KINDS or not subj_id or not src_key:
        raise ValueError(
            f"Invalid failure notice identity: subject_kind={kind!r}, subject_id={subj_id!r}, source_key={src_key!r}"
        )

    eff_recipient = str(recipient_user_id or "").strip()
    if (not eff_recipient or is_company_system_user(eff_recipient)) and kind in (
        "company_automation",
        "data_task",
    ):
        from src.dalton.access_tiers import get_primary_admin_user_id

        eff_recipient = str((await get_primary_admin_user_id(conn=conn)) or "").strip()

    bounded_max = max(1, int(max_attempts or FAILURE_NOTICE_MAX_ATTEMPTS))

    # 1. Read or initialize recovery_epoch for this (subject_kind, source_key)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO dalton_failure_notice_streaks (
                subject_kind, source_key, recovery_epoch, updated_at
            )
            VALUES (%s, %s, 0, NOW())
            ON CONFLICT (subject_kind, source_key) DO NOTHING
            """,
            (kind, src_key),
        )
        await cur.execute(
            """
            SELECT recovery_epoch
            FROM dalton_failure_notice_streaks
            WHERE subject_kind = %s AND source_key = %s
            """,
            (kind, src_key),
        )
        streak_row = await cur.fetchone()
    recovery_epoch = int(streak_row["recovery_epoch"]) if isinstance(streak_row, Mapping) else 0

    explicit_window = str(dedup_window_key or cooldown_window_key or "").strip()
    effective_dedup_key = explicit_window or f"{reason}:epoch:{recovery_epoch}"

    # 2. Compute covered targets from delivery.held + explicit covered_targets
    covered = extract_held_delivery_covered_targets(delivery)
    if covered_targets is not None:
        for item in covered_targets:
            if isinstance(item, tuple) and len(item) == 2:
                norm = normalize_channel_target(str(item[1]))
            else:
                norm = normalize_channel_target(str(item))
            if norm is not None:
                ch, canon = norm
                covered.add((ch, canon))
                if canon == f"{ch}:@me" or _is_dm_target(canon):
                    covered.add((ch, f"{ch}:@me"))

    # 3. Resolve candidate DM targets and deduplicate against automation_finish / notify_on_failure
    ineligible_reason: str | None = None
    if kind == "data_task" and targets is None:
        dt_allowed, dt_skip_reason, dt_dm_targets, dt_covered = (
            await evaluate_data_task_failure_notice_eligibility(
                conn,
                job_key=src_key,
                recipient_user_id=eff_recipient,
                run_id=subj_id,
                task_notify_enabled=data_task_notify_enabled,
                notify_status=data_task_notify_status,
            )
        )
        covered.update(dt_covered)
        if not dt_allowed:
            ineligible_reason = dt_skip_reason or "toggle_off"
            candidate_targets: list[tuple[str, str]] = []
        else:
            candidate_targets = dt_dm_targets
    elif kind == "data_task" and data_task_notify_enabled is False:
        ineligible_reason = "task_notify_disabled"
        candidate_targets = []
    else:
        candidate_targets = await _resolve_dm_targets_in_tx(
            conn,
            eff_recipient,
            explicit_targets=targets,
            allow_bound_fallback=(kind != "data_task"),
        )

    uncovered_targets = [
        (ch, tgt) for ch, tgt in candidate_targets if not _target_is_covered(ch, tgt, covered)
    ]

    summary_base: dict[str, Any] = {
        "failure_reason": reason,
        "recovery_epoch": recovery_epoch,
        "dedup_window_key": effective_dedup_key,
        "total_targets": len(uncovered_targets),
        "sent_count": 0,
        "failed_count": 0,
        "retrying_count": len(uncovered_targets),
        "covered_by_finish_count": len(candidate_targets) - len(uncovered_targets),
    }
    if detail:
        summary_base["detail"] = _sanitize_text(detail, max_chars=260)
    if isinstance(metadata, Mapping) and metadata.get("index_dates"):
        summary_base["index_dates"] = metadata["index_dates"]
    if ineligible_reason:
        summary_base["ineligible_reason"] = ineligible_reason

    # If no targets remain: either disabled/covered (not_applicable) or unconfigured
    if not uncovered_targets:
        if ineligible_reason in ("task_disabled", "task_notify_disabled", "toggle_off", "unknown_task"):
            terminal_agg = "not_applicable"
        elif (candidate_targets and len(uncovered_targets) == 0) or (not candidate_targets and covered):
            terminal_agg = "not_applicable"
        else:
            terminal_agg = "unconfigured"
        summary_base["retrying_count"] = 0
        notice_id = str(uuid4())
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_failure_notices (
                    notice_id, subject_kind, subject_id, source_key,
                    recipient_user_id, failure_reason, dedup_window_key,
                    aggregate_status, summary_json, created_at, updated_at
                )
                VALUES (%s::uuid, %s, %s, %s, %s, %s, NULL, %s, %s::jsonb, NOW(), NOW())
                ON CONFLICT (subject_kind, subject_id) DO NOTHING
                RETURNING notice_id::text AS notice_id, aggregate_status
                """,
                (
                    notice_id,
                    kind,
                    subj_id,
                    src_key,
                    eff_recipient,
                    reason,
                    terminal_agg,
                    json.dumps(summary_base, ensure_ascii=False),
                ),
            )
            inserted = await cur.fetchone()
            if inserted is None:
                await cur.execute(
                    """
                    SELECT notice_id::text AS notice_id, aggregate_status
                    FROM dalton_failure_notices
                    WHERE subject_kind = %s AND subject_id = %s
                    """,
                    (kind, subj_id),
                )
                existing = await cur.fetchone()
                return {
                    "notice_id": str(existing["notice_id"]) if isinstance(existing, Mapping) else notice_id,
                    "subject_kind": kind,
                    "subject_id": subj_id,
                    "source_key": src_key,
                    "aggregate_status": str(existing["aggregate_status"]) if isinstance(existing, Mapping) else terminal_agg,
                    "dedup_window_key": None,
                    "target_receipt_ids": [],
                    "suppressed": False,
                }
        return {
            "notice_id": str(inserted["notice_id"]),
            "subject_kind": kind,
            "subject_id": subj_id,
            "source_key": src_key,
            "aggregate_status": str(inserted["aggregate_status"]),
            "dedup_window_key": None,
            "target_receipt_ids": [],
            "suppressed": False,
        }

    # 4. Atomic insert competing on uq_dalton_failure_notices_dedup_key AND (subject_kind, subject_id)
    notice_id = str(uuid4())
    target_receipt_ids: list[str] = []
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO dalton_failure_notices (
                notice_id, subject_kind, subject_id, source_key,
                recipient_user_id, failure_reason, dedup_window_key,
                aggregate_status, summary_json, created_at, updated_at
            )
            VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, 'retrying', %s::jsonb, NOW(), NOW())
            ON CONFLICT DO NOTHING
            RETURNING notice_id::text AS notice_id, aggregate_status
            """,
            (
                notice_id,
                kind,
                subj_id,
                src_key,
                eff_recipient,
                reason,
                effective_dedup_key,
                json.dumps(summary_base, ensure_ascii=False),
            ),
        )
        won = await cur.fetchone()
        if won is None:
            # Either this (subject_kind, subject_id) was already enqueued, or another worker won dedup_window_key.
            await cur.execute(
                """
                SELECT notice_id::text AS notice_id, aggregate_status, dedup_window_key
                FROM dalton_failure_notices
                WHERE subject_kind = %s AND subject_id = %s
                """,
                (kind, subj_id),
            )
            existing = await cur.fetchone()
            if isinstance(existing, Mapping):
                return {
                    "notice_id": str(existing["notice_id"]),
                    "subject_kind": kind,
                    "subject_id": subj_id,
                    "source_key": src_key,
                    "aggregate_status": str(existing["aggregate_status"]),
                    "dedup_window_key": existing.get("dedup_window_key"),
                    "target_receipt_ids": [],
                    "suppressed": str(existing["aggregate_status"]) == "suppressed",
                }
            # Lost the atomic partial unique index race -> insert suppressed audit row
            suppressed_summary = {
                **summary_base,
                "total_targets": 0,
                "retrying_count": 0,
                "suppressed_by_dedup_window_key": effective_dedup_key,
            }
            suppressed_id = str(uuid4())
            await cur.execute(
                """
                INSERT INTO dalton_failure_notices (
                    notice_id, subject_kind, subject_id, source_key,
                    recipient_user_id, failure_reason, dedup_window_key,
                    aggregate_status, summary_json, created_at, updated_at
                )
                VALUES (%s::uuid, %s, %s, %s, %s, %s, NULL, 'suppressed', %s::jsonb, NOW(), NOW())
                ON CONFLICT (subject_kind, subject_id) DO NOTHING
                RETURNING notice_id::text AS notice_id, aggregate_status
                """,
                (
                    suppressed_id,
                    kind,
                    subj_id,
                    src_key,
                    eff_recipient,
                    reason,
                    json.dumps(suppressed_summary, ensure_ascii=False),
                ),
            )
            sup_row = await cur.fetchone()
            return {
                "notice_id": str(sup_row["notice_id"]) if isinstance(sup_row, Mapping) else suppressed_id,
                "subject_kind": kind,
                "subject_id": subj_id,
                "source_key": src_key,
                "aggregate_status": "suppressed",
                "dedup_window_key": None,
                "target_receipt_ids": [],
                "suppressed": True,
            }

        winning_notice_id = str(won["notice_id"])
        for ch, tgt in uncovered_targets:
            tr_id = str(uuid4())
            payload = _build_failure_notice_payload(
                subject_kind=kind,
                subject_id=subj_id,
                source_key=src_key,
                failure_reason=reason,
                title=title,
                detail=detail,
                metadata=metadata,
                channel=ch,
            )
            await cur.execute(
                """
                INSERT INTO dalton_failure_notice_targets (
                    target_receipt_id, notice_id, channel, target,
                    status, attempt_count, max_attempts,
                    next_retry_at, payload_json, created_at, updated_at
                )
                VALUES (%s::uuid, %s::uuid, %s, %s, 'pending', 0, %s, NOW(), %s::jsonb, NOW(), NOW())
                ON CONFLICT (notice_id, channel, target) DO NOTHING
                """,
                (
                    tr_id,
                    winning_notice_id,
                    ch,
                    tgt,
                    bounded_max,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            target_receipt_ids.append(tr_id)

    return {
        "notice_id": winning_notice_id,
        "subject_kind": kind,
        "subject_id": subj_id,
        "source_key": src_key,
        "aggregate_status": "retrying",
        "dedup_window_key": effective_dedup_key,
        "target_receipt_ids": target_receipt_ids,
        "suppressed": False,
    }


async def on_automation_settle_in_tx(
    conn: Any,
    *,
    automation: Mapping[str, Any],
    execution_id: str,
    outcome_status: str,
    failure_reason: str | None = None,
    error: str | None = None,
    delivery: Any = None,
    thread_id: str | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any] | None:
    """Hook called inside ``automation_settlement.py`` ``leave_finish`` transaction.

    When ``is_failure_notice_enabled()`` is False, this is a strict no-op.
    When enabled, any SQL error propagates so the outer settlement transaction rolls back
    and existing settlement retry re-runs both settlement and enqueue atomically.
    """
    if not is_failure_notice_enabled():
        return None
    auto_id = str(automation.get("automation_id") or "").strip()
    if not auto_id:
        return None

    from src.dalton.database.automation_recycle import automation_is_soft_deleted

    if await automation_is_soft_deleted(auto_id, conn=conn):
        return None

    raw_uid = str(automation.get("user_id") or "").strip()
    is_company = (
        is_company_system_user(raw_uid)
        or str(automation.get("scope") or "").strip().lower() == "company"
    )
    subject_kind = "company_automation" if is_company else "personal_automation"

    if outcome_status == "completed":
        await bump_failure_notice_recovery_epoch_in_tx(
            conn,
            subject_kind=subject_kind,
            source_key=auto_id,
        )
        return None

    if outcome_status != "failed":
        return None

    reason = str(failure_reason or "failed").strip().lower() or "failed"
    auto_name = str(automation.get("name") or "").strip()
    return await enqueue_failure_notice_in_tx(
        conn,
        subject_kind=subject_kind,
        subject_id=str(execution_id),
        source_key=auto_id,
        recipient_user_id=None if is_company else raw_uid,
        failure_reason=reason,
        title=f"定时任务失败：{auto_name}" if auto_name else "定时任务执行失败提醒",
        detail=error,
        delivery=delivery,
        metadata={
            "automation_name": auto_name,
            "thread_id": thread_id,
            "workspace_id": workspace_id,
        },
    )


async def recompute_failure_notice_aggregate_in_tx(
    cur: Any,
    notice_id: str,
) -> str | None:
    """Recompute and persist ``dalton_failure_notices.aggregate_status`` from its targets."""
    await cur.execute(
        """
        SELECT notice_id::text AS notice_id, aggregate_status, summary_json
        FROM dalton_failure_notices
        WHERE notice_id = %s::uuid
        FOR UPDATE
        """,
        (notice_id,),
    )
    notice_row = await cur.fetchone()
    if not isinstance(notice_row, Mapping):
        return None

    await cur.execute(
        """
        SELECT status, error_code
        FROM dalton_failure_notice_targets
        WHERE notice_id = %s::uuid
        """,
        (notice_id,),
    )
    target_rows = list(await cur.fetchall() or [])
    if not target_rows:
        return str(notice_row["aggregate_status"])

    sent_count = 0
    failed_count = 0
    retrying_count = 0
    last_error_code: str | None = None
    for row in target_rows:
        st = str(row.get("status") or "").strip().lower()
        if st == "sent":
            sent_count += 1
        elif st == "terminal_failed":
            failed_count += 1
            if row.get("error_code"):
                last_error_code = str(row["error_code"])
        else:
            retrying_count += 1
            if row.get("error_code") and last_error_code is None:
                last_error_code = str(row["error_code"])

    if retrying_count > 0:
        new_agg = "retrying"
    elif failed_count == 0 and sent_count > 0:
        new_agg = "delivered"
    elif sent_count > 0 and failed_count > 0:
        new_agg = "partial_failed"
    else:
        new_agg = "failed"

    raw_summary = notice_row.get("summary_json")
    summary: dict[str, Any] = dict(raw_summary) if isinstance(raw_summary, Mapping) else {}
    summary.update(
        {
            "total_targets": len(target_rows),
            "sent_count": sent_count,
            "failed_count": failed_count,
            "retrying_count": retrying_count,
        }
    )
    if last_error_code:
        summary["last_error_code"] = last_error_code
    elif new_agg == "delivered":
        summary.pop("last_error_code", None)

    await cur.execute(
        """
        UPDATE dalton_failure_notices
        SET aggregate_status = %s,
            summary_json = %s::jsonb,
            updated_at = NOW()
        WHERE notice_id = %s::uuid
        """,
        (new_agg, json.dumps(summary, ensure_ascii=False), notice_id),
    )
    return new_agg


class _ClaimDeadlineExceeded(Exception):
    """Internal sentinel used to roll back a claim transaction if deadline passed."""


async def claim_due_failure_notice_targets(
    *,
    limit: int = 10,
    claim_ttl_seconds: int = FAILURE_NOTICE_CLAIM_TTL_SECONDS,
    deadline_monotonic: float | None = None,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Atomically claim due ``pending`` / ``retryable_failed`` targets with fresh ``claim_token``.

    When ``deadline_monotonic`` is supplied and has already been reached before or
    during the claim transaction, the transaction is rolled back so no target loses
    an attempt or holds an unstarted lease.
    """
    eff_limit = max(1, int(limit or 10))
    eff_ttl = max(5, int(claim_ttl_seconds or FAILURE_NOTICE_CLAIM_TTL_SECONDS))
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        return []
    try:
        async with get_db_connection(conn) as owned:
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                return []
            async with owned.transaction():
                async with owned.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        WITH due AS (
                            SELECT t.target_receipt_id,
                                   t.status AS prior_status,
                                   t.next_retry_at AS prior_next_retry_at,
                                   t.error_code AS prior_error_code,
                                   t.error_message AS prior_error_message
                            FROM dalton_failure_notice_targets t
                            WHERE t.status IN ('pending', 'retryable_failed')
                              AND (t.next_retry_at IS NULL OR t.next_retry_at <= NOW())
                              AND t.attempt_count < t.max_attempts
                            ORDER BY COALESCE(t.next_retry_at, t.created_at) ASC, t.created_at ASC
                            FOR UPDATE OF t SKIP LOCKED
                            LIMIT %s
                        )
                        UPDATE dalton_failure_notice_targets t
                        SET status = 'sending',
                            attempt_count = t.attempt_count + 1,
                            claim_token = gen_random_uuid(),
                            claim_expires_at = NOW() + (%s * INTERVAL '1 second'),
                            next_retry_at = NULL,
                            error_code = NULL,
                            error_message = NULL,
                            updated_at = NOW()
                        FROM due
                        WHERE t.target_receipt_id = due.target_receipt_id
                        RETURNING
                            t.target_receipt_id::text AS target_receipt_id,
                            t.notice_id::text AS notice_id,
                            t.channel,
                            t.target,
                            t.status,
                            t.attempt_count,
                            t.max_attempts,
                            t.claim_token::text AS claim_token,
                            t.claim_expires_at,
                            t.payload_json,
                            due.prior_status,
                            due.prior_next_retry_at,
                            due.prior_error_code,
                            due.prior_error_message
                        """,
                        (eff_limit, eff_ttl),
                    )
                    rows = list(await cur.fetchall() or [])
                if rows and deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                    raise _ClaimDeadlineExceeded()
    except _ClaimDeadlineExceeded:
        return []
    return [dict(r) for r in rows]


async def release_unstarted_failure_notice_claim(
    *,
    target_receipt_id: str,
    claim_token: str,
    prior_status: str | None = None,
    prior_next_retry_at: datetime | None = None,
    prior_error_code: str | None = None,
    prior_error_message: str | None = None,
    conn: Any = None,
) -> bool:
    """Return an unstarted ``sending`` lease without consuming an attempt."""
    tr_id = _normalize_uuid(target_receipt_id)
    tok = _normalize_uuid(claim_token)
    if tr_id is None or tok is None:
        return False
    norm_prior = str(prior_status or "").strip().lower()
    restored_status = norm_prior if norm_prior in ("pending", "retryable_failed") else None
    async with get_db_connection(conn) as owned:
        async with owned.transaction():
            async with owned.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_failure_notice_targets
                    SET status = COALESCE(
                            %s::text,
                            CASE
                                WHEN attempt_count <= 1 THEN 'pending'
                                ELSE 'retryable_failed'
                            END
                        ),
                        attempt_count = GREATEST(attempt_count - 1, 0),
                        claim_token = NULL,
                        claim_expires_at = NULL,
                        next_retry_at = COALESCE(%s, NOW()),
                        error_code = %s,
                        error_message = %s,
                        updated_at = NOW()
                    WHERE target_receipt_id = %s::uuid
                      AND status = 'sending'
                      AND claim_token = %s::uuid
                    """,
                    (
                        restored_status,
                        prior_next_retry_at,
                        prior_error_code,
                        prior_error_message,
                        tr_id,
                        tok,
                    ),
                )
                return cur.rowcount > 0


async def settle_failure_notice_target_attempt(
    *,
    target_receipt_id: str,
    claim_token: str,
    transport_res: Mapping[str, Any],
    conn: Any = None,
) -> str | None:
    """Apply gateway send outcome to a ``sending`` target fenced by ``claim_token``."""
    tr_id = _normalize_uuid(target_receipt_id)
    tok = _normalize_uuid(claim_token)
    if tr_id is None or tok is None:
        return None

    gw_status = str(transport_res.get("status") or "").strip().lower()
    raw_code = str(transport_res.get("error_code") or transport_res.get("code") or "").strip()
    err_code = redact_managed_tokens(raw_code)[:96] if raw_code else ""
    raw_msg = str(transport_res.get("error_message") or transport_res.get("message") or "").strip()
    err_msg = _sanitize_text(raw_msg, max_chars=256) if raw_msg else None
    uncertain = bool(transport_res.get("uncertain")) or gw_status in ("unknown", "in_flight")

    items = transport_res.get("items") if isinstance(transport_res.get("items"), list) else []
    item_partial = gw_status == "partial" or any(
        isinstance(it, Mapping) and str(it.get("status") or "").strip().lower() == "partial"
        for it in items
    )
    platform_message_ids: list[str] = []
    for it in items:
        if isinstance(it, Mapping) and isinstance(it.get("platform_message_ids"), list):
            for mid in it["platform_message_ids"]:
                if mid:
                    platform_message_ids.append(str(mid))
    if not platform_message_ids and isinstance(transport_res.get("platform_message_ids"), list):
        platform_message_ids = [str(m) for m in transport_res["platform_message_ids"] if m]

    sent_chunks = 0
    try:
        sent_chunks = int(transport_res.get("sent_chunks") or transport_res.get("items_sent") or 0)
    except (TypeError, ValueError):
        sent_chunks = 0

    if not uncertain and is_uncertain_notify_error_code(err_code) and not item_partial:
        uncertain = True

    async with get_db_connection(conn) as owned:
        async with owned.transaction():
            async with owned.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT target_receipt_id::text AS target_receipt_id,
                           notice_id::text AS notice_id,
                           status, attempt_count, max_attempts,
                           claim_token::text AS claim_token,
                           payload_json
                    FROM dalton_failure_notice_targets
                    WHERE target_receipt_id = %s::uuid
                    FOR UPDATE
                    """,
                    (tr_id,),
                )
                row = await cur.fetchone()
                if not isinstance(row, Mapping):
                    return None
                if (
                    str(row.get("status") or "") != "sending"
                    or _normalize_uuid(row.get("claim_token")) != tok
                ):
                    return None

                notice_id = str(row["notice_id"])
                attempt_count = int(row.get("attempt_count") or 0)
                max_attempts = max(1, int(row.get("max_attempts") or FAILURE_NOTICE_MAX_ATTEMPTS))
                payload = dict(row.get("payload_json") or {}) if isinstance(row.get("payload_json"), Mapping) else {}

                if gw_status == "sent" or item_partial:
                    if item_partial:
                        payload["partial_delivery"] = True
                    await cur.execute(
                        """
                        UPDATE dalton_failure_notice_targets
                        SET status = 'sent',
                            claim_token = NULL,
                            claim_expires_at = NULL,
                            next_retry_at = NULL,
                            error_code = NULL,
                            error_message = NULL,
                            platform_message_ids = %s::jsonb,
                            payload_json = %s::jsonb,
                            updated_at = NOW()
                        WHERE target_receipt_id = %s::uuid
                          AND status = 'sending'
                          AND claim_token = %s::uuid
                        """,
                        (
                            json.dumps(platform_message_ids, ensure_ascii=False),
                            json.dumps(payload, ensure_ascii=False),
                            tr_id,
                            tok,
                        ),
                    )
                    await recompute_failure_notice_aggregate_in_tx(cur, notice_id)
                    return "sent"

                if uncertain:
                    await cur.execute(
                        """
                        UPDATE dalton_failure_notice_targets
                        SET status = 'uncertain',
                            uncertain_since = COALESCE(uncertain_since, NOW()),
                            error_code = %s,
                            error_message = %s,
                            updated_at = NOW()
                        WHERE target_receipt_id = %s::uuid
                          AND status = 'sending'
                          AND claim_token = %s::uuid
                        """,
                        (err_code or "delivery_uncertain", err_msg, tr_id, tok),
                    )
                    await recompute_failure_notice_aggregate_in_tx(cur, notice_id)
                    return "uncertain"

                # Confirmed failure: if any chunks were already sent, stop resending.
                eff_code = err_code or "delivery_failed"
                if sent_chunks > 0:
                    payload["partial_delivery"] = True
                    new_status = "terminal_failed"
                    next_retry_s: int | None = None
                elif is_failure_notice_terminal_error_code(eff_code) or attempt_count >= max_attempts:
                    new_status = "terminal_failed"
                    next_retry_s = None
                else:
                    new_status = "retryable_failed"
                    next_retry_s = _retry_backoff_seconds(attempt_count)

                await cur.execute(
                    """
                    UPDATE dalton_failure_notice_targets
                    SET status = %s,
                        claim_token = NULL,
                        claim_expires_at = NULL,
                        next_retry_at = CASE
                            WHEN %s::integer IS NOT NULL THEN NOW() + (%s::integer * INTERVAL '1 second')
                            ELSE NULL
                        END,
                        error_code = %s,
                        error_message = %s,
                        payload_json = %s::jsonb,
                        updated_at = NOW()
                    WHERE target_receipt_id = %s::uuid
                      AND status = 'sending'
                      AND claim_token = %s::uuid
                    """,
                    (
                        new_status,
                        next_retry_s,
                        next_retry_s,
                        eff_code,
                        err_msg,
                        json.dumps(payload, ensure_ascii=False),
                        tr_id,
                        tok,
                    ),
                )
                await recompute_failure_notice_aggregate_in_tx(cur, notice_id)
                return new_status


async def reconcile_uncertain_failure_notice_targets(
    *,
    limit: int = 20,
    unknown_timeout_minutes: int = FAILURE_NOTICE_UNKNOWN_TIMEOUT_MINUTES,
    reconcile_backoff_seconds: int = FAILURE_NOTICE_UNCERTAIN_RECONCILE_BACKOFF_SECONDS,
    now: datetime | None = None,
    conn: Any = None,
) -> dict[str, int]:
    """Reconcile expired ``sending`` and ``uncertain`` targets against ``dalton_channel_deliveries``.

    Lock order matches ``authorize_and_claim_failure_notice``:
    1. Lock ``dalton_failure_notice_targets`` (``FOR UPDATE SKIP LOCKED``).
    2. Clear ``claim_token = NULL`` so any late gateway claim is denied with
       ``claim_expired_or_superseded``.
    3. Read ``dalton_channel_deliveries`` by ``failure_notice:{target_receipt_id}:item:0``:
       - ``sent`` -> ``sent``
       - ``partial`` -> ``sent`` with ``payload_json.partial_delivery = true`` (zero resend)
       - ``failed`` + ``sent_chunks == 0`` -> ``retryable_failed`` (if retryable & < max_attempts)
         else ``terminal_failed``
       - ``failed`` + ``sent_chunks > 0`` -> ``terminal_failed`` (stop resending)
       - missing row (``c IS NULL``) -> confirmed unreached after lease expiry + token fence ->
         ``retryable_failed`` if ``attempt_count < max_attempts`` else ``terminal_failed``
       - ``in_flight`` / ``unknown`` -> stay ``uncertain`` with ``next_retry_at`` backed off so
         older unsettled rows do not starve newer targets; after ``unknown_timeout_minutes`` (15m),
         seal as ``terminal_failed`` with ``error_code = 'delivery_outcome_unknown_timeout'``.
    """
    eff_limit = max(1, int(limit or 20))
    eff_timeout_m = max(1, int(unknown_timeout_minutes or FAILURE_NOTICE_UNKNOWN_TIMEOUT_MINUTES))
    eff_reconcile_backoff_s = max(
        1, int(reconcile_backoff_seconds or FAILURE_NOTICE_UNCERTAIN_RECONCILE_BACKOFF_SECONDS)
    )
    ref_now = now or datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)

    counts = {
        "reconciled": 0,
        "sent": 0,
        "retryable_failed": 0,
        "terminal_failed": 0,
        "still_uncertain": 0,
    }

    async with get_db_connection(conn) as owned:
        async with owned.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT target_receipt_id::text AS target_receipt_id
                FROM dalton_failure_notice_targets
                WHERE (status = 'sending' AND claim_expires_at <= %s)
                   OR (
                       status = 'uncertain'
                       AND (claim_expires_at IS NULL OR claim_expires_at <= %s)
                       AND (next_retry_at IS NULL OR next_retry_at <= %s)
                   )
                ORDER BY COALESCE(next_retry_at, claim_expires_at, uncertain_since, updated_at) ASC,
                         created_at ASC
                LIMIT %s
                """,
                (ref_now, ref_now, ref_now, eff_limit),
            )
            candidate_ids = [
                str(r["target_receipt_id"])
                for r in (await cur.fetchall() or [])
                if isinstance(r, Mapping) and r.get("target_receipt_id")
            ]

    for tr_id in candidate_ids:
        async with get_db_connection(conn) as owned:
            async with owned.transaction():
                async with owned.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT target_receipt_id::text AS target_receipt_id,
                               notice_id::text AS notice_id,
                               status, attempt_count, max_attempts,
                               claim_token::text AS claim_token,
                               claim_expires_at,
                               next_retry_at,
                               uncertain_since,
                               error_code, error_message,
                               payload_json
                        FROM dalton_failure_notice_targets
                        WHERE target_receipt_id = %s::uuid
                        FOR UPDATE SKIP LOCKED
                        """,
                        (tr_id,),
                    )
                    row = await cur.fetchone()
                    if not isinstance(row, Mapping):
                        continue
                    cur_status = str(row.get("status") or "").strip().lower()
                    exp_at = row.get("claim_expires_at")
                    if isinstance(exp_at, datetime) and exp_at.tzinfo is None:
                        exp_at = exp_at.replace(tzinfo=timezone.utc)
                    next_reconcile_at = row.get("next_retry_at")
                    if isinstance(next_reconcile_at, datetime) and next_reconcile_at.tzinfo is None:
                        next_reconcile_at = next_reconcile_at.replace(tzinfo=timezone.utc)
                    if cur_status == "sending":
                        if exp_at is None or exp_at > ref_now:
                            continue
                    elif cur_status == "uncertain":
                        if (exp_at is not None and exp_at > ref_now) or (
                            next_reconcile_at is not None and next_reconcile_at > ref_now
                        ):
                            continue
                    else:
                        continue

                    notice_id = str(row["notice_id"])
                    attempt_count = int(row.get("attempt_count") or 0)
                    max_attempts = max(1, int(row.get("max_attempts") or FAILURE_NOTICE_MAX_ATTEMPTS))
                    uncertain_since = row.get("uncertain_since") or ref_now
                    if isinstance(uncertain_since, datetime) and uncertain_since.tzinfo is None:
                        uncertain_since = uncertain_since.replace(tzinfo=timezone.utc)
                    payload = dict(row.get("payload_json") or {}) if isinstance(row.get("payload_json"), Mapping) else {}

                    # Fence any late gateway claim by invalidating claim_token under row lock
                    await cur.execute(
                        """
                        UPDATE dalton_failure_notice_targets
                        SET claim_token = NULL,
                            claim_expires_at = NULL,
                            uncertain_since = COALESCE(uncertain_since, %s),
                            updated_at = NOW()
                        WHERE target_receipt_id = %s::uuid
                        """,
                        (uncertain_since, tr_id),
                    )

                    d_key = failure_notice_delivery_key(tr_id)
                    await cur.execute(
                        """
                        SELECT status, sent_chunks, error_code, error_message, platform_message_ids
                        FROM dalton_channel_deliveries
                        WHERE delivery_key = %s
                        """,
                        (d_key,),
                    )
                    ch_row = await cur.fetchone()
                    counts["reconciled"] += 1

                    if ch_row is None:
                        # Lease expired + claim_token cleared + no channel row => confirmed zero-sent
                        if attempt_count < max_attempts:
                            new_st = "retryable_failed"
                            err_c = str(row.get("error_code") or "claim_timeout_unreached")
                            backoff_s = _retry_backoff_seconds(attempt_count)
                            counts["retryable_failed"] += 1
                        else:
                            new_st = "terminal_failed"
                            err_c = "attempts_exhausted"
                            backoff_s = None
                            counts["terminal_failed"] += 1
                        await cur.execute(
                            """
                            UPDATE dalton_failure_notice_targets
                            SET status = %s,
                                claim_token = NULL,
                                claim_expires_at = NULL,
                                next_retry_at = CASE
                                    WHEN %s::integer IS NOT NULL THEN %s + (%s::integer * INTERVAL '1 second')
                                    ELSE NULL
                                END,
                                error_code = %s,
                                updated_at = NOW()
                            WHERE target_receipt_id = %s::uuid
                            """,
                            (new_st, backoff_s, ref_now, backoff_s, err_c, tr_id),
                        )
                        await recompute_failure_notice_aggregate_in_tx(cur, notice_id)
                        continue

                    ch_status = str(ch_row.get("status") or "").strip().lower()
                    ch_sent_chunks = int(ch_row.get("sent_chunks") or 0)
                    ch_err_code = str(ch_row.get("error_code") or "").strip()
                    ch_err_msg = _sanitize_text(ch_row.get("error_message"), max_chars=256) or None
                    ch_msg_ids = (
                        list(ch_row.get("platform_message_ids") or [])
                        if isinstance(ch_row.get("platform_message_ids"), list)
                        else []
                    )

                    if ch_status in ("sent", "partial"):
                        if ch_status == "partial":
                            payload["partial_delivery"] = True
                        await cur.execute(
                            """
                            UPDATE dalton_failure_notice_targets
                            SET status = 'sent',
                                claim_token = NULL,
                                claim_expires_at = NULL,
                                next_retry_at = NULL,
                                error_code = NULL,
                                error_message = NULL,
                                platform_message_ids = %s::jsonb,
                                payload_json = %s::jsonb,
                                updated_at = NOW()
                            WHERE target_receipt_id = %s::uuid
                            """,
                            (
                                json.dumps(ch_msg_ids, ensure_ascii=False),
                                json.dumps(payload, ensure_ascii=False),
                                tr_id,
                            ),
                        )
                        await recompute_failure_notice_aggregate_in_tx(cur, notice_id)
                        counts["sent"] += 1
                        continue

                    if ch_status == "failed":
                        eff_code = ch_err_code or "delivery_failed"
                        if ch_sent_chunks > 0:
                            payload["partial_delivery"] = True
                            new_st = "terminal_failed"
                            backoff_s = None
                            counts["terminal_failed"] += 1
                        elif is_failure_notice_terminal_error_code(eff_code) or attempt_count >= max_attempts:
                            new_st = "terminal_failed"
                            backoff_s = None
                            counts["terminal_failed"] += 1
                        else:
                            new_st = "retryable_failed"
                            backoff_s = _retry_backoff_seconds(attempt_count)
                            counts["retryable_failed"] += 1
                        await cur.execute(
                            """
                            UPDATE dalton_failure_notice_targets
                            SET status = %s,
                                claim_token = NULL,
                                claim_expires_at = NULL,
                                next_retry_at = CASE
                                    WHEN %s::integer IS NOT NULL THEN %s + (%s::integer * INTERVAL '1 second')
                                    ELSE NULL
                                END,
                                error_code = %s,
                                error_message = %s,
                                payload_json = %s::jsonb,
                                updated_at = NOW()
                            WHERE target_receipt_id = %s::uuid
                            """,
                            (
                                new_st,
                                backoff_s,
                                ref_now,
                                backoff_s,
                                eff_code,
                                ch_err_msg,
                                json.dumps(payload, ensure_ascii=False),
                                tr_id,
                            ),
                        )
                        await recompute_failure_notice_aggregate_in_tx(cur, notice_id)
                        continue

                    # ch_status in ('in_flight', 'unknown') or other unsettled state
                    elapsed = ref_now - uncertain_since
                    if elapsed >= timedelta(minutes=eff_timeout_m):
                        await cur.execute(
                            """
                            UPDATE dalton_failure_notice_targets
                            SET status = 'terminal_failed',
                                claim_token = NULL,
                                claim_expires_at = NULL,
                                next_retry_at = NULL,
                                error_code = 'delivery_outcome_unknown_timeout',
                                error_message = %s,
                                updated_at = NOW()
                            WHERE target_receipt_id = %s::uuid
                            """,
                            (
                                f"Delivery outcome stayed {ch_status or 'unknown'} for >= {eff_timeout_m}m.",
                                tr_id,
                            ),
                        )
                        await recompute_failure_notice_aggregate_in_tx(cur, notice_id)
                        counts["terminal_failed"] += 1
                    else:
                        deadline_at = uncertain_since + timedelta(minutes=eff_timeout_m)
                        next_check_at = min(
                            ref_now + timedelta(seconds=eff_reconcile_backoff_s),
                            deadline_at,
                        )
                        await cur.execute(
                            """
                            UPDATE dalton_failure_notice_targets
                            SET status = 'uncertain',
                                claim_token = NULL,
                                claim_expires_at = NULL,
                                next_retry_at = %s,
                                uncertain_since = %s,
                                error_code = %s,
                                updated_at = NOW()
                            WHERE target_receipt_id = %s::uuid
                            """,
                            (next_check_at, uncertain_since, ch_status or "unknown", tr_id),
                        )
                        await recompute_failure_notice_aggregate_in_tx(cur, notice_id)
                        counts["still_uncertain"] += 1

    return counts


async def process_due_failure_notices(
    *,
    limit: int = 10,
    max_duration_s: float = 30.0,
    post_fn: Callable[..., Awaitable[dict[str, Any]]] = post_failure_notice_gateway_notify,
    conn: Any = None,
) -> dict[str, int]:
    """Run one bounded cycle: reconcile expired/uncertain targets, then claim & dispatch one target at a time.

    Each due target is claimed (consuming one attempt and starting its 60s lease) only
    immediately before its gateway request is initiated. When ``max_duration_s`` elapses,
    remaining due targets stay untouched in their existing status without consuming attempts.
    """
    if not is_failure_notice_enabled():
        return {"reconciled": 0, "claimed": 0, "sent": 0, "failed": 0, "uncertain": 0}
    if post_fn is post_failure_notice_gateway_notify and not has_capability(
        CapabilityId.CHANNEL_GATEWAY
    ):
        return {"reconciled": 0, "claimed": 0, "sent": 0, "failed": 0, "uncertain": 0}

    eff_limit = max(1, int(limit or 10))
    started = time.monotonic()
    deadline = started + max_duration_s
    reconcile_stats = await reconcile_uncertain_failure_notice_targets(limit=eff_limit, conn=conn)
    summary = {
        "reconciled": reconcile_stats["reconciled"],
        "claimed": 0,
        "sent": reconcile_stats["sent"],
        "failed": 0,
        "uncertain": reconcile_stats["still_uncertain"],
    }

    seen_ids: set[str] = set()
    for _ in range(eff_limit):
        if time.monotonic() >= deadline:
            break
        claimed_rows = await claim_due_failure_notice_targets(
            limit=1,
            deadline_monotonic=deadline,
            conn=conn,
        )
        if not claimed_rows:
            break
        row = claimed_rows[0]
        tr_id = str(row["target_receipt_id"])
        claim_tok = str(row["claim_token"])
        if time.monotonic() >= deadline:
            await release_unstarted_failure_notice_claim(
                target_receipt_id=tr_id,
                claim_token=claim_tok,
                prior_status=row.get("prior_status"),
                prior_next_retry_at=row.get("prior_next_retry_at"),
                prior_error_code=row.get("prior_error_code"),
                prior_error_message=row.get("prior_error_message"),
                conn=conn,
            )
            break
        if tr_id in seen_ids:
            await release_unstarted_failure_notice_claim(
                target_receipt_id=tr_id,
                claim_token=claim_tok,
                prior_status=row.get("prior_status"),
                prior_next_retry_at=row.get("prior_next_retry_at"),
                prior_error_code=row.get("prior_error_code"),
                prior_error_message=row.get("prior_error_message"),
                conn=conn,
            )
            break
        seen_ids.add(tr_id)
        summary["claimed"] += 1
        target = str(row["target"])
        try:
            res = await post_fn(
                tr_id,
                claim_tok,
                target=target,
            )
        except asyncio.CancelledError:
            await settle_failure_notice_target_attempt(
                target_receipt_id=tr_id,
                claim_token=claim_tok,
                transport_res={
                    "status": "unknown",
                    "uncertain": True,
                    "error_code": "delivery_uncertain",
                },
                conn=conn,
            )
            raise
        except Exception as exc:
            logger.warning(
                "[FAILURE_NOTICE] Dispatch failed for target_receipt_id=%s (%s)",
                tr_id,
                type(exc).__name__,
            )
            res = {
                "status": "unknown",
                "uncertain": True,
                "error_code": "delivery_uncertain",
            }
        final_st = await settle_failure_notice_target_attempt(
            target_receipt_id=tr_id,
            claim_token=claim_tok,
            transport_res=res if isinstance(res, Mapping) else {"status": "unknown", "uncertain": True},
            conn=conn,
        )
        if final_st == "sent":
            summary["sent"] += 1
        elif final_st == "uncertain":
            summary["uncertain"] += 1
        elif final_st in ("retryable_failed", "terminal_failed"):
            summary["failed"] += 1

    return summary


class FailureNoticeSweeper:
    """Lifespan-managed periodic scanner for ``dalton_failure_notice_targets``.

    Every backend worker may run one instance; PostgreSQL ``FOR UPDATE OF t SKIP LOCKED``
    ensures at-most-one worker claims each due target or reconciles each expired lease.
    When ``is_failure_notice_enabled()`` is False, ticks perform zero database queries.
    """

    _instance: "FailureNoticeSweeper | None" = None

    def __init__(
        self,
        *,
        interval_s: float = FAILURE_NOTICE_SWEEP_INTERVAL_SECONDS,
        limit: int = 10,
        max_duration_s: float = 25.0,
        process_fn: Callable[..., Awaitable[dict[str, int]]] | None = None,
    ) -> None:
        self._interval_s = max(0.05, float(interval_s))
        self._limit = max(1, int(limit))
        self._max_duration_s = max(1.0, float(max_duration_s))
        self._process_fn = process_fn
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._wake_event = asyncio.Event()

    @classmethod
    def get_instance(cls) -> "FailureNoticeSweeper":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stopping.clear()
        self._wake_event.clear()
        self._task = asyncio.create_task(self._run(), name="dalton-failure-notice-sweeper")
        logger.info("[FAILURE_NOTICE] Periodic sweeper started (interval=%.1fs)", self._interval_s)

    def nudge(self) -> None:
        if self.running:
            self._wake_event.set()

    async def stop(self) -> None:
        self._stopping.set()
        self._wake_event.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def run_once(self) -> dict[str, int]:
        if not is_failure_notice_enabled() or not has_capability(CapabilityId.CHANNEL_GATEWAY):
            return {"reconciled": 0, "claimed": 0, "sent": 0, "failed": 0, "uncertain": 0}
        fn = self._process_fn or process_due_failure_notices
        return await fn(limit=self._limit, max_duration_s=self._max_duration_s)

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "[FAILURE_NOTICE] Periodic sweep failed (%s)",
                    type(exc).__name__,
                )
            self._wake_event.clear()
            if self._stopping.is_set():
                break
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=self._interval_s)
            except asyncio.TimeoutError:
                pass


def wake_failure_notice_dispatcher() -> None:
    """Best-effort nudge on the local worker loop to drain newly enqueued failure notices."""
    global _dispatch_wake_task
    if not is_failure_notice_enabled() or not has_capability(CapabilityId.CHANNEL_GATEWAY):
        return
    sweeper = FailureNoticeSweeper._instance
    if sweeper is not None and sweeper.running:
        sweeper.nudge()
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _dispatch_wake_task is not None and not _dispatch_wake_task.done():
        return

    async def _run() -> None:
        try:
            await process_due_failure_notices(limit=10, max_duration_s=30.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[FAILURE_NOTICE] Background wake dispatch failed (%s)",
                type(exc).__name__,
            )

    _dispatch_wake_task = loop.create_task(_run(), name="dalton_failure_notice_wake")


async def load_failure_notice_summaries(
    subject_ids: Sequence[str],
    *,
    conn: Any = None,
) -> dict[str, dict[str, Any]]:
    """Batch-load compact failure-notice summaries keyed by lowercased ``subject_id``."""
    clean_ids = sorted({str(s).strip() for s in subject_ids if str(s or "").strip()})
    if not clean_ids:
        return {}

    async with get_db_connection(conn) as owned:
        async with owned.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    n.notice_id::text AS notice_id,
                    n.subject_kind,
                    n.subject_id,
                    n.failure_reason,
                    n.aggregate_status,
                    n.summary_json,
                    n.updated_at,
                    COALESCE(t.total_targets, 0) AS total_targets,
                    COALESCE(t.sent_count, 0) AS sent_count,
                    COALESCE(t.failed_count, 0) AS failed_count,
                    COALESCE(t.retrying_count, 0) AS retrying_count,
                    t.last_error_code
                FROM dalton_failure_notices n
                LEFT JOIN LATERAL (
                    SELECT
                        COUNT(*)::int AS total_targets,
                        COUNT(*) FILTER (WHERE status = 'sent')::int AS sent_count,
                        COUNT(*) FILTER (WHERE status = 'terminal_failed')::int AS failed_count,
                        COUNT(*) FILTER (
                            WHERE status IN ('pending', 'sending', 'uncertain', 'retryable_failed')
                        )::int AS retrying_count,
                        MAX(error_code) FILTER (WHERE error_code IS NOT NULL) AS last_error_code
                    FROM dalton_failure_notice_targets
                    WHERE notice_id = n.notice_id
                ) t ON TRUE
                WHERE lower(n.subject_id) = ANY(%s)
                  AND n.subject_kind IN ('personal_automation', 'company_automation', 'data_task')
                """,
                ([s.lower() for s in clean_ids],),
            )
            rows = list(await cur.fetchall() or [])

    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        sid = str(row.get("subject_id") or "").strip().lower()
        if not sid:
            continue
        summary_json = row.get("summary_json") if isinstance(row.get("summary_json"), Mapping) else {}
        result[sid] = {
            "notice_id": str(row["notice_id"]),
            "subject_kind": str(row["subject_kind"]),
            "status": str(row["aggregate_status"]),
            "failure_reason": str(row.get("failure_reason") or ""),
            "total_targets": int(row.get("total_targets") or 0),
            "sent_count": int(row.get("sent_count") or 0),
            "failed_count": int(row.get("failed_count") or 0),
            "retrying_count": int(row.get("retrying_count") or 0),
            "last_error_code": row.get("last_error_code") or summary_json.get("last_error_code"),
            "updated_at": row.get("updated_at"),
        }
    return result
