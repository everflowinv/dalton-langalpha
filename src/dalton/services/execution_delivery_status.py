"""Aggregate execution delivery status across ``dalton_notify_deliveries`` and ``delivery_result``.

Execution status itself (``completed`` / ``succeeded``) is never mutated. Instead,
reads compute an immutable ``delivery_summary`` describing whether delivery:
- was not configured / not applicable (``None``)
- succeeded for every active target (``delivered``)
- is still pending or has retries remaining (``retrying``)
- failed terminally for some targets while others succeeded (``partial_failed``)
- failed terminally for all active targets (``failed``)
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping, MutableMapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from psycopg.rows import dict_row

from src.dalton.notify.channel_transport import UNCERTAIN_NOTIFY_ERROR_CODES
from src.dalton.notify.dispatch import (
    CARD_LIMIT_EXCEEDED_ERROR_CODE,
    NON_RETRYABLE_TERMINAL_ERROR_CODES,
    is_card_limit_not_allowed,
)
from src.dalton.settings import is_dalton_enabled
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

DEFAULT_MAX_DELIVERY_ATTEMPTS = 3
DEFAULT_RETRY_LOOKBACK_HOURS = 24
DEFAULT_STALE_PENDING_MINUTES = 10
DELIVERY_SETTLE_SECONDS = 180

_FAILURE_ALERT_SOURCE = "automation_report_failure_alert"
_NON_RETRIED_NOTIFY_SOURCES = frozenset(
    {
        "notify_test",
        "report_delivery_admin_notice",
        "report_delivery_missing_profile_notice",
    }
)
_SYNCED_UNDELIVERED_RE = re.compile(r"已生成但未送达[（(]([^）)]+)[）)]")
_SYNCED_EXCERPT_SUFFIX_RE = re.compile(
    r"(?:(?:\s*\n+\s*|\s+)投递状态：已生成但未送达(?:[（(][^）)]*[）)])?|\A\s*投递状态：已生成但未送达(?:[（(][^）)]*[）)])?)\s*\Z"
)
_CARD_ELEMENT_LIMIT_RE = re.compile(
    r"(?:element\s+card\s+limit|exceeds\s+\d+\s+element\s+card\s+limit)",
    re.IGNORECASE,
)
_CARD_BYTE_LIMIT_RE = re.compile(
    r"(?:byte\s+card\s+limit|exceeds\s+\d+\s+byte\s+card\s+limit|30720|card\s+too\s+large)",
    re.IGNORECASE,
)
_FEISHU_OPEN_ID_RE = re.compile(r"^ou_[A-Za-z0-9_-]+$")
_FEISHU_CHAT_ID_RE = re.compile(r"^oc_[A-Za-z0-9_-]+$")
_DISCORD_SNOWFLAKE_RE = re.compile(r"^[0-9]{5,32}$")

CARD_ELEMENT_LIMIT_ERROR_CODE = "card_element_limit_exceeded"

_CARD_LIMIT_UPGRADEABLE_CODES = frozenset(
    {
        "",
        "not_allowed",
        CARD_LIMIT_EXCEEDED_ERROR_CODE,
        CARD_ELEMENT_LIMIT_ERROR_CODE,
    }
)

_ERROR_REASON_ZH: dict[str, str] = {
    CARD_LIMIT_EXCEEDED_ERROR_CODE: "卡片超过 30 KB 上限",
    CARD_ELEMENT_LIMIT_ERROR_CODE: "卡片元素数超过上限",
    "attempts_exhausted": "多次重试后仍投递失败",
    "target_failed": "目标通道投递失败",
    "delivery_failed": "目标通道投递失败",
    "delivery_uncertain": "投递状态未确认",
    "delivery_unconfirmed": "投递状态未确认",
    "unknown": "投递状态未确认",
    "in_flight": "投递状态未确认",
    "partial": "投递状态未确认",
    "gateway_restarted": "投递状态未确认",
    "feishu_cards_not_supported": "卡片包含不支持的图片或格式",
    "images_not_supported": "卡片包含不支持的图片或格式",
    "image_placeholder_not_supported": "卡片包含不支持的图片或格式",
    "missing_card": "投递内容为空",
    "missing_text": "投递内容为空",
    "unsupported_platform": "不支持的投递平台",
    "service_token_unconfigured": "通知网关未配置",
    "gateway_url_unconfigured": "通知网关未配置",
    "connecterror": "通知网关连接或服务异常",
    "connecttimeout": "通知网关连接或服务异常",
    "http_500": "通知网关连接或服务异常",
    "http_502": "通知网关连接或服务异常",
    "http_503": "通知网关连接或服务异常",
    "http_504": "通知网关连接或服务异常",
    "not_allowed": "投递被拒绝",
}

_ERROR_REASON_EN: dict[str, str] = {
    CARD_LIMIT_EXCEEDED_ERROR_CODE: "Card exceeds 30 KB limit",
    CARD_ELEMENT_LIMIT_ERROR_CODE: "Card exceeds element limit",
    "attempts_exhausted": "Delivery failed after retries",
    "target_failed": "Target channel delivery failed",
    "delivery_failed": "Target channel delivery failed",
    "delivery_uncertain": "Delivery could not be confirmed",
    "delivery_unconfirmed": "Delivery could not be confirmed",
    "unknown": "Delivery could not be confirmed",
    "in_flight": "Delivery could not be confirmed",
    "partial": "Delivery could not be confirmed",
    "gateway_restarted": "Delivery could not be confirmed",
    "feishu_cards_not_supported": "Card contains unsupported images or format",
    "images_not_supported": "Card contains unsupported images or format",
    "image_placeholder_not_supported": "Card contains unsupported images or format",
    "missing_card": "Delivery content is empty",
    "missing_text": "Delivery content is empty",
    "unsupported_platform": "Unsupported delivery platform",
    "service_token_unconfigured": "Notification gateway is not configured",
    "gateway_url_unconfigured": "Notification gateway is not configured",
    "connecterror": "Notification gateway unreachable or returned an error",
    "connecttimeout": "Notification gateway unreachable or returned an error",
    "http_500": "Notification gateway unreachable or returned an error",
    "http_502": "Notification gateway unreachable or returned an error",
    "http_503": "Notification gateway unreachable or returned an error",
    "http_504": "Notification gateway unreachable or returned an error",
    "not_allowed": "Delivery rejected",
}


def strip_synced_delivery_excerpt_suffix(excerpt: str | None) -> str | None:
    """Strip the fallback ``投递状态：已生成但未送达（...）`` suffix appended to ``result_excerpt``."""
    if not isinstance(excerpt, str):
        return None
    cleaned = _SYNCED_EXCERPT_SUFFIX_RE.sub("", excerpt).strip()
    return cleaned or None


def normalize_delivery_error_code(
    error_code: str | None,
    error_message: str | None = None,
) -> str | None:
    """Normalize raw gateway/dispatch codes and messages into a canonical failure code."""
    code = str(error_code or "").strip()
    msg = str(error_message or "").strip()

    if not code and msg:
        matched = _SYNCED_UNDELIVERED_RE.search(msg)
        if matched:
            code = matched.group(1).strip()

    code_l = code.lower()
    if code_l in _CARD_LIMIT_UPGRADEABLE_CODES:
        if _CARD_ELEMENT_LIMIT_RE.search(msg):
            return CARD_ELEMENT_LIMIT_ERROR_CODE
        if is_card_limit_not_allowed(code_l, msg) or _CARD_BYTE_LIMIT_RE.search(msg):
            return CARD_LIMIT_EXCEEDED_ERROR_CODE
    if code_l in _ERROR_REASON_ZH:
        return code_l
    if code:
        return code_l
    return None


def humanize_delivery_error(
    error_code: str | None,
    error_message: str | None = None,
    *,
    lang: Literal["zh", "en"] = "zh",
) -> str | None:
    """Map a delivery error code/message to a concise human-readable reason."""
    table = _ERROR_REASON_ZH if lang == "zh" else _ERROR_REASON_EN
    normalized = normalize_delivery_error_code(error_code, error_message)
    if normalized and normalized in table:
        return table[normalized]

    msg = str(error_message or "").strip()
    if msg:
        matched = _SYNCED_UNDELIVERED_RE.search(msg)
        if matched:
            inner = matched.group(1).strip()
            inner_l = inner.lower()
            if inner_l in table:
                return table[inner_l]
            return inner
        return msg
    if normalized:
        return normalized
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _within_settle_window(completed_at: Any, now: datetime | None) -> bool:
    ended = _parse_datetime(completed_at)
    if ended is None:
        return False
    ref = now or datetime.now(UTC)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    elapsed = (ref - ended).total_seconds()
    return 0 <= elapsed < DELIVERY_SETTLE_SECONDS


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _within_retry_window(
    row: Mapping[str, Any],
    *,
    completed_at: Any = None,
    now: datetime | None = None,
    lookback_hours: int = DEFAULT_RETRY_LOOKBACK_HOURS,
) -> bool:
    """Return True if a notify row is still inside ``retry_failed_notify_deliveries``'s lookback window."""
    anchor = (
        _parse_datetime(row.get("created_at"))
        or _parse_datetime(row.get("updated_at"))
        or _parse_datetime(completed_at)
    )
    if anchor is None:
        return True
    ref = now or datetime.now(UTC)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    return (ref - anchor) < timedelta(hours=max(1, int(lookback_hours)))


def _is_failure_alert_row(row: Mapping[str, Any]) -> bool:
    payload = row.get("payload_json")
    return (
        isinstance(payload, Mapping)
        and str(payload.get("source") or "").strip() == _FAILURE_ALERT_SOURCE
    )


def _notify_row_source(row: Mapping[str, Any]) -> str:
    payload = row.get("payload_json")
    if isinstance(payload, Mapping):
        return str(payload.get("source") or "").strip().lower()
    return ""


def _is_pending_stale(
    row: Mapping[str, Any],
    *,
    now: datetime | None = None,
    stale_pending_minutes: int = DEFAULT_STALE_PENDING_MINUTES,
) -> bool:
    """Return True when a pending notify row has exceeded the sender's stale-pending threshold."""
    updated = _parse_datetime(row.get("updated_at")) or _parse_datetime(row.get("created_at"))
    if updated is None:
        return False
    ref = now or datetime.now(UTC)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    return (ref - updated) >= timedelta(minutes=max(1, int(stale_pending_minutes)))


def _is_reconcile_only_transport(
    row: Mapping[str, Any],
    *,
    notify_status: Any = None,
) -> bool:
    """Return True when uncertain pending rows are reconciled read-only and never resent."""
    auto_id = str(row.get("automation_id") or "").strip().lower()
    if auto_id.startswith("data:"):
        return True
    payload = row.get("payload_json")
    if isinstance(payload, Mapping):
        route_key = str(payload.get("route_key") or "").strip().lower()
        if route_key.startswith("data:"):
            return True
        source = str(payload.get("source") or "").strip().lower()
        if source in ("automation_report", "automation_report_failure_alert"):
            return True
        transport = str(payload.get("transport") or "").strip().lower()
        if transport == "gateway":
            return True
    ns = str(notify_status or "").strip().lower()
    return ns in ("pending_dispatch", "dispatched", "failed", "skipped", "preview_only")


def _summarize_script_notify_status(
    notify_status: Any,
) -> dict[str, Any] | None:
    """Fallback summary for company scripts when no target delivery rows exist yet."""
    ns = str(notify_status or "").strip().lower()
    if ns == "pending_dispatch":
        return {
            "status": "retrying",
            "total_targets": 0,
            "sent_count": 0,
            "failed_count": 0,
            "retrying_count": 0,
            "failure_code": None,
            "failure_reason": None,
            "failure_reasons": [],
            "targets": [],
        }
    if ns == "failed":
        reason_zh = humanize_delivery_error("delivery_failed", lang="zh")
        return {
            "status": "failed",
            "total_targets": 0,
            "sent_count": 0,
            "failed_count": 0,
            "retrying_count": 0,
            "failure_code": "delivery_failed",
            "failure_reason": reason_zh,
            "failure_reasons": [reason_zh] if reason_zh else [],
            "targets": [],
        }
    return None


def summarize_execution_delivery(
    *,
    notify_rows: Sequence[Mapping[str, Any]] = (),
    delivery_result: Sequence[Mapping[str, Any]] | None = None,
    completed_at: Any = None,
    notify_status: Any = None,
    max_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS,
    retry_lookback_hours: int = DEFAULT_RETRY_LOOKBACK_HOURS,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Compute canonical delivery status summary for one execution.

    Returns ``None`` when no active delivery targets exist (no delivery configured
    or only skipped rows).
    """
    bounded_max = max(1, int(max_attempts))
    is_script_pending_dispatch = (
        str(notify_status or "").strip().lower() == "pending_dispatch"
    )
    targets: list[dict[str, Any]] = []
    has_notify_rows = False
    seen_notify_keys: dict[tuple[str, str], dict[str, Any]] = {}
    seen_normalized_targets: dict[tuple[str, str, str], dict[str, Any]] = {}

    for raw in notify_rows or ():
        if not isinstance(raw, Mapping) or _is_failure_alert_row(raw):
            continue
        st = str(raw.get("status") or "").strip().lower()
        if st in ("sent", "pending", "failed", "skipped"):
            has_notify_rows = True
        if st == "skipped":
            continue
        channel = str(raw.get("channel") or "").strip().lower() or None
        target = str(raw.get("target") or "").strip() or None
        raw_code = str(raw.get("error_code") or "").strip()
        raw_code_l = raw_code.lower()
        ch_err_code = str(raw.get("channel_error_code") or "").strip()
        ch_err_msg = str(raw.get("channel_error_message") or "").strip()
        attempt = _safe_int(raw.get("attempt_count"), 0)
        eff_code = normalize_delivery_error_code(
            raw_code or ch_err_code or None,
            ch_err_msg or None,
        )
        source = _notify_row_source(raw)
        within_window = _within_retry_window(
            raw,
            completed_at=completed_at,
            now=now,
            lookback_hours=retry_lookback_hours,
        )
        # Company script scheduler scans all `notify_status = 'pending_dispatch'` runs
        # without a 24h created_at cutoff, subject to attempt_count < max_attempts.
        within_retry_eligibility = within_window or is_script_pending_dispatch
        source_retried = source not in _NON_RETRIED_NOTIFY_SOURCES
        reconcile_only_transport = _is_reconcile_only_transport(
            raw, notify_status=notify_status
        )

        if st == "sent":
            target_status = "sent"
            reason_zh = None
        elif st == "pending":
            if raw_code_l in UNCERTAIN_NOTIFY_ERROR_CODES:
                # Gateway / report / data-task rows with uncertain error codes are never
                # resent by the sender — only reconciled read-only against receipts.
                # Legacy bridge rows are only retried while eligible and under the attempt cap.
                can_retry_pending = (
                    not reconcile_only_transport
                    and within_retry_eligibility
                    and source_retried
                    and attempt < bounded_max
                )
            elif attempt >= bounded_max:
                # Claim cap exhausted (dispatch.py:1511 forbids another claim/send):
                # treat unresolved pending immediately as unconfirmed instead of waiting 10m.
                can_retry_pending = False
            elif attempt == 0:
                can_retry_pending = within_retry_eligibility and source_retried
            elif not _is_pending_stale(raw, now=now):
                can_retry_pending = True
            else:
                can_retry_pending = (
                    within_retry_eligibility
                    and source_retried
                    and attempt < bounded_max
                )
            if can_retry_pending:
                target_status = "retrying"
                reason_zh = None
            else:
                target_status = "failed"
                eff_code = eff_code or raw_code_l or "delivery_uncertain"
                reason_zh = humanize_delivery_error(
                    eff_code,
                    ch_err_msg or None,
                    lang="zh",
                )
        elif st == "failed":
            can_retry_failed = (
                within_retry_eligibility
                and source_retried
                and attempt < bounded_max
                and eff_code not in NON_RETRYABLE_TERMINAL_ERROR_CODES
                and eff_code != CARD_ELEMENT_LIMIT_ERROR_CODE
            )
            if can_retry_failed:
                target_status = "retrying"
                reason_zh = None
            else:
                target_status = "failed"
                reason_zh = humanize_delivery_error(
                    eff_code or raw_code or ch_err_code or None,
                    ch_err_msg or None,
                    lang="zh",
                )
        else:
            continue

        target_entry: dict[str, Any] = {
            "target": target,
            "channel": channel,
            "name": None,
            "status": target_status,
            "error_code": eff_code if target_status == "failed" else None,
            "error_message": reason_zh if target_status == "failed" else None,
            "attempt_count": attempt,
            "max_attempts": bounded_max,
        }
        targets.append(target_entry)
        if channel or target:
            seen_notify_keys[(channel or "", target or "")] = target_entry
        norm_key = _parse_normalized_target(target, channel)
        if norm_key is not None:
            seen_normalized_targets[norm_key] = target_entry

    for raw in delivery_result or ():
        if not isinstance(raw, Mapping):
            continue
        method = str(raw.get("method") or "").strip()
        address = str(raw.get("address") or "").strip() or None
        name = str(raw.get("name") or "").strip() or None
        raw_err = str(raw.get("error") or "").strip()
        raw_err_code = str(raw.get("error_code") or "").strip() or None

        # Avoid double-counting failures mirrored by sync_terminal_undelivered_execution
        # when authoritative dalton_notify_deliveries rows were already loaded.
        if seen_notify_keys:
            matched_notify = seen_notify_keys.get((method.lower(), address or ""))
            if matched_notify is None:
                dr_channel = method.split(":", 1)[0].lower() if method else None
                dr_norm = _parse_normalized_target(address or method, dr_channel)
                if dr_norm is not None:
                    matched_notify = seen_normalized_targets.get(dr_norm)
            if matched_notify is not None:
                if name and not matched_notify.get("name"):
                    matched_notify["name"] = name
                continue
            if raw_err and _SYNCED_UNDELIVERED_RE.search(raw_err):
                continue

        if raw.get("success") is True:
            target_status = "sent"
            eff_code = None
            reason_zh = None
        else:
            explicit_status = str(raw.get("status") or "").strip().lower()
            raw_retrying = raw.get("retrying")
            if raw_retrying is False:
                is_retrying = False
            elif raw_retrying is True or explicit_status in ("pending", "retrying"):
                is_retrying = (
                    True
                    if completed_at is None
                    else _within_settle_window(completed_at, now)
                )
            else:
                is_retrying = False
            if is_retrying:
                target_status = "retrying"
                eff_code = None
                reason_zh = None
            else:
                target_status = "failed"
                eff_code = normalize_delivery_error_code(raw_err_code, raw_err or None)
                reason_zh = humanize_delivery_error(
                    eff_code or raw_err_code,
                    raw_err or None,
                    lang="zh",
                )

        targets.append(
            {
                "target": address or method or None,
                "channel": method.split(":", 1)[0].lower() if method else None,
                "name": name,
                "status": target_status,
                "error_code": eff_code,
                "error_message": reason_zh,
                "attempt_count": _safe_int(raw.get("attempt_count"), 1 if target_status != "retrying" else 0),
                "max_attempts": bounded_max,
            }
        )

    if not targets:
        if has_notify_rows:
            return None
        return _summarize_script_notify_status(notify_status)

    sent_count = sum(1 for item in targets if item["status"] == "sent")
    failed_items = [item for item in targets if item["status"] == "failed"]
    failed_count = len(failed_items)
    retrying_count = sum(1 for item in targets if item["status"] == "retrying")
    total_targets = len(targets)

    if retrying_count > 0:
        status: Literal["delivered", "failed", "partial_failed", "retrying"] = "retrying"
    elif failed_count == 0:
        status = "delivered"
    elif sent_count > 0:
        status = "partial_failed"
    else:
        status = "failed"

    failure_reasons: list[str] = []
    first_code: str | None = None
    for item in failed_items:
        if first_code is None and item.get("error_code"):
            first_code = str(item["error_code"])
        reason = str(item.get("error_message") or "").strip()
        if reason and reason not in failure_reasons:
            failure_reasons.append(reason)

    return {
        "status": status,
        "total_targets": total_targets,
        "sent_count": sent_count,
        "failed_count": failed_count,
        "retrying_count": retrying_count,
        "failure_code": first_code if status in ("failed", "partial_failed") else None,
        "failure_reason": (
            failure_reasons[0]
            if failure_reasons and status in ("failed", "partial_failed")
            else None
        ),
        "failure_reasons": failure_reasons if status in ("failed", "partial_failed") else [],
        "targets": targets,
    }


def _parse_normalized_target(
    raw_target: str | None,
    fallback_channel: str | None = None,
) -> tuple[str, str, str] | None:
    """Normalize a notify target or channel ``target_address`` to ``(platform, kind, ident)``."""
    cleaned = str(raw_target or "").strip()
    if not cleaned:
        return None
    parts = cleaned.split(":")
    if len(parts) == 1:
        platform = str(fallback_channel or "").strip().lower()
        if not platform:
            return None
        ident = parts[0].strip()
        if not ident:
            return None
        if ident == "@me":
            return (platform, "dm", "@me")
        if platform == "feishu" and _FEISHU_OPEN_ID_RE.match(ident):
            return ("feishu", "dm", ident)
        if platform == "feishu" and _FEISHU_CHAT_ID_RE.match(ident):
            return ("feishu", "group", ident)
        if platform == "discord" and _DISCORD_SNOWFLAKE_RE.match(ident):
            return ("discord", "group", ident)
        return (platform, "raw", ident)

    platform = parts[0].strip().lower()
    if not platform:
        return None
    if len(parts) == 2:
        second = parts[1].strip()
        if not second:
            return None
        if second == "@me":
            return (platform, "dm", "@me")
        if platform == "feishu" and _FEISHU_OPEN_ID_RE.match(second):
            return ("feishu", "dm", second)
        if platform == "feishu" and _FEISHU_CHAT_ID_RE.match(second):
            return ("feishu", "group", second)
        if platform == "discord" and _DISCORD_SNOWFLAKE_RE.match(second):
            return ("discord", "group", second)
        return (platform, "raw", second)

    subkind = parts[1].strip().lower()
    ident = ":".join(parts[2:]).strip()
    if not ident:
        return None
    if subkind == "user":
        return (platform, "dm", ident)
    if subkind in ("chat", "channel", "group"):
        return (platform, "group", ident)
    return (platform, "raw", ":".join(parts[1:]).strip())


def _channel_row_matches_target(
    notify_targets: list[tuple[str, str, str]],
    ch_row: Mapping[str, Any],
    *,
    reconcile_recipient_id: str | None = None,
) -> bool:
    ch_plat = str(ch_row.get("platform") or "").strip().lower() or None
    ch_parsed = _parse_normalized_target(ch_row.get("target_address"), ch_plat)
    if ch_parsed is None or not notify_targets:
        return False

    c_plat, c_kind, c_ident = ch_parsed
    dkey = str(ch_row.get("delivery_key") or "").strip()
    dkey_parts = set(dkey.split(":")) if dkey else set()

    for n_plat, n_kind, n_ident in notify_targets:
        if n_plat != c_plat:
            continue
        if (n_kind, n_ident) == (c_kind, c_ident):
            return True
        if n_kind == "dm" and c_kind == "dm" and (n_ident == "@me" or c_ident == "@me"):
            expected_recipient = (
                reconcile_recipient_id
                or (n_ident if n_ident != "@me" else None)
                or (c_ident if c_ident != "@me" else None)
            )
            if expected_recipient and dkey_parts:
                # If delivery_key embeds a specific recipient id, require it to match
                # so two DMs on the same platform never borrow each other's error.
                key_recipients = {
                    part
                    for part in dkey_parts
                    if _FEISHU_OPEN_ID_RE.match(part) or _DISCORD_SNOWFLAKE_RE.match(part)
                }
                if key_recipients and expected_recipient not in key_recipients:
                    continue
            return True
    return False


def _match_channel_error(
    notify_row: Mapping[str, Any],
    channel_rows_by_run: Mapping[str, list[Mapping[str, Any]]],
) -> tuple[str | None, str | None]:
    delivery_id = str(notify_row.get("delivery_id") or "").strip().lower()
    exec_id = str(notify_row.get("execution_id") or "").strip().lower()
    channel = str(notify_row.get("channel") or "").strip().lower() or None
    payload = notify_row.get("payload_json")
    reconcile_keys: set[str] = set()
    reconcile_canonical_target: str | None = None
    reconcile_recipient_id: str | None = None
    if isinstance(payload, Mapping):
        raw_keys = payload.get("reconcile_delivery_keys")
        if isinstance(raw_keys, list):
            reconcile_keys = {str(k).strip() for k in raw_keys if str(k or "").strip()}
        raw_canon = str(payload.get("reconcile_canonical_target") or "").strip()
        if raw_canon:
            reconcile_canonical_target = raw_canon
        raw_recipient = str(payload.get("reconcile_recipient_id") or "").strip()
        if raw_recipient:
            reconcile_recipient_id = raw_recipient

    notify_targets: list[tuple[str, str, str]] = []
    for raw_t in (reconcile_canonical_target, notify_row.get("target")):
        parsed = _parse_normalized_target(raw_t, channel)
        if parsed is not None and parsed not in notify_targets:
            notify_targets.append(parsed)

    candidates: list[Mapping[str, Any]] = []
    if delivery_id and delivery_id in channel_rows_by_run:
        candidates.extend(channel_rows_by_run[delivery_id])
    if exec_id and exec_id in channel_rows_by_run:
        for ch_row in channel_rows_by_run[exec_id]:
            dkey = str(ch_row.get("delivery_key") or "").strip()
            if reconcile_keys:
                if dkey in reconcile_keys:
                    candidates.append(ch_row)
            elif _channel_row_matches_target(
                notify_targets,
                ch_row,
                reconcile_recipient_id=reconcile_recipient_id,
            ):
                candidates.append(ch_row)

    for ch_row in candidates:
        msg = str(ch_row.get("error_message") or "").strip()
        code = str(ch_row.get("error_code") or "").strip()
        if str(ch_row.get("status") or "").strip().lower() == "failed" and (msg or code):
            return code or None, msg or None
    for ch_row in candidates:
        msg = str(ch_row.get("error_message") or "").strip()
        code = str(ch_row.get("error_code") or "").strip()
        if msg or code:
            return code or None, msg or None
    return None, None


async def load_execution_notify_rows(
    execution_ids: Sequence[str],
    *,
    conn: Any = None,
) -> dict[str, list[dict[str, Any]]]:
    """Batch-load ``dalton_notify_deliveries`` rows (and channel error messages) by execution id."""
    cleaned_ids = sorted(
        {str(eid).strip().lower() for eid in execution_ids if str(eid or "").strip()}
    )
    if not cleaned_ids:
        return {}

    async with (
        get_db_connection(conn) as owned,
        owned.cursor(row_factory=dict_row) as cur,
    ):
        await cur.execute(
            """
            SELECT delivery_id::text AS delivery_id,
                   execution_id,
                   automation_id,
                   channel,
                   target,
                   status,
                   attempt_count,
                   error_code,
                   payload_json,
                   created_at,
                   updated_at
            FROM dalton_notify_deliveries
            WHERE execution_id = ANY(%s)
              AND COALESCE(payload_json->>'source', '') <> 'automation_report_failure_alert'
            ORDER BY created_at ASC, delivery_id ASC
            """,
            (cleaned_ids,),
        )
        raw_rows = [dict(r) for r in (await cur.fetchall() or [])]

        failed_run_ids: set[str] = set()
        for row in raw_rows:
            if str(row.get("status") or "").strip().lower() == "failed":
                eid = str(row.get("execution_id") or "").strip().lower()
                did = str(row.get("delivery_id") or "").strip().lower()
                if eid:
                    failed_run_ids.add(eid)
                if did:
                    failed_run_ids.add(did)

        channel_rows_by_run: dict[str, list[dict[str, Any]]] = {}
        if failed_run_ids:
            try:
                await cur.execute(
                    """
                    SELECT delivery_key,
                           run_id,
                           platform,
                           target_address,
                           status,
                           error_code,
                           error_message
                    FROM dalton_channel_deliveries
                    WHERE run_id = ANY(%s)
                    ORDER BY updated_at DESC, created_at DESC
                    """,
                    (sorted(failed_run_ids),),
                )
                for ch_row in await cur.fetchall() or []:
                    ch_dict = dict(ch_row)
                    rid = str(ch_dict.get("run_id") or "").strip().lower()
                    if rid:
                        channel_rows_by_run.setdefault(rid, []).append(ch_dict)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - optional gateway error-message enrichment
                logger.debug(
                    "[EXEC_DELIVERY_STATUS] Skipping dalton_channel_deliveries detail lookup: %s",
                    type(exc).__name__,
                )

    by_exec: dict[str, list[dict[str, Any]]] = {}
    for row in raw_rows:
        eid = str(row.get("execution_id") or "").strip().lower()
        if not eid:
            continue
        if channel_rows_by_run and str(row.get("status") or "").strip().lower() == "failed":
            ch_code, ch_msg = _match_channel_error(row, channel_rows_by_run)
            if ch_code:
                row["channel_error_code"] = ch_code
            if ch_msg:
                row["channel_error_message"] = ch_msg
        by_exec.setdefault(eid, []).append(row)
    return by_exec


def _get_field(item: Any, key: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(key)
    return getattr(item, key, None)


def _set_delivery_summary(item: Any, summary: dict[str, Any] | None) -> None:
    if isinstance(item, MutableMapping):
        item["delivery_summary"] = summary
        return
    if hasattr(item, "delivery_summary"):
        if summary is None:
            item.delivery_summary = None
        else:
            from src.server.models.automation import ExecutionDeliverySummary

            item.delivery_summary = ExecutionDeliverySummary.model_validate(summary)


async def enrich_executions_with_delivery_summary(
    executions: Sequence[Any],
    *,
    max_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS,
    retry_lookback_hours: int = DEFAULT_RETRY_LOOKBACK_HOURS,
    conn: Any = None,
    now: datetime | None = None,
) -> None:
    """Attach ``delivery_summary`` onto each execution dict or response model in-place."""
    if not executions:
        return

    exec_ids: list[str] = []
    for item in executions:
        raw_id = _get_field(item, "automation_execution_id") or _get_field(item, "run_id")
        if raw_id is not None:
            clean = str(raw_id).strip().lower()
            if clean:
                exec_ids.append(clean)

    notify_by_exec: dict[str, list[dict[str, Any]]] = {}
    if exec_ids and is_dalton_enabled():
        try:
            notify_by_exec = await load_execution_notify_rows(exec_ids, conn=conn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - delivery enrichment must not fail execution reads
            logger.warning(
                "[EXEC_DELIVERY_STATUS] Failed loading dalton_notify_deliveries for %d execution(s): %s",
                len(exec_ids),
                type(exc).__name__,
            )

    for item in executions:
        raw_id = _get_field(item, "automation_execution_id") or _get_field(item, "run_id")
        eid = str(raw_id).strip().lower() if raw_id is not None else ""
        summary = summarize_execution_delivery(
            notify_rows=notify_by_exec.get(eid, ()),
            delivery_result=_get_field(item, "delivery_result"),
            completed_at=_get_field(item, "completed_at"),
            notify_status=_get_field(item, "notify_status"),
            max_attempts=max_attempts,
            retry_lookback_hours=retry_lookback_hours,
            now=now,
        )
        _set_delivery_summary(item, summary)


async def enrich_automations_with_delivery_summary(
    automations: Sequence[Any],
    *,
    max_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS,
    retry_lookback_hours: int = DEFAULT_RETRY_LOOKBACK_HOURS,
    conn: Any = None,
    now: datetime | None = None,
) -> None:
    """Enrich ``last_execution`` across a sequence of automation dicts or response models."""
    executions = []
    for auto in automations:
        last = _get_field(auto, "last_execution")
        if last is not None:
            executions.append(last)
    if executions:
        await enrich_executions_with_delivery_summary(
            executions,
            max_attempts=max_attempts,
            retry_lookback_hours=retry_lookback_hours,
            conn=conn,
            now=now,
        )


def resolve_execution_delivery_summary(
    exec_row: Mapping[str, Any] | Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Read an existing ``delivery_summary`` or derive one from ``delivery_result`` / ``notify_status``."""
    existing = _get_field(exec_row, "delivery_summary")
    if isinstance(existing, Mapping):
        return dict(existing)
    if existing is not None and hasattr(existing, "model_dump"):
        return existing.model_dump()
    return summarize_execution_delivery(
        delivery_result=_get_field(exec_row, "delivery_result"),
        completed_at=_get_field(exec_row, "completed_at"),
        notify_status=_get_field(exec_row, "notify_status"),
        now=now,
    )


def format_execution_delivery_note(
    exec_row: Mapping[str, Any] | Any,
    *,
    lang: Literal["zh", "en"] = "zh",
) -> str | None:
    """Format a completed execution's delivery warning/progress phrase, or None if OK/unconfigured."""
    status = str(_get_field(exec_row, "status") or "").strip().lower()
    if status not in ("completed", "succeeded"):
        return None
    summary = resolve_execution_delivery_summary(exec_row)
    if not summary:
        return None
    d_status = str(summary.get("status") or "").strip().lower()
    if d_status == "delivered":
        return None

    reason = humanize_delivery_error(
        summary.get("failure_code"),
        summary.get("failure_reason"),
        lang=lang,
    )
    failed_count = _safe_int(summary.get("failed_count"), 0)
    total_targets = _safe_int(summary.get("total_targets"), 0)

    if d_status == "failed":
        if lang == "zh":
            return f"已完成 · 投递失败（{reason}）" if reason else "已完成 · 投递失败"
        return f"finished · delivery failed ({reason})" if reason else "finished · delivery failed"

    if d_status == "partial_failed":
        count_part = f"{failed_count}/{total_targets}" if total_targets > 0 else ""
        if lang == "zh":
            if count_part and reason:
                return f"已完成 · 部分投递失败（{count_part}：{reason}）"
            if reason:
                return f"已完成 · 部分投递失败（{reason}）"
            if count_part:
                return f"已完成 · 部分投递失败（{count_part}）"
            return "已完成 · 部分投递失败"
        if count_part and reason:
            return f"finished · partial delivery failed ({count_part}: {reason})"
        if reason:
            return f"finished · partial delivery failed ({reason})"
        if count_part:
            return f"finished · partial delivery failed ({count_part})"
        return "finished · partial delivery failed"

    if d_status == "retrying":
        return "已完成 · 投递中" if lang == "zh" else "finished · delivering"

    return None
