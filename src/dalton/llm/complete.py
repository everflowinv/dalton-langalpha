"""Budget-gated, idempotent server-side one-shot LLM wrapper (`dalton_complete` / `journaled_complete`, G1.3, P0N.7)."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import time
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Type

from pydantic import BaseModel, ValidationError

from src.dalton.budget.port import (
    format_daily_budget_exhausted_message,
    format_task_cap_exhausted_message,
)
from src.dalton.budget.service import (
    _is_subscription_model,
    _resolve_provider_for_model,
    reconcile_and_summarize_day,
    resolve_user_budget_config,
)
from src.dalton.database.budget import (
    fetch_scoped_task_spend,
    resolve_effective_task_cap,
)
from src.dalton.database.llm_journal import (
    claim_journal_invocation,
    complete_journal_done,
    complete_journal_failed,
    has_reported_token_usage,
    inspect_journal_state,
    is_journal_response_replayable,
    record_journal_replay,
)
from src.dalton.credentials import (  # DALTON-HOOK(team-credentials): record credential_user_id in journal usage while user_id stays caller
    credential_user_id as _resolve_credential_user_id,
    is_team_credential_sharing_enabled,
)
from src.dalton.database.provider_events import record_provider_event, sanitize_error_text
from src.dalton.llm.overlay import load_dalton_meta, load_overlay_data
from src.dalton.settings import get_dalton_settings
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)


class JournalError(RuntimeError):
    """Base error for journaled one-shot LLM calls."""

    def __init__(
        self,
        message: str,
        *,
        error_kind: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.status_code = status_code


class JournalInFlight(JournalError):
    """Another worker is currently executing the same invocation."""


class JournalIndeterminate(JournalError):
    """Prior invocation was interrupted after send; auto-resend is forbidden without confirmation."""


class JournalConflict(JournalError):
    """Same `invocation_id` was called with a different `request_hash`."""


class BudgetExhaustedError(JournalError):
    """Daily cash budget or scoped task cap is exhausted."""


@dataclass
class DaltonCompleteResult:
    """Result envelope returned by `dalton_complete` / `journaled_complete`."""

    ok: bool
    status: str  # "done" | "indeterminate" | "in_flight" | "budget_exhausted" | "failed" | "conflict"
    data: Any = None
    text: str | None = None
    model: str = ""
    provider: str | None = None
    billing_type: str = "platform"
    usage: dict[str, Any] = field(default_factory=dict)
    cash_usd: float = 0.0
    notional_usd: float = 0.0
    replayed: bool = False
    cached: bool = False
    invocation_id: str = ""
    attempt: int = 1
    error_kind: str | None = None
    error: str | None = None
    stream_started: bool = False
    chunks_received: int = 0

    @property
    def cost_usd(self) -> float:
        return self.cash_usd

    @property
    def value(self) -> Any:
        return self.data if self.data is not None else self.text


def resolve_model_billing_and_provider(model_key: str) -> tuple[str, str]:
    """Return `(billing_type, provider)` for `model_key` (`'oauth'` vs `'platform'`)."""
    key = (model_key or "").strip()
    if not key:
        return "platform", "unknown"

    if key.endswith("-oauth"):
        return "oauth", _resolve_provider_for_model(key, "oauth")

    try:
        meta = load_dalton_meta() or {}
        m_meta = meta.get(key)
        if isinstance(m_meta, dict) and m_meta.get("billing") == "subscription":
            return "oauth", _resolve_provider_for_model(key, "oauth")
    except Exception:
        pass

    return "platform", _resolve_provider_for_model(key, "platform")


def model_requires_user_oauth(model_key: str) -> bool:
    """Return True only when ``model_key`` requires a user OAuth or setup token.

    Coding-plan subscription models (e.g. ``glm-5.3-flash-plan``) have
    ``billing="subscription"`` / ``billing_type="oauth"`` for notional pricing,
    yet run on API keys (deployment env key or BYOK) rather than user OAuth
    tokens, so they must NOT be gated as requiring a ``credential_user_id``.
    """
    key = (model_key or "").strip()
    if not key:
        return False
    from src.llms.claude_code import is_claude_code_model
    from src.llms.llm import LLM as LLMFactory
    from src.server.services.llm.clients import subscription_route

    if is_claude_code_model(key):
        return True
    mc = LLMFactory.get_model_config()
    return subscription_route(key, mc, custom_entry=None) is not None


def compute_invocation_id_and_hash(
    *,
    scope_ref: str,
    purpose: str,
    model_key: str,
    system_prompt: str | None,
    user_prompt: str,
    response_schema: Type[BaseModel] | dict[str, Any] | None,
    reasoning_effort: str | None,
    slot: str,
) -> tuple[str, str]:
    """Compute `(invocation_id, request_hash)` per P0N.7 §2."""
    if isinstance(response_schema, type) and issubclass(response_schema, BaseModel):
        schema_name: str | None = response_schema.__name__
        schema_json: dict[str, Any] | None = response_schema.model_json_schema()
    elif isinstance(response_schema, dict):
        schema_name = "json_schema"
        schema_json = response_schema
    else:
        schema_name = None
        schema_json = None

    canonical = json.dumps(
        {
            "v": 1,
            "purpose": purpose,
            "scope_ref": scope_ref,
            "model": model_key,
            "system": system_prompt or "",
            "user": user_prompt,
            "schema": schema_name,
            "schema_json": schema_json,
            "effort": reasoning_effort,
            "slot": slot,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    inv_digest = hashlib.sha256((f"{scope_ref}\n{canonical}").encode("utf-8")).hexdigest()[:40]
    return f"lj1:{inv_digest}", request_hash


def compute_call_costs(
    model_key: str,
    billing_type: str,
    token_usage: dict[str, Any] | None,
) -> tuple[Decimal, Decimal]:
    """Compute `(cash_usd, notional_usd)` for a single LLM invocation (or its per-attempt calls)."""
    from src.dalton.llm.notional_pricing import (
        build_notional_price_index,
        calculate_notional_cost,
        is_subscription_model,
        resolve_notional_card,
    )

    usage = token_usage if isinstance(token_usage, dict) else {}
    raw_calls = usage.get("per_call_usages")
    call_usages: list[dict[str, int]] = []
    if isinstance(raw_calls, list) and raw_calls:
        for item in raw_calls:
            if isinstance(item, dict):
                call_usages.append(
                    {
                        "input_tokens": int(item.get("input_tokens") or 0),
                        "output_tokens": int(item.get("output_tokens") or 0),
                        "cached_tokens": int(item.get("cached_tokens") or 0),
                        "cache_5m_tokens": int(item.get("cache_5m_tokens") or 0),
                        "cache_1h_tokens": int(item.get("cache_1h_tokens") or 0),
                    }
                )
    if not call_usages:
        call_usages = [
            {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cached_tokens": int(usage.get("cached_tokens") or 0),
                "cache_5m_tokens": int(usage.get("cache_5m_tokens") or 0),
                "cache_1h_tokens": int(usage.get("cache_1h_tokens") or 0),
            }
        ]

    calc_cost = Decimal("0")
    calc_notional: Decimal | None = None
    try:
        from src.utils.tracking.core import calculate_cost_from_per_call_records

        res = calculate_cost_from_per_call_records(
            [
                {
                    "model_name": model_key,
                    "usage": dict(cu),
                    "billing_type": billing_type,
                }
                for cu in call_usages
            ]
        )
        calc_cost = Decimal(str(res.get("total_cost") or "0")).quantize(Decimal("0.000001"))
        per_call = res.get("per_call_costs") or []
        if per_call and all(
            isinstance(c, dict) and c.get("notional_cost") is not None for c in per_call
        ):
            calc_notional = sum(
                (Decimal(str(c["notional_cost"])) for c in per_call),
                Decimal("0"),
            ).quantize(Decimal("0.000001"))
    except Exception as exc:
        logger.debug("[DaltonComplete] calculate_cost_from_per_call_records failed: %s", exc)

    is_sub = (
        billing_type == "oauth"
        or _is_subscription_model(model_key, billing_type)
        or is_subscription_model(model_key, billing_type)
    )
    if is_sub:
        idx = build_notional_price_index(overlay_loader=load_overlay_data)
        card = resolve_notional_card(model_key, index=idx)
        if card is not None:
            total_notional = Decimal("0")
            for cu in call_usages:
                usd, _ = calculate_notional_cost(
                    card,
                    input_tokens=cu["input_tokens"],
                    output_tokens=cu["output_tokens"],
                    cached_tokens=cu["cached_tokens"],
                    cache_5m_tokens=cu["cache_5m_tokens"],
                    cache_1h_tokens=cu["cache_1h_tokens"],
                )
                total_notional += Decimal(str(usd))
            return Decimal("0"), total_notional.quantize(Decimal("0.000001"))
        if calc_notional is not None and calc_notional > Decimal("0"):
            return Decimal("0"), calc_notional
        if calc_cost > Decimal("0"):
            return Decimal("0"), calc_cost
        return Decimal("0"), Decimal("0")

    return calc_cost, Decimal("0")


_NUMERIC_USAGE_KEYS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "cache_5m_tokens",
    "cache_1h_tokens",
    "reasoning_tokens",
    "audio_input_tokens",
    "audio_output_tokens",
)


def _extract_single_call_dict(usage: dict[str, Any]) -> dict[str, int | float]:
    return {
        k: v
        for k, v in usage.items()
        if k in _NUMERIC_USAGE_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool)
    }


def _build_usage_from_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
    if not calls:
        return {}
    totals: dict[str, Any] = {}
    per_call: list[dict[str, Any]] = []
    for item in calls:
        if not isinstance(item, dict):
            continue
        clean = {
            k: v
            for k, v in item.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        if not clean:
            continue
        per_call.append(clean)
        for k, v in clean.items():
            totals[k] = totals.get(k, 0) + v
    if not per_call:
        return {}
    totals["per_call_usages"] = per_call
    return totals


def _extract_usage_from_exception(
    exc: BaseException,
    collected_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    """Extract provider-reported usage from *this* exception itself or the active ``make_api_call``
    collector. Never walks ``__cause__`` or ``__context__`` so prior request usage cannot be
    double-counted, and never sums both sources (picks whichever carries more completed calls)."""
    exc_usage: dict[str, Any] = {}
    for attr in ("token_usage", "usage"):
        raw = getattr(exc, attr, None)
        if isinstance(raw, dict) and has_reported_token_usage(raw):
            exc_usage = dict(raw)
            break

    collected_usage = _build_usage_from_calls(collected_calls)
    if not exc_usage:
        return collected_usage
    if not collected_usage:
        return exc_usage

    exc_calls = exc_usage.get("per_call_usages")
    exc_count = len(exc_calls) if isinstance(exc_calls, list) and exc_calls else 1
    col_count = len(collected_usage.get("per_call_usages") or [])
    if col_count > exc_count:
        merged = dict(collected_usage)
        for meta_key in ("retry_attempts", "parsing_failed"):
            if meta_key in exc_usage and meta_key not in merged:
                merged[meta_key] = exc_usage[meta_key]
        return merged
    return exc_usage


def _build_attempt_call_records(
    *,
    collected_usages: Any,
    effective_model: str,
    billing_type: str,
    fallback_usage: dict[str, Any] | None,
    fallback_cash_usd: Decimal,
    fallback_notional_usd: Decimal,
    fallback_outcome: str,
    fallback_error_kind: str | None,
    fallback_unknown_calls: int,
    fallback_occurred_at: datetime | None,
    credential_holder: str | None = None,
) -> tuple[list[dict[str, Any]], Decimal, Decimal]:
    """Convert per-request records captured by `make_api_call` into per-request ledger rows
    with each request's own `occurred_at` (send timestamp) and per-request cost."""
    raw_records = getattr(collected_usages, "call_records", None)
    if not isinstance(raw_records, list) or not raw_records:
        return [], fallback_cash_usd, fallback_notional_usd

    out: list[dict[str, Any]] = []
    total_cash = Decimal("0")
    total_notional = Decimal("0")
    total_count = len(raw_records)

    for idx, rec in enumerate(raw_records, start=1):
        if not isinstance(rec, dict):
            continue
        rec_usage = dict(rec.get("usage") or {})
        rec_has_usage = has_reported_token_usage(rec_usage)
        rec_outcome_raw = str(rec.get("outcome") or "")
        rec_unknown = max(0, int(rec.get("unknown_usage_calls") or 0))
        if rec_outcome_raw == "in_flight" and rec_unknown == 0 and not rec_has_usage:
            rec_unknown = 1

        if rec_has_usage:
            c_i, n_i = compute_call_costs(effective_model, billing_type, rec_usage)
        else:
            c_i, n_i = Decimal("0"), Decimal("0")
        total_cash += c_i
        total_notional += n_i

        if credential_holder:
            rec_usage.setdefault("credential_user_id", credential_holder)
        rec_usage["usage_known"] = rec_unknown == 0
        rec_usage["unknown_usage_calls"] = rec_unknown

        if rec_outcome_raw == "unbilled_error" and not rec_has_usage and rec_unknown == 0:
            row_outcome = "failed"
            row_error_kind = fallback_error_kind or "pre_send"
        elif rec_unknown > 0:
            row_outcome = "indeterminate"
            row_error_kind = fallback_error_kind or "unknown"
        elif fallback_outcome == "done":
            row_outcome = "done"
            row_error_kind = None
        elif rec_has_usage:
            row_outcome = "indeterminate"
            row_error_kind = (
                "post_call_error"
                if fallback_error_kind == "post_call_error"
                else ("parse_error" if idx < total_count or fallback_error_kind != "cancelled" else "cancelled")
            )
        else:
            row_outcome = fallback_outcome if fallback_outcome in ("done", "failed", "indeterminate") else "indeterminate"
            row_error_kind = fallback_error_kind

        out.append(
            {
                "call_seq": idx,
                "outcome": row_outcome,
                "error_kind": row_error_kind,
                "usage": rec_usage,
                "cash_usd": c_i,
                "notional_usd": n_i,
                "usage_known": rec_unknown == 0,
                "unknown_usage_calls": rec_unknown,
                "occurred_at": rec.get("occurred_at") or fallback_occurred_at,
            }
        )

    # Defensive fallback if a custom LLM wrapper returned usage without going through
    # `extract_token_usage` inside `_track_upstream_call`.
    if (
        out
        and total_cash == Decimal("0")
        and total_notional == Decimal("0")
        and (fallback_cash_usd > Decimal("0") or fallback_notional_usd > Decimal("0"))
    ):
        first_occ = out[0].get("occurred_at") or fallback_occurred_at
        return (
            [
                {
                    "call_seq": 1,
                    "outcome": fallback_outcome,
                    "error_kind": fallback_error_kind,
                    "usage": dict(fallback_usage) if isinstance(fallback_usage, dict) else fallback_usage,
                    "cash_usd": fallback_cash_usd,
                    "notional_usd": fallback_notional_usd,
                    "usage_known": fallback_unknown_calls == 0,
                    "unknown_usage_calls": max(0, int(fallback_unknown_calls or 0)),
                    "occurred_at": first_occ,
                }
            ],
            fallback_cash_usd,
            fallback_notional_usd,
        )

    return out, total_cash, total_notional


def _merge_attempt_usage(
    prior: Any,
    current: dict[str, Any],
    *,
    preserve_credential_holder: bool = True,
) -> dict[str, Any]:
    """Combine usage from an earlier journal attempt with the current attempt's usage."""
    if not isinstance(prior, dict) or not has_reported_token_usage(prior):
        return dict(current)

    prior_calls_raw = prior.get("per_call_usages")
    if isinstance(prior_calls_raw, list) and prior_calls_raw:
        prior_calls = [dict(c) for c in prior_calls_raw if isinstance(c, dict)]
    else:
        single_prior = _extract_single_call_dict(prior)
        prior_calls = [single_prior] if single_prior else []

    curr_calls_raw = current.get("per_call_usages")
    if isinstance(curr_calls_raw, list) and curr_calls_raw:
        curr_calls = [dict(c) for c in curr_calls_raw if isinstance(c, dict)]
    elif has_reported_token_usage(current):
        single_curr = _extract_single_call_dict(current)
        curr_calls = [single_curr] if single_curr else []
    else:
        curr_calls = []

    merged: dict[str, Any] = dict(current)
    for key in _NUMERIC_USAGE_KEYS:
        pv = prior.get(key)
        cv = current.get(key)
        p_num = pv if isinstance(pv, (int, float)) and not isinstance(pv, bool) else 0
        c_num = cv if isinstance(cv, (int, float)) and not isinstance(cv, bool) else 0
        if p_num or c_num:
            merged[key] = p_num + c_num
    combined_calls = prior_calls + curr_calls
    if combined_calls:
        merged["per_call_usages"] = combined_calls
    if preserve_credential_holder and "credential_user_id" not in merged and prior.get("credential_user_id"):
        merged["credential_user_id"] = prior["credential_user_id"]
    elif not preserve_credential_holder:
        merged.pop("credential_user_id", None)
    return merged


class DeadlineBeforeSendError(Exception):
    """The caller's deadline passed before the request went upstream: nothing was sent."""


# The ledger note for a resend is best-effort: never wait on it for long.
_RESEND_NOTE_TIMEOUT_S = 2.0
# Bounded wait when flushing already-collected billing on external task cancellation.
_CANCEL_SETTLE_TIMEOUT_S = 5.0


def _classify_exception(
    exc: BaseException,
    *,
    provider: str | None = None,
    sdk: str | None = None,
) -> tuple[str, str]:
    """Classify an LLM invocation error into `(target_state, error_kind)` per P0N.7 §3.

    Returns:
        - `('failed', 'pre_send')` when request was never sent
        - `('failed', 'auth_expired')` when `muse-oauth` returns 401 / `invalid_api_key`
        - `('failed', 'http_4xx')` on explicit 400/401/403/404 rejection without charge
        - `('indeterminate', ...)` on timeout, 429, 5xx, stream cut, or unknown mid-flight error
    """
    if isinstance(exc, DeadlineBeforeSendError):
        return "failed", "deadline"
    if isinstance(exc, ( asyncio.TimeoutError, TimeoutError)):
        return "indeterminate", "timeout"

    from src.llms.api_call import StructuredOutputParseError

    if isinstance(exc, (StructuredOutputParseError, json.JSONDecodeError, ValidationError)):
        return "indeterminate", "parse_error"

    from src.dalton.llm.providers.xai_grok.client import (
        classify_xai_error,
        has_stream_started,
    )

    status_code = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    xai_kind = classify_xai_error(
        exc,
        provider=provider,
        sdk=sdk,
        require_xai_origin=True,
    )
    if xai_kind == "billing" and not has_stream_started(exc):
        return "failed", "billing"
    if xai_kind == "rate_limit" and status_code not in (400, 401, 403, 404, 422):
        return "indeterminate", "rate_limit"

    cls_name = exc.__class__.__name__
    msg = str(exc)
    msg_lower = msg.lower()
    eff_provider = provider or getattr(exc, "provider", None)

    status_code = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )

    if eff_provider == "muse-oauth":
        from src.dalton.llm.providers.muse.client import is_muse_auth_expired_error

        if is_muse_auth_expired_error(exc):
            return "failed", "auth_expired"

    if cls_name in (
        "MissingProviderKeyError",
        "ProviderDisabledError",
        "ValueError",
        "TypeError",
        "KeyError",
    ) or any(
        token in msg_lower
        for token in (
            "missingproviderkey",
            "providerdisabled",
            "已在 dalton 配置中关闭",
            "no model configured",
            "not logged in",
            "no valid oauth token",
            "oauth_required",
            "requires a connected",
            "unsupported sdk",
        )
    ):
        return "failed", "pre_send"

    if isinstance(status_code, int):
        if status_code in (400, 401, 403, 404, 422, 426):
            # 426 Upgrade Required: the request was refused before any work (e.g. an
            # outdated client version), so it is a clean failure, not an unknown outcome.
            return "failed", f"http_{status_code}"
        if status_code == 429:
            return "indeterminate", "http_429"
        if status_code >= 500:
            return "indeterminate", f"http_{status_code}"

    for code in ("400", "401", "403", "404"):
        if f"http {code}" in msg_lower or f"status {code}" in msg_lower or f"status_code={code}" in msg_lower or f"error code: {code}" in msg_lower:
            return "failed", "http_4xx"

    if "timeout" in msg_lower or "timed out" in msg_lower:
        return "indeterminate", "timeout"

    return "indeterminate", "stream_cut"


def _unwrap_content_blocks(val: Any) -> str:
    """Convert a string or LangChain list of content blocks (`[{'type': 'text', 'text': '...'}]`) into plain text."""
    if isinstance(val, list):
        parts: list[str] = []
        for item in val:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        if parts:
            return "".join(parts)
    if isinstance(val, str):
        stripped = val.strip()
        if stripped.startswith("[{") and stripped.endswith("}]") and "'text'" in stripped:
            try:
                import ast

                parsed_blocks = ast.literal_eval(stripped)
                if isinstance(parsed_blocks, list):
                    return _unwrap_content_blocks(parsed_blocks)
            except Exception:
                pass
        return val
    return str(val if val is not None else "")


_REDACTED_CARD_METADATA_KEYS: tuple[str, ...] = (
    "type",
    "topic_label",
    "claim",
    "tickers",
    "entities",
    "sectors",
    "figures",
    "quote_event_id",
    "cited_sources",
    "confidence",
)
_REDACTED_FIGURE_KEYS: tuple[str, ...] = ("metric", "value", "unit", "period")
_RAW_TEXT_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "quote",
        "quotes",
        "text",
        "raw_text",
        "event_text",
        "content",
        "events",
        "messages",
        "transcript",
        "body",
        "raw",
    }
)


def _strip_raw_fields(val: Any) -> Any:
    """Recursively strip verbatim quote and raw event text fields from structured payloads."""
    if isinstance(val, dict):
        return {
            str(k): _strip_raw_fields(v)
            for k, v in val.items()
            if str(k).strip().lower() not in _RAW_TEXT_FIELD_NAMES
        }
    if isinstance(val, list):
        return [_strip_raw_fields(item) for item in val]
    return val


def _redact_card_for_journal(card: dict[str, Any]) -> dict[str, Any]:
    """Keep only structured card metadata (never `quote` or raw event text) for `dalton_llm_journal`."""
    redacted: dict[str, Any] = {}
    for k in _REDACTED_CARD_METADATA_KEYS:
        if k not in card:
            continue
        if k == "figures":
            figs: list[dict[str, Any]] = []
            for f in card.get("figures") or []:
                if isinstance(f, dict):
                    # Scalars only: a nested object could carry quoted source text.
                    figs.append(
                        {
                            fk: f[fk]
                            for fk in _REDACTED_FIGURE_KEYS
                            if fk in f and (f[fk] is None or isinstance(f[fk], (str, int, float, bool)))
                        }
                    )
            redacted["figures"] = figs
        else:
            redacted[k] = _strip_raw_fields(card[k])
    return redacted


def _redact_response_for_journal(db_resp: dict[str, Any]) -> dict[str, Any] | None:
    """Build a privacy-safe journal payload when `persist_raw=False`.

    Omits raw free-form text responses, strips `quote` and any raw event text fields from
    structured extraction cards (retaining only structured metadata), and marks the journal
    payload `replayable=False` because downstream quote verification requires raw text.
    """
    if not isinstance(db_resp, dict):
        return None
    kind = db_resp.get("kind")
    data = db_resp.get("data")
    if kind == "text" and isinstance(db_resp.get("text"), str):
        raw_txt = db_resp["text"].strip()
        if raw_txt.startswith("```"):
            lines = raw_txt.splitlines()
            if len(lines) >= 2 and lines[-1].strip() == "```":
                raw_txt = "\n".join(lines[1:-1]).strip()
        if raw_txt.startswith("{") and raw_txt.endswith("}"):
            try:
                parsed_obj = json.loads(raw_txt)
                if isinstance(parsed_obj, dict) and ("cards" in parsed_obj or "category" in parsed_obj):
                    kind = "json"
                    data = parsed_obj
            except Exception:
                return None
    if kind in ("json", "structured") and isinstance(data, dict):
        cat = str(data.get("category") or "").strip().lower()
        if cat in ("ops", "private"):
            return {
                "kind": kind,
                "replayable": False,
                "data": {"category": cat, "topic_label": "", "cards": []},
            }
        if "cards" in data or "category" in data:
            raw_cards = data.get("cards") if isinstance(data.get("cards"), list) else []
            redacted_cards = [
                _redact_card_for_journal(c) for c in raw_cards if isinstance(c, dict)
            ]
            return {
                "kind": kind,
                "replayable": False,
                "data": {
                    "category": cat or "research",
                    "topic_label": str(data.get("topic_label") or "").strip(),
                    "cards": redacted_cards,
                },
            }
        return {
            "kind": kind,
            "replayable": False,
            "data": _strip_raw_fields(data),
        }
    return None


def _serialize_response_payload(raw_out: Any) -> tuple[dict[str, Any], Any, str | None]:
    """Convert `LLMService.complete` output into `(json_dict_for_db, parsed_data, text)`."""
    if isinstance(raw_out, BaseModel):
        dumped = raw_out.model_dump(mode="json")
        return {"kind": "structured", "data": dumped}, raw_out, json.dumps(dumped, ensure_ascii=False)
    if isinstance(raw_out, dict):
        return {"kind": "json", "data": raw_out}, raw_out, json.dumps(raw_out, ensure_ascii=False)
    text_val = _unwrap_content_blocks(raw_out)
    return {"kind": "text", "text": text_val}, None, text_val


def _deserialize_cached_response(
    stored: Any,
    response_schema: Type[BaseModel] | dict[str, Any] | None,
) -> tuple[Any, str | None]:
    """Restore `(data, text)` from `dalton_llm_journal.response`."""
    if not isinstance(stored, dict):
        text_val = _unwrap_content_blocks(stored)
        return None, text_val

    if isinstance(response_schema, type) and issubclass(response_schema, BaseModel):
        payload = stored.get("data") if "data" in stored else stored
        if isinstance(payload, dict):
            try:
                model_inst = response_schema.model_validate(payload)
                return model_inst, json.dumps(payload, ensure_ascii=False)
            except Exception:
                return payload, json.dumps(payload, ensure_ascii=False)

    if "data" in stored and stored["data"] is not None:
        data_val = stored["data"]
        return data_val, json.dumps(data_val, ensure_ascii=False) if isinstance(data_val, dict) else str(data_val)

    if "text" in stored:
        text_val = _unwrap_content_blocks(stored.get("text"))
        return None, text_val

    return stored, json.dumps(stored, ensure_ascii=False)


async def _check_cash_budget_admission(
    user_id: str,
    *,
    thread_id: str | None = None,
    workspace_id: str | None = None,
) -> tuple[bool, str | None, str | None]:
    """Verify daily cash budget (and optional scoped task cap) before sending a cash LLM call."""
    settings = get_dalton_settings().budget
    floor_usd = Decimal(str(settings.admission_floor_usd or 0.25))

    async with get_db_connection() as conn:
        async with conn.transaction():
            summary = await reconcile_and_summarize_day(conn, user_id)
            decs = summary["_decimals"]
            cap_usd = decs["cap_usd"]
            spent_usd = decs["spent_usd"]
            available = cap_usd - spent_usd
            configured = bool(summary.get("configured", True))

            effective_floor = min(floor_usd, cap_usd) if cap_usd > Decimal("0") else floor_usd
            if not configured or available <= Decimal("0") or available < effective_floor:
                settled_total = decs["settled_usd"] + decs["unreserved_usage_usd"]
                open_total = decs["open_reserved_usd"] + decs["expired_conservative_usd"]
                msg = format_daily_budget_exhausted_message(
                    cap_usd, settled_total, open_total, configured=configured
                )
                return False, "budget_exhausted", msg

            if thread_id or workspace_id:
                cfg = await resolve_user_budget_config(conn, user_id)
                task_cap = await resolve_effective_task_cap(
                    conn,
                    user_id,
                    thread_id,
                    workspace_id,
                    cfg.get("default_task_cap_usd"),
                )
                if task_cap is not None:
                    task_spent = await fetch_scoped_task_spend(
                        conn,
                        user_id,
                        thread_id=thread_id,
                        workspace_id=workspace_id,
                        summary_rows=summary.get("_raw_rows"),
                    )
                    if task_cap <= Decimal("0") or task_spent >= task_cap:
                        msg = format_task_cap_exhausted_message(task_cap, task_spent)
                        return False, "task_cap_exhausted", msg

    return True, None, None


def _finish(result: DaltonCompleteResult, *, raise_on_error: bool) -> DaltonCompleteResult:
    if not raise_on_error or result.ok:
        return result
    msg = result.error or result.status
    if result.status == "indeterminate":
        raise JournalIndeterminate(msg, error_kind=result.error_kind)
    if result.status == "in_flight":
        raise JournalInFlight(msg, error_kind=result.error_kind)
    if result.status == "budget_exhausted":
        raise BudgetExhaustedError(msg, error_kind=result.error_kind)
    if result.status == "conflict":
        raise JournalConflict(msg, error_kind=result.error_kind)
    raise JournalError(
        msg,
        error_kind=result.error_kind,
        status_code=401 if result.error_kind == "auth_expired" else None,
    )


_UNSET = object()


async def dalton_complete(
    llm_service: Any = None,
    *,
    user_id: str,
    credential_user_id: Any = _UNSET,
    ignore_custom_models: bool = False,
    record_provider_events: bool = True,
    user_prompt: str = "",
    system_prompt: str | None = None,
    scope_ref: str | None = None,
    purpose: str = "general",
    request_model: str | None = None,
    model: str | None = None,
    response_schema: Type[BaseModel] | dict[str, Any] | None = None,
    slot: Literal["background", "primary"] = "background",
    reasoning_effort: str | None = None,
    is_byok: bool = True,
    timeout_s: float = 300.0,
    stale_after_s: int = 900,
    resend_indeterminate: bool = False,
    resend_max_attempts: int | None = None,
    invocation_id: str | None = None,
    workspace_id: str | None = None,
    thread_id: str | None = None,
    raise_on_error: bool = False,
    disable_tracing: bool = True,
    persist_raw: bool = True,
    persist_response: bool = True,
    skip_journal: bool = False,
    deadline: float | None = None,
) -> DaltonCompleteResult:
    """Execute a server-side one-shot LLM call with budget gating and Postgres journal idempotency (G1.3, P0N.7).

    ``deadline`` (``time.monotonic``) bounds only a new upstream send: the journal is
    consulted first, so a finished call still replays after the deadline; the time
    left is re-read just before sending (after the journal, claim and config waits)
    and caps the model timeout.

    ``credential_user_id`` separates credential resolution (passed to
    ``LLMService.complete``; ``None`` enforces deployment env/platform credentials
    only) from ``user_id`` (used for budget admission and journal accounting).
    ``record_provider_events=False`` suppresses writing raw error text to
    ``dalton_provider_events``, ``dalton_llm_journal.error_text``, and parse
    retry debug logs. ``persist_response=False`` writes usage, cost, and state
    to ``dalton_llm_journal`` while keeping ``response`` NULL.
    """
    from src.llms.api_call import redact_parse_error_logs

    with (
        redact_parse_error_logs()
        if not record_provider_events
        else nullcontext()
    ):
        return await _dalton_complete_impl(
            llm_service=llm_service,
            user_id=user_id,
            credential_user_id=credential_user_id,
            ignore_custom_models=ignore_custom_models,
            record_provider_events=record_provider_events,
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            scope_ref=scope_ref,
            purpose=purpose,
            request_model=request_model,
            model=model,
            response_schema=response_schema,
            slot=slot,
            reasoning_effort=reasoning_effort,
            is_byok=is_byok,
            timeout_s=timeout_s,
            stale_after_s=stale_after_s,
            resend_indeterminate=resend_indeterminate,
            resend_max_attempts=resend_max_attempts,
            invocation_id=invocation_id,
            workspace_id=workspace_id,
            thread_id=thread_id,
            raise_on_error=raise_on_error,
            disable_tracing=disable_tracing,
            persist_raw=persist_raw,
            persist_response=persist_response,
            skip_journal=skip_journal,
            deadline=deadline,
        )


async def _dalton_complete_impl(
    llm_service: Any = None,
    *,
    user_id: str,
    credential_user_id: Any = _UNSET,
    ignore_custom_models: bool = False,
    record_provider_events: bool = True,
    user_prompt: str = "",
    system_prompt: str | None = None,
    scope_ref: str | None = None,
    purpose: str = "general",
    request_model: str | None = None,
    model: str | None = None,
    response_schema: Type[BaseModel] | dict[str, Any] | None = None,
    slot: Literal["background", "primary"] = "background",
    reasoning_effort: str | None = None,
    is_byok: bool = True,
    timeout_s: float = 300.0,
    stale_after_s: int = 900,
    resend_indeterminate: bool = False,
    resend_max_attempts: int | None = None,
    invocation_id: str | None = None,
    workspace_id: str | None = None,
    thread_id: str | None = None,
    raise_on_error: bool = False,
    disable_tracing: bool = True,
    persist_raw: bool = True,
    persist_response: bool = True,
    skip_journal: bool = False,
    deadline: float | None = None,
) -> DaltonCompleteResult:
    effective_model = (model or request_model or "").strip()
    if not effective_model:
        try:
            from src.dalton.llm.role_policy import _load_default_agent_config

            llm_cfg = _load_default_agent_config().get("llm") or {}
            if slot == "background":
                effective_model = str(llm_cfg.get("background") or "").strip()
            else:
                effective_model = str(
                    llm_cfg.get("name") or llm_cfg.get("preferred_model") or ""
                ).strip()
        except Exception:
            pass
        if not effective_model and slot == "background":
            try:
                from src.dalton.settings import get_dalton_settings

                effective_model = str(
                    get_dalton_settings().models.assist_model or ""
                ).strip()
            except Exception:
                pass
    if not effective_model:
        raise ValueError(
            f"默认模型未配置（slot={slot}，请在 agent_config.dalton.yaml / dalton.yaml 中配置或显式传入 model）"
        )

    billing_type, provider = resolve_model_billing_and_provider(effective_model)
    if scope_ref:
        eff_scope = scope_ref
    else:
        eff_scope = (
            f"{purpose}:"
            + hashlib.sha256(f"{system_prompt or ''}\n{user_prompt}".encode("utf-8")).hexdigest()[:24]
        )
        if workspace_id and str(workspace_id) not in eff_scope:
            eff_scope = f"{eff_scope}@ws:{workspace_id}"
        if thread_id and str(thread_id) not in eff_scope:
            eff_scope = f"{eff_scope}@th:{thread_id}"
    computed_inv_id, request_hash = compute_invocation_id_and_hash(
        scope_ref=eff_scope,
        purpose=purpose,
        model_key=effective_model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_schema=response_schema,
        reasoning_effort=reasoning_effort,
        slot=slot,
    )
    inv_id = invocation_id or computed_inv_id
    if not user_id or not str(user_id).strip():
        raise ValueError("user_id is required")
    eff_user_id = str(user_id).strip()
    if credential_user_id is _UNSET:
        llm_user_id: str | None = user_id
    elif credential_user_id is None:
        llm_user_id = None
    else:
        llm_user_id = str(credential_user_id).strip() or None

    # 1. Inspect journal first so cached replays (`done`) and indeterminate/in-flight states
    #    return immediately without charging or re-sending.
    action, existing_row = (
        ("proceed", None)
        if skip_journal
        else await inspect_journal_state(
            inv_id,
            request_hash=request_hash,
            stale_after_s=stale_after_s,
            resend_indeterminate=resend_indeterminate,
            resend_max_attempts=resend_max_attempts,
        )
    )

    if action == "conflict":
        return _finish(
            DaltonCompleteResult(
                ok=False,
                status="conflict",
                model=effective_model,
                provider=provider,
                billing_type=billing_type,
                invocation_id=inv_id,
                error_kind="conflict",
                error=f"Journal invocation_id {inv_id} collided with mismatched request_hash",
            ),
            raise_on_error=raise_on_error,
        )

    if (
        action == "done"
        and existing_row is not None
        and is_journal_response_replayable(existing_row.get("response"))
    ):
        try:
            await record_journal_replay(inv_id)
        except Exception as exc:
            logger.debug("[DaltonComplete] record_journal_replay failed for %s: %s", inv_id, exc)
        restored_data, restored_text = _deserialize_cached_response(
            existing_row.get("response"), response_schema
        )
        return DaltonCompleteResult(
            ok=True,
            status="done",
            data=restored_data,
            text=restored_text,
            model=str(existing_row.get("model_key") or effective_model),
            provider=existing_row.get("provider") or provider,
            billing_type=str(existing_row.get("billing_type") or billing_type),
            usage=existing_row.get("usage") if isinstance(existing_row.get("usage"), dict) else {},
            cash_usd=0.0,
            notional_usd=float(Decimal(str(existing_row.get("notional_usd") or "0"))),
            replayed=True,
            cached=True,
            invocation_id=inv_id,
            attempt=int(existing_row.get("attempt") or 1),
        )

    if action == "in_flight":
        return _finish(
            DaltonCompleteResult(
                ok=False,
                status="in_flight",
                model=effective_model,
                provider=provider,
                billing_type=billing_type,
                invocation_id=inv_id,
                attempt=int((existing_row or {}).get("attempt") or 1),
                error_kind="in_flight",
                error="该请求正在由另一个进程执行中，请稍候。",
            ),
            raise_on_error=raise_on_error,
        )

    # A5: a deadline already past means nothing new can be sent. The journal
    # above still replayed a finished answer; here, with nothing cached, return
    # WITHOUT claiming or recording a failure — the 87-batch audit incident had
    # every past-deadline call claim its journal row and write
    # DeadlineBeforeSendError into it, painting three healthy models as broken.
    if deadline is not None and time.monotonic() >= deadline:
        return _finish(
            DaltonCompleteResult(
                ok=False,
                status="failed",
                model=effective_model,
                provider=provider,
                billing_type=billing_type,
                invocation_id=inv_id,
                attempt=1,
                error_kind="deadline",
                error="截止时间已到，未发送请求（journal 中无缓存结果）",
            ),
            raise_on_error=raise_on_error,
        )

    if action == "indeterminate":
        return _finish(
            DaltonCompleteResult(
                ok=False,
                status="indeterminate",
                model=effective_model,
                provider=provider,
                billing_type=billing_type,
                invocation_id=inv_id,
                attempt=int((existing_row or {}).get("attempt") or 1),
                error_kind=str((existing_row or {}).get("error_kind") or "indeterminate"),
                error=(
                    "上次调用在返回前中断，供应商可能已经计费。"
                    "确认后可重新发起（会再计一次费用）。"
                ),
            ),
            raise_on_error=raise_on_error,
        )

    # 2. Budget admission check for cash models (`billing_type != 'oauth'`)
    if billing_type != "oauth" and not skip_journal:
        try:
            admitted, err_kind, err_msg = await _check_cash_budget_admission(
                eff_user_id,
                thread_id=thread_id,
                workspace_id=workspace_id,
            )
        except Exception as exc:
            logger.error("[DaltonComplete] budget check failed for %s: %s", inv_id, exc)
            return _finish(
                DaltonCompleteResult(
                    ok=False,
                    status="budget_exhausted",
                    model=effective_model,
                    provider=provider,
                    billing_type=billing_type,
                    invocation_id=inv_id,
                    error_kind="budget_db_error",
                    error=f"预算检查不可用：{sanitize_error_text(exc)}",
                ),
                raise_on_error=raise_on_error,
            )

        if not admitted:
            return _finish(
                DaltonCompleteResult(
                    ok=False,
                    status="budget_exhausted",
                    model=effective_model,
                    provider=provider,
                    billing_type=billing_type,
                    invocation_id=inv_id,
                    error_kind=err_kind or "budget_exhausted",
                    error=err_msg or "今日现金额度已用完",
                ),
                raise_on_error=raise_on_error,
            )

    # 3. Atomically claim invocation in `dalton_llm_journal`
    claim_action, claimed_row = (
        ("proceed", {"attempt": 1})
        if skip_journal
        else await claim_journal_invocation(
            invocation_id=inv_id,
            user_id=eff_user_id,
            scope_ref=eff_scope,
            purpose=purpose,
            model_key=effective_model,
            provider=provider,
            billing_type=billing_type,
            request_hash=request_hash,
            stale_after_s=stale_after_s,
            resend_indeterminate=resend_indeterminate,
            resend_max_attempts=resend_max_attempts,
            **({"stamp_credential_holder": False} if llm_user_id is None else {}),
        )
    )
    attempt = int(claimed_row.get("attempt") or 1)

    if claim_action == "done" and is_journal_response_replayable(claimed_row.get("response")):
        try:
            await record_journal_replay(inv_id)
        except Exception as exc:
            logger.debug("[DaltonComplete] record_journal_replay failed for %s: %s", inv_id, exc)
        restored_data, restored_text = _deserialize_cached_response(
            claimed_row.get("response"), response_schema
        )
        return DaltonCompleteResult(
            ok=True,
            status="done",
            data=restored_data,
            text=restored_text,
            model=str(claimed_row.get("model_key") or effective_model),
            provider=claimed_row.get("provider") or provider,
            billing_type=str(claimed_row.get("billing_type") or billing_type),
            usage=claimed_row.get("usage") if isinstance(claimed_row.get("usage"), dict) else {},
            cash_usd=0.0,
            notional_usd=float(Decimal(str(claimed_row.get("notional_usd") or "0"))),
            replayed=True,
            cached=True,
            invocation_id=inv_id,
            attempt=attempt,
        )

    if claim_action in ("in_flight", "indeterminate", "conflict"):
        return _finish(
            DaltonCompleteResult(
                ok=False,
                status=claim_action,
                model=effective_model,
                provider=provider,
                billing_type=billing_type,
                invocation_id=inv_id,
                attempt=attempt,
                error_kind=claim_action,
                error=f"Journal state is {claim_action}",
            ),
            raise_on_error=raise_on_error,
        )

    # 4. Invoke underlying LLMService.complete
    # R2-N1 #6: Separate pre-call/in-call errors from post-call errors so an exception
    # after the upstream LLM already returned is ALWAYS recorded as `indeterminate`
    # (never `failed`), preventing double billing on retry.
    llm_call_completed = False
    # R2a-2 / R2a fix1-4 (2026-10-28 language review lock-ups): this worker owns
    # the claim, so it may really repeat a call whose previous attempt ended
    # uncertain ('indeterminate', or a stale 'pending' read as a crash) and may
    # already have been billed. The ledger note is written only once the request
    # has actually gone out (see the `finally` below): never before the send, so
    # writing it cannot eat into the send's deadline, and a call that dies before
    # sending leaves no note. A worker that lost the race (`in_flight`) or
    # replayed a cached result returned earlier and records nothing.
    is_resend = (
        resend_indeterminate
        and claim_action == "claimed"
        and existing_row is not None
        and str(existing_row.get("state") or "") in ("indeterminate", "pending")
    )
    resend_sent = False
    llm_call_entered = False
    attempt_started_at: datetime | None = None
    cash_dec: Decimal | None = None
    notional_dec: Decimal | None = None
    usage_dict: dict[str, Any] = {}
    collected_usages: Any = []
    try:
        from src.llms.api_call import _utc_now, collect_api_call_usage

        if not llm_user_id and model_requires_user_oauth(effective_model):
            raise ValueError("OAuth model requires a credential user")

        if llm_service is None:
            from ptc_agent.config import load_from_files
            from src.server.services.llm.service import LLMService

            agent_cfg = await load_from_files()
            llm_service = LLMService(agent_config=agent_cfg, logger=logger)

        pydantic_schema: Type[BaseModel] | None = (
            response_schema
            if isinstance(response_schema, type) and issubclass(response_schema, BaseModel)
            else None
        )

        send_timeout_s = timeout_s
        if deadline is not None:
            send_timeout_s = min(timeout_s, deadline - time.monotonic())
            if send_timeout_s <= 0:
                raise DeadlineBeforeSendError("deadline reached before the request was sent")
        resend_sent = is_resend

        def _is_unbilled_provider_error(err: BaseException) -> bool:
            if isinstance(err, (asyncio.CancelledError, TimeoutError)):
                return False
            err_state, _err_kind = _classify_exception(err, provider=provider)
            return err_state == "failed"

        attempt_started_at = _utc_now()
        llm_call_entered = True
        with collect_api_call_usage(is_unbilled_error=_is_unbilled_provider_error) as collected_usages:
            raw_result = await asyncio.wait_for(
                llm_service.complete(
                    user_id=llm_user_id,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_schema=pydantic_schema,
                    slot=slot,
                    request_model=effective_model,
                    is_byok=is_byok,
                    reasoning_effort=reasoning_effort,
                    disable_tracing=disable_tracing,
                    return_token_usage=True,
                    **({"ignore_custom_models": True} if ignore_custom_models else {}),
                ),
                timeout=send_timeout_s,
            )
        llm_call_completed = True

        if isinstance(raw_result, tuple) and len(raw_result) == 2:
            out_obj, token_usage = raw_result
        else:
            out_obj, token_usage = raw_result, {}

        usage_dict = dict(token_usage) if isinstance(token_usage, dict) else {}
        if credential_user_id is _UNSET:
            if eff_user_id and is_team_credential_sharing_enabled():  # DALTON-HOOK(team-credentials): stamp credential holder into usage while billing/journal user_id stays caller
                usage_dict.setdefault("credential_user_id", _resolve_credential_user_id(eff_user_id))
        elif llm_user_id and llm_user_id != eff_user_id:
            usage_dict.setdefault("credential_user_id", llm_user_id)
        cash_dec, notional_dec = compute_call_costs(
            effective_model, billing_type, usage_dict
        )
        db_resp, parsed_data, text_val = _serialize_response_payload(out_obj)
        journal_resp = (
            None
            if not persist_response
            else (db_resp if persist_raw else _redact_response_for_journal(db_resp))
        )
        attempt_unknown_calls = int(getattr(collected_usages, "unknown_calls", 0) or 0) + int(
            getattr(collected_usages, "in_flight_calls", 0) or 0
        )
        cred_holder = (
            str(usage_dict["credential_user_id"])
            if usage_dict.get("credential_user_id")
            else None
        )
        done_call_records, per_call_cash, per_call_notional = _build_attempt_call_records(
            collected_usages=collected_usages,
            effective_model=effective_model,
            billing_type=billing_type,
            fallback_usage=usage_dict,
            fallback_cash_usd=cash_dec,
            fallback_notional_usd=notional_dec,
            fallback_outcome="done",
            fallback_error_kind=None,
            fallback_unknown_calls=attempt_unknown_calls,
            fallback_occurred_at=attempt_started_at,
            credential_holder=cred_holder,
        )
        if done_call_records:
            cash_dec = per_call_cash
            notional_dec = per_call_notional
        merged_prior = attempt > 1 and has_reported_token_usage(claimed_row.get("usage"))
        journal_usage = (
            _merge_attempt_usage(
                claimed_row.get("usage"),
                usage_dict,
                preserve_credential_holder=(llm_user_id is not None),
            )
            if merged_prior
            else usage_dict
        )

        if not skip_journal:
            extra_done_kwargs: dict[str, Any] = {}
            if llm_user_id is None:
                extra_done_kwargs["stamp_credential_holder"] = False
            if merged_prior:
                extra_done_kwargs["attempt_usage"] = usage_dict
            if attempt_unknown_calls > 0:
                extra_done_kwargs["unknown_usage_calls"] = attempt_unknown_calls
            if done_call_records:
                extra_done_kwargs["call_records"] = done_call_records
                extra_done_kwargs["occurred_at"] = done_call_records[0].get("occurred_at") or attempt_started_at
            await complete_journal_done(
                inv_id,
                attempt=attempt,
                response=journal_resp,
                usage=journal_usage,
                cash_usd=cash_dec,
                notional_usd=notional_dec,
                provider=provider,
                billing_type=billing_type,
                **extra_done_kwargs,
            )

        return DaltonCompleteResult(
            ok=True,
            status="done",
            data=parsed_data,
            text=text_val,
            model=effective_model,
            provider=provider,
            billing_type=billing_type,
            usage=usage_dict,
            cash_usd=float(cash_dec),
            notional_usd=float(notional_dec),
            replayed=False,
            cached=False,
            invocation_id=inv_id,
            attempt=attempt,
        )
    except asyncio.CancelledError as cancel_exc:
        if not skip_journal:
            try:
                if not llm_call_completed:
                    exc_usage = _extract_usage_from_exception(cancel_exc, collected_usages)
                    if has_reported_token_usage(exc_usage):
                        usage_dict = dict(exc_usage)
                        if credential_user_id is _UNSET:
                            if eff_user_id and is_team_credential_sharing_enabled():  # DALTON-HOOK(team-credentials): stamp credential holder into usage while billing/journal user_id stays caller
                                usage_dict.setdefault(
                                    "credential_user_id",
                                    _resolve_credential_user_id(eff_user_id),
                                )
                        elif llm_user_id and llm_user_id != eff_user_id:
                            usage_dict.setdefault("credential_user_id", llm_user_id)
                        cash_dec, notional_dec = compute_call_costs(
                            effective_model, billing_type, usage_dict
                        )

                has_usage = has_reported_token_usage(usage_dict)
                sent_calls = int(getattr(collected_usages, "sent_calls", 0) or 0)
                tracking_active = bool(getattr(collected_usages, "tracking_active", False))
                traced_unknown_calls = int(
                    getattr(collected_usages, "unknown_calls", 0) or 0
                ) + int(getattr(collected_usages, "in_flight_calls", 0) or 0)

                if llm_call_completed or tracking_active or sent_calls > 0:
                    attempt_unknown_calls = traced_unknown_calls
                elif not llm_call_entered:
                    attempt_unknown_calls = 0
                else:
                    attempt_unknown_calls = 0 if has_usage else 1

                if (
                    not llm_call_completed
                    and not has_usage
                    and attempt_unknown_calls == 0
                    and (not llm_call_entered or (tracking_active and sent_calls == 0))
                ):
                    cancel_state = "failed"
                else:
                    cancel_state = "indeterminate"
                cancel_error_kind = "cancelled"
                final_attempt_unknown = attempt_unknown_calls > 0

                attempt_cancel_usage: dict[str, Any] | None = None
                journal_cancel_usage: dict[str, Any] | None = None
                merged_prior_cancel = False
                if cancel_state == "indeterminate" or has_usage or final_attempt_unknown:
                    attempt_cancel_usage = dict(usage_dict)
                    attempt_cancel_usage["usage_known"] = not final_attempt_unknown
                    attempt_cancel_usage["unknown_usage_calls"] = attempt_unknown_calls
                    merged_prior_cancel = attempt > 1 and has_reported_token_usage(
                        claimed_row.get("usage")
                    )
                    journal_cancel_usage = (
                        _merge_attempt_usage(
                            claimed_row.get("usage"),
                            attempt_cancel_usage,
                            preserve_credential_holder=(llm_user_id is not None),
                        )
                        if merged_prior_cancel
                        else attempt_cancel_usage
                    )

                cred_holder = (
                    str(usage_dict["credential_user_id"])
                    if isinstance(usage_dict, dict) and usage_dict.get("credential_user_id")
                    else None
                )
                cancel_call_records, per_call_cash, per_call_notional = _build_attempt_call_records(
                    collected_usages=collected_usages,
                    effective_model=effective_model,
                    billing_type=billing_type,
                    fallback_usage=attempt_cancel_usage,
                    fallback_cash_usd=cash_dec or Decimal("0"),
                    fallback_notional_usd=notional_dec or Decimal("0"),
                    fallback_outcome=cancel_state,
                    fallback_error_kind=cancel_error_kind,
                    fallback_unknown_calls=attempt_unknown_calls,
                    fallback_occurred_at=attempt_started_at,
                    credential_holder=cred_holder,
                )
                if cancel_call_records:
                    cash_dec = per_call_cash
                    notional_dec = per_call_notional

                extra_cancel_kwargs: dict[str, Any] = {
                    "usage_known": not final_attempt_unknown,
                    "unknown_usage_calls": attempt_unknown_calls,
                }
                if llm_user_id is None:
                    extra_cancel_kwargs["stamp_credential_holder"] = False
                if merged_prior_cancel:
                    extra_cancel_kwargs["attempt_usage"] = attempt_cancel_usage
                if cancel_call_records:
                    extra_cancel_kwargs["call_records"] = cancel_call_records
                    extra_cancel_kwargs["occurred_at"] = (
                        cancel_call_records[0].get("occurred_at") or attempt_started_at
                    )
                elif attempt_started_at is not None:
                    extra_cancel_kwargs["occurred_at"] = attempt_started_at

                await asyncio.wait_for(
                    complete_journal_failed(
                        inv_id,
                        attempt=attempt,
                        error_kind=cancel_error_kind,
                        error_text="CancelledError",
                        state=cancel_state,
                        usage=journal_cancel_usage,
                        cash_usd=cash_dec,
                        notional_usd=notional_dec,
                        **extra_cancel_kwargs,
                    ),
                    timeout=_CANCEL_SETTLE_TIMEOUT_S,
                )
            except Exception as journal_exc:
                logger.error(
                    "[DaltonComplete] complete_journal_failed on cancellation failed for %s: %s",
                    inv_id,
                    journal_exc,
                )
        raise
    except Exception as exc:
        from src.dalton.llm.providers.xai_grok.client import get_stream_progress

        if llm_call_completed:
            target_state = "indeterminate"
            error_kind = "post_call_error"
            stream_started, chunks_received = True, 0
        else:
            target_state, error_kind = _classify_exception(exc, provider=provider)
            stream_started, chunks_received = get_stream_progress(exc)
            exc_usage = _extract_usage_from_exception(exc, collected_usages)
            if has_reported_token_usage(exc_usage):
                usage_dict = dict(exc_usage)
                if credential_user_id is _UNSET:
                    if eff_user_id and is_team_credential_sharing_enabled():  # DALTON-HOOK(team-credentials): stamp credential holder into usage while billing/journal user_id stays caller
                        usage_dict.setdefault(
                            "credential_user_id", _resolve_credential_user_id(eff_user_id)
                        )
                elif llm_user_id and llm_user_id != eff_user_id:
                    usage_dict.setdefault("credential_user_id", llm_user_id)
                cash_dec, notional_dec = compute_call_costs(
                    effective_model, billing_type, usage_dict
                )
                if target_state == "failed":
                    target_state = "indeterminate"
                    if error_kind == "pre_send":
                        error_kind = "parse_error"

        has_usage = has_reported_token_usage(usage_dict)
        sent_calls = int(getattr(collected_usages, "sent_calls", 0) or 0)
        tracking_active = bool(getattr(collected_usages, "tracking_active", False))
        traced_unknown_calls = int(getattr(collected_usages, "unknown_calls", 0) or 0) + int(
            getattr(collected_usages, "in_flight_calls", 0) or 0
        )
        if llm_call_completed or tracking_active or sent_calls > 0:
            # When `LLMService.complete` / `make_api_call` tracked upstream sends, only
            # actually-sent requests whose outcome was unknown (timeout/5xx/stream cut
            # while in flight) count as unknown usage — a timeout during `resolve_llm_config`
            # (tracking_active=True, sent_calls=0), during retry backoff sleep, or an
            # explicitly unbilled provider rejection (e.g. xAI 402 pre-stream) has
            # `traced_unknown_calls == 0`.
            attempt_unknown_calls = traced_unknown_calls
            if (
                not llm_call_completed
                and not has_usage
                and attempt_unknown_calls == 0
                and tracking_active
                and sent_calls == 0
            ):
                target_state = "failed"
        elif not llm_call_entered:
            attempt_unknown_calls = 0
            if not has_usage:
                target_state = "failed"
        else:
            attempt_unknown_calls = (
                1
                if target_state == "indeterminate"
                and (not has_usage or error_kind not in ("parse_error", "post_call_error"))
                else 0
            )
        final_attempt_unknown = attempt_unknown_calls > 0
        attempt_fail_usage: dict[str, Any] | None = None
        journal_fail_usage: dict[str, Any] | None = None
        merged_prior_fail = False
        if target_state == "indeterminate" or has_usage or final_attempt_unknown:
            attempt_fail_usage = dict(usage_dict)
            attempt_fail_usage["usage_known"] = not final_attempt_unknown
            attempt_fail_usage["unknown_usage_calls"] = attempt_unknown_calls
            attempt_fail_usage["stream_started"] = stream_started
            attempt_fail_usage["chunks_received"] = chunks_received
            merged_prior_fail = attempt > 1 and has_reported_token_usage(claimed_row.get("usage"))
            journal_fail_usage = (
                _merge_attempt_usage(
                    claimed_row.get("usage"),
                    attempt_fail_usage,
                    preserve_credential_holder=(llm_user_id is not None),
                )
                if merged_prior_fail
                else attempt_fail_usage
            )

        cred_holder = (
            str(usage_dict["credential_user_id"])
            if isinstance(usage_dict, dict) and usage_dict.get("credential_user_id")
            else None
        )
        fail_call_records, per_call_cash, per_call_notional = _build_attempt_call_records(
            collected_usages=collected_usages,
            effective_model=effective_model,
            billing_type=billing_type,
            fallback_usage=attempt_fail_usage,
            fallback_cash_usd=cash_dec or Decimal("0"),
            fallback_notional_usd=notional_dec or Decimal("0"),
            fallback_outcome=target_state,
            fallback_error_kind=error_kind,
            fallback_unknown_calls=attempt_unknown_calls,
            fallback_occurred_at=attempt_started_at,
            credential_holder=cred_holder,
        )
        if fail_call_records:
            cash_dec = per_call_cash
            notional_dec = per_call_notional

        safe_msg = sanitize_error_text(exc, max_len=240)
        if provider == "muse-oauth" and error_kind == "auth_expired":
            if "请重新连接 Muse" not in safe_msg:
                safe_msg = f"{safe_msg}（请重新连接 Muse）"
        err_summary = safe_msg if record_provider_events else type(exc).__name__
        if not skip_journal:
            try:
                extra_fail_kwargs: dict[str, Any] = {
                    "usage_known": not final_attempt_unknown,
                    "unknown_usage_calls": attempt_unknown_calls,
                }
                if llm_user_id is None:
                    extra_fail_kwargs["stamp_credential_holder"] = False
                if merged_prior_fail:
                    extra_fail_kwargs["attempt_usage"] = attempt_fail_usage
                if fail_call_records:
                    extra_fail_kwargs["call_records"] = fail_call_records
                    extra_fail_kwargs["occurred_at"] = (
                        fail_call_records[0].get("occurred_at") or attempt_started_at
                    )
                await complete_journal_failed(
                    inv_id,
                    attempt=attempt,
                    error_kind=error_kind,
                    error_text=err_summary,
                    state=target_state,
                    usage=journal_fail_usage,
                    cash_usd=cash_dec,
                    notional_usd=notional_dec,
                    **extra_fail_kwargs,
                )
            except Exception as journal_exc:
                if record_provider_events:
                    logger.error(
                        "[DaltonComplete] complete_journal_failed failed for %s (original %s: %s): %s",
                        inv_id,
                        error_kind,
                        safe_msg,
                        journal_exc,
                        exc_info=True,
                    )
                else:
                    logger.error(
                        "[DaltonComplete] complete_journal_failed failed for %s (original %s/%s): %s",
                        inv_id,
                        error_kind,
                        type(exc).__name__,
                        type(journal_exc).__name__,
                    )
            if provider and record_provider_events:
                await record_provider_event(
                    provider,
                    "journal_fail",
                    user_id=eff_user_id,
                    model_key=effective_model,
                    detail={
                        "purpose": purpose,
                        "error_kind": error_kind,
                        "state": target_state,
                        "message": safe_msg,
                    },
                    best_effort=True,
                )
        result_usage = {
            **usage_dict,
            "stream_started": stream_started,
            "chunks_received": chunks_received,
        }
        if target_state == "indeterminate" or final_attempt_unknown:
            result_usage["usage_known"] = not final_attempt_unknown
            result_usage["unknown_usage_calls"] = attempt_unknown_calls
        return _finish(
            DaltonCompleteResult(
                ok=False,
                status=target_state,
                model=effective_model,
                provider=provider,
                billing_type=billing_type,
                usage=result_usage,
                cash_usd=float(cash_dec or Decimal("0")),
                notional_usd=float(notional_dec or Decimal("0")),
                invocation_id=inv_id,
                attempt=attempt,
                error_kind=error_kind,
                error=err_summary,
                stream_started=stream_started,
                chunks_received=chunks_received,
            ),
            raise_on_error=raise_on_error,
        )
    finally:
        if resend_sent and record_provider_events:
            # Bounded: a note stuck on a database lock must not hold up the
            # call's own result, its exception, or a cancellation in progress.
            try:
                await asyncio.wait_for(
                    record_provider_event(
                        provider or "unknown",
                        "journal_resend_indeterminate",
                        user_id=eff_user_id,
                        model_key=effective_model,
                        detail={
                            "invocation_id": inv_id,
                            "purpose": purpose,
                            "previous_state": str(existing_row.get("state") or ""),
                            "previous_attempt": int(existing_row.get("attempt") or 1),
                            "note": "因上次状态不确定而重发",
                        },
                        best_effort=True,
                    ),
                    timeout=_RESEND_NOTE_TIMEOUT_S,
                )
            except TimeoutError:
                logger.warning("[DaltonComplete] resend note for %s timed out; skipped", inv_id)


journaled_complete = dalton_complete

