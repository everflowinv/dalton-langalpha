"""Budget-gated, idempotent server-side one-shot LLM wrapper (`dalton_complete` / `journaled_complete`, G1.3, P0N.7)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Type

from pydantic import BaseModel

from src.dalton.budget.port import (
    format_daily_budget_exhausted_message,
    format_task_cap_exhausted_message,
)
from src.dalton.budget.service import (
    _notional_price_index,
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
    inspect_journal_state,
    is_journal_response_replayable,
    record_journal_replay,
)
from src.dalton.credentials import (  # DALTON-HOOK(team-credentials): record credential_user_id in journal usage while user_id stays caller
    credential_user_id,
    is_team_credential_sharing_enabled,
)
from src.dalton.database.provider_events import record_provider_event, sanitize_error_text
from src.dalton.llm.overlay import load_dalton_meta, load_overlay_data
from src.dalton.settings import get_dalton_settings
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)


class JournalError(RuntimeError):
    """Base error for journaled one-shot LLM calls."""


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

    try:
        overlay = load_overlay_data() or {}
        models_map = overlay.get("models") or {}
        mcfg = models_map.get(key)
        if isinstance(mcfg, dict):
            if (mcfg.get("dalton") or {}).get("billing") == "subscription":
                return "oauth", "openclaw-broker"
            ref = str(mcfg.get("model_id") or "")
            prov_models = ((overlay.get("providers") or {}).get("models") or {}).get(
                "openclaw-broker", []
            )
            for pm in prov_models:
                if isinstance(pm, dict) and str(pm.get("id")) == ref:
                    if (pm.get("pricing") or {}).get("pricing_type") == "subscription" or (
                        pm.get("dalton") or {}
                    ).get("billing") == "subscription":
                        return "oauth", "openclaw-broker"
    except Exception:
        pass

    return "platform", _resolve_provider_for_model(key, "platform")


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
    """Compute `(cash_usd, notional_usd)` for a single LLM invocation."""
    usage = token_usage if isinstance(token_usage, dict) else {}
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    cached_tok = int(usage.get("cached_tokens") or 0)

    calc_cost = Decimal("0")
    try:
        from src.utils.tracking.core import calculate_cost_from_per_call_records

        res = calculate_cost_from_per_call_records(
            [
                {
                    "model_name": model_key,
                    "usage": {
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                        "cached_tokens": cached_tok,
                    },
                    "billing_type": billing_type,
                }
            ]
        )
        calc_cost = Decimal(str(res.get("total_cost") or "0")).quantize(Decimal("0.000001"))
    except Exception as exc:
        logger.debug("[DaltonComplete] calculate_cost_from_per_call_records failed: %s", exc)

    if billing_type == "oauth":
        if calc_cost > Decimal("0"):
            return Decimal("0"), calc_cost
        idx = _notional_price_index()
        card = idx.get(model_key)
        if card is not None:
            uncached_in = max(0, in_tok - cached_tok)
            usd = (
                uncached_in * card["input"]
                + cached_tok * card["cached_input"]
                + out_tok * card["output"]
            ) / 1_000_000.0
            return Decimal("0"), Decimal(f"{usd:.6f}")
        return Decimal("0"), Decimal("0")

    return calc_cost, Decimal("0")


def _classify_exception(exc: BaseException) -> tuple[str, str]:
    """Classify an LLM invocation error into `(target_state, error_kind)` per P0N.7 §3.

    Returns:
        - `('failed', 'pre_send')` when request was never sent
        - `('failed', 'http_4xx')` on explicit 400/401/403/404 rejection without charge
        - `('indeterminate', ...)` on timeout, 429, 5xx, stream cut, or unknown mid-flight error
    """
    if isinstance(exc, ( asyncio.TimeoutError, TimeoutError)):
        return "indeterminate", "timeout"

    cls_name = exc.__class__.__name__
    msg = str(exc)
    msg_lower = msg.lower()

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

    status_code = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if isinstance(status_code, int):
        if status_code in (400, 401, 403, 404, 422):
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
        raise JournalIndeterminate(msg)
    if result.status == "in_flight":
        raise JournalInFlight(msg)
    if result.status == "budget_exhausted":
        raise BudgetExhaustedError(msg)
    if result.status == "conflict":
        raise JournalConflict(msg)
    raise JournalError(msg)


async def dalton_complete(
    llm_service: Any = None,
    *,
    user_id: str,
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
    invocation_id: str | None = None,
    workspace_id: str | None = None,
    thread_id: str | None = None,
    raise_on_error: bool = False,
    disable_tracing: bool = True,
    persist_raw: bool = True,
    skip_journal: bool = False,
) -> DaltonCompleteResult:
    """Execute a server-side one-shot LLM call with budget gating and Postgres journal idempotency (G1.3, P0N.7)."""
    effective_model = (model or request_model or "").strip()
    if not effective_model:
        effective_model = "deepseek-flash" if slot == "background" else "claude-opus-5-5-oauth"

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
    cash_dec: Decimal | None = None
    notional_dec: Decimal | None = None
    try:
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

        raw_result = await asyncio.wait_for(
            llm_service.complete(
                user_id=user_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_schema=pydantic_schema,
                slot=slot,
                request_model=effective_model,
                is_byok=is_byok,
                reasoning_effort=reasoning_effort,
                disable_tracing=disable_tracing,
                return_token_usage=True,
            ),
            timeout=timeout_s,
        )
        llm_call_completed = True

        if isinstance(raw_result, tuple) and len(raw_result) == 2:
            out_obj, token_usage = raw_result
        else:
            out_obj, token_usage = raw_result, {}

        usage_dict = dict(token_usage) if isinstance(token_usage, dict) else {}
        if eff_user_id and is_team_credential_sharing_enabled():  # DALTON-HOOK(team-credentials): stamp credential holder into usage while billing/journal user_id stays caller
            usage_dict.setdefault("credential_user_id", credential_user_id(eff_user_id))
        cash_dec, notional_dec = compute_call_costs(
            effective_model, billing_type, usage_dict
        )
        db_resp, parsed_data, text_val = _serialize_response_payload(out_obj)
        journal_resp = db_resp if persist_raw else _redact_response_for_journal(db_resp)

        if not skip_journal:
            await complete_journal_done(
                inv_id,
                response=journal_resp,
                usage=usage_dict,
                cash_usd=cash_dec,
                notional_usd=notional_dec,
                provider=provider,
                billing_type=billing_type,
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
    except Exception as exc:
        if llm_call_completed:
            target_state = "indeterminate"
            error_kind = "post_call_error"
        else:
            target_state, error_kind = _classify_exception(exc)
        safe_msg = sanitize_error_text(exc, max_len=240)
        if not skip_journal:
            try:
                await complete_journal_failed(
                    inv_id,
                    error_kind=error_kind,
                    error_text=safe_msg,
                    state=target_state,
                    cash_usd=cash_dec,
                    notional_usd=notional_dec,
                )
            except Exception as journal_exc:
                logger.error(
                    "[DaltonComplete] complete_journal_failed failed for %s (original %s: %s): %s",
                    inv_id,
                    error_kind,
                    safe_msg,
                    journal_exc,
                    exc_info=True,
                )
            if provider:
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
        return _finish(
            DaltonCompleteResult(
                ok=False,
                status=target_state,
                model=effective_model,
                provider=provider,
                billing_type=billing_type,
                invocation_id=inv_id,
                attempt=attempt,
                error_kind=error_kind,
                error=safe_msg,
            ),
            raise_on_error=raise_on_error,
        )


journaled_complete = dalton_complete

