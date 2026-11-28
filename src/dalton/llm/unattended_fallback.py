"""Unattended-turn subscription quota fallback, live admission, and wakeup checkpoint deferral.

Implements Design §2 (prod-audit-2.md) for ``$0`` OAuth subscription pool fallback:
- Trusted server-side unattended source classification (never trusting client-supplied fields).
- Structured OAuth subscription availability & Redis quota cooldown probing (distinguishing
  terminal quota cooldown vs plain 429 rate-limit backoff and auth disconnects).
- Profile-aware candidate chain resolution strictly confined to ``$0`` OAuth subscription models.
- Live admission re-verification before switching models (modality/catalog isolation +
  original snapshot RouteFingerprint check, latest org policy via ``policy_refuses``, strict
  payer/subject unity, and live subscription availability).
- Cross-worker idempotent wakeup deferral & mid-turn checkpoint-resume ledger in
  ``dalton_wakeup_quota_deferrals`` (shared 3-defer / 24h cap keyed by ``root_job_id``).
- Optional call-site hook into Item 3 (``dalton_failure_notices``) that cleanly no-ops
  when Item 3 is absent or disabled.
"""

from __future__ import annotations

import copy
import logging
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import HTTPException
from psycopg.rows import dict_row

from src.dalton.profile import DeploymentProfile, resolve_profile
from src.dalton.services.team_model_catalog.snapshot import (
    RouteFingerprint,
    bind_verified_resume_variant_binding,
    get_active_catalog_snapshot,
    load_resume_source_snapshot,
    resolve_live_route_fingerprint,
    verify_candidate_route_against_snapshot,
)
from src.dalton.settings import is_unattended_quota_fallback_enabled
from src.llms.provider_errors import read_refusal
from src.server.contracts.turn_kinds import (
    UNWATCHED_RUN_META,
    classify_origin,
    posted_by_server,
    run_unwatched,
)
from src.server.database.pool import get_db_connection
from src.server.utils.pg_sanitize import SafeJson, normalize_uuid

logger = logging.getLogger(__name__)

_HKT = timezone(timedelta(hours=8))

# Switch-policy marker stamped on unattended fallback clients so ModelResilienceMiddleware
# only switches onto them for quota cooldown / terminal quota / auth failures (never transient 5xx).
SWITCH_POLICY_ATTR = "_dalton_switch_policy"
SWITCH_POLICY_QUOTA_OR_AUTH = "quota_or_auth_only"
UNATTENDED_FALLBACK_META_ATTR = "_dalton_unattended_fallback_meta"
DALTON_CANDIDATE_FAILURES_ATTR = "_dalton_candidate_quota_failures"

MAX_WAKEUP_QUOTA_DEFERS = 3
WAKEUP_QUOTA_DEADLINE_HOURS = 24
DEFAULT_UNKNOWN_RESET_DELAY_MINUTES = 30
WAKEUP_QUOTA_RESUME_HOOK = "wakeup_quota_resume"
DALTON_SUBSCRIPTION_COOLDOWN_TYPE = "dalton_subscription_cooldown"
DALTON_WAKEUP_QUOTA_EXHAUSTED_TYPE = "dalton_wakeup_quota_exhausted"

QUOTA_OR_AUTH_REASONS: frozenset[str] = frozenset(
    {
        "plan_limit",
        "quota_exceeded",
        "out_of_credit",
        "plan_excludes",
        "invalid_key",
    }
)
TERMINAL_QUOTA_REASONS: frozenset[str] = frozenset(
    {
        "plan_limit",
        "quota_exceeded",
        "out_of_credit",
        "plan_excludes",
    }
)


@dataclass(frozen=True)
class UnattendedTurnContext:
    """Server-verified execution context for an unattended turn."""

    is_unattended: bool
    subject_user_id: str
    source_kind: str
    payer_user_id: str | None = None
    ignore_custom_models: bool = False
    task_fallbacks: tuple[str, ...] = ()
    run_id: str | None = None
    root_job_id: str | None = None
    wakeup_id: str | None = None
    thread_id: str | None = None
    role: str = "analyst"
    require_vision: bool = False


@dataclass(frozen=True)
class SubscriptionAvailability:
    """Structured availability status for a subscription model."""

    available: bool
    kind: Literal["auth_unavailable", "quota_cooldown"] | None = None
    refusal_code: str | None = None
    provider: str | None = None
    reset_at: datetime | None = None
    cooldown_key: str | None = None
    message: str | None = None
    model_name: str = ""


@dataclass(frozen=True)
class FallbackAdmissionResult:
    """Outcome of live admission checks before activating a ``$0`` subscription fallback candidate."""

    admitted: bool
    model_name: str
    reason: str | None = None
    billing: str = "oauth"
    route: RouteFingerprint | None = None


@dataclass(frozen=True)
class WakeupDeferDecision:
    """Outcome of evaluating and recording a wakeup quota deferral / checkpoint resume."""

    action: Literal["defer", "exhausted_failed", "already_settled"]
    root_job_id: str
    defer_count: int
    effective_reset_at: datetime | None = None
    held_known: bool = True
    scheduled_wake_at: datetime | None = None
    deadline_at: datetime | None = None
    reason: str = "subscription_quota_exhausted"
    idempotent_replay: bool = False
    resume_checkpoint_id: str | None = None


_ACTIVE_UNATTENDED_CONTEXT: ContextVar[UnattendedTurnContext | None] = ContextVar(
    "dalton_active_unattended_context", default=None
)


def get_active_unattended_turn_context() -> UnattendedTurnContext | None:
    """Return the active server-stamped unattended turn context, if any."""
    return _ACTIVE_UNATTENDED_CONTEXT.get()


@contextmanager
def bind_unattended_turn_context(
    ctx: UnattendedTurnContext | None,
) -> Iterator[UnattendedTurnContext | None]:
    """Bind ``ctx`` across client resolution / fallback building for this turn."""
    if ctx is None:
        yield None
        return
    token = _ACTIVE_UNATTENDED_CONTEXT.set(ctx)
    try:
        yield ctx
    finally:
        _ACTIVE_UNATTENDED_CONTEXT.reset(token)


# ---------------------------------------------------------------------------
# 1. Trusted server-side unattended source classification
# ---------------------------------------------------------------------------


def is_unattended_turn(
    request: Any = None,
    *,
    run_metadata: Mapping[str, Any] | None = None,
    run_row: Mapping[str, Any] | None = None,
    query_type: str | None = None,
    platform: str | None = None,
    from_service: bool | None = None,
    internal_endpoint: bool = False,
    assist_call: bool = False,
) -> bool:
    """Return True strictly for server-trusted unattended turns.

    Only server-stamped fields are consulted (``query_type`` is stripped from
    non-service requests at ingress in ``messaging.py``):
    - Internal host LLM endpoint (``/api/v1/dalton/internal/llm/complete``) and ``assist_json``.
    - Automation turns (``classify_origin == "automation"`` or ``automation_execution_id``
      or server-set ``origin.type == "automation"``).
    - Server-posted notification/wakeup turns (``posted_by_server(query_type, platform)``
      where ``platform is None`` and ``query_type in {"system", "market_target", "wakeup"}``).
    - Restart-resumed unwatched turns (``run_unwatched(run_row) is True`` or
      ``run_metadata["unwatched"] is True``).
    - Chief-of-Staff tool-dispatched analyst turns (``report_back_ptc_thread_id`` /
      ``cos_handoff`` or server-posted agent steer with ``steer_job_id`` and no platform).
    """
    if internal_endpoint or assist_call:
        return True

    meta: dict[str, Any] = {}
    if isinstance(run_row, Mapping) and isinstance(run_row.get("metadata"), Mapping):
        meta.update(run_row["metadata"])
    if isinstance(run_metadata, Mapping):
        meta.update(run_metadata)

    qt = query_type if query_type is not None else getattr(request, "query_type", None)
    plat = platform if platform is not None else getattr(request, "platform", None)

    # Interactive client requests (explicit from_service=False) cannot spoof unattended status
    # via request fields; only durable server metadata on run_row / run_metadata counts.
    if meta.get("automation_execution_id") or meta.get("automation_id"):
        return True
    if classify_origin(meta, qt if from_service is not False else None) == "automation":
        return True

    if run_row is not None and run_unwatched(run_row) is True:
        return True
    if meta.get(UNWATCHED_RUN_META) is True:
        return True

    if meta.get("report_back_ptc_thread_id") or meta.get("cos_handoff"):
        return True

    if from_service is False:
        return False

    if posted_by_server(qt, plat):
        return True

    origin = getattr(request, "origin", None)
    origin_type = getattr(origin, "type", None)
    if origin_type == "automation":
        return True
    if (
        origin_type == "agent"
        and not plat
        and getattr(request, "steer_job_id", None)
    ):
        return True

    return False


def classify_unattended_source_kind(
    request: Any = None,
    *,
    run_metadata: Mapping[str, Any] | None = None,
    run_row: Mapping[str, Any] | None = None,
    query_type: str | None = None,
    internal_endpoint: bool = False,
    assist_call: bool = False,
) -> str:
    """Return a concise server-side unattended source kind label."""
    if internal_endpoint:
        return "data_task"
    if assist_call:
        return "assist"
    meta: dict[str, Any] = {}
    if isinstance(run_row, Mapping) and isinstance(run_row.get("metadata"), Mapping):
        meta.update(run_row["metadata"])
    if isinstance(run_metadata, Mapping):
        meta.update(run_metadata)
    qt = query_type if query_type is not None else getattr(request, "query_type", None)
    if meta.get("automation_execution_id") or meta.get("automation_id"):
        return "automation"
    if qt == "wakeup":
        return "wakeup"
    if qt == "market_target":
        return "market_target"
    if qt == "system" or meta.get("report_back_ptc_thread_id") or meta.get("cos_handoff"):
        return "report"
    if run_row is not None and run_unwatched(run_row) is True:
        return "unwatched"
    return "automation"


# ---------------------------------------------------------------------------
# 2. Structured cooldown & subscription availability probing
# ---------------------------------------------------------------------------


def _parse_iso_or_epoch(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def resolve_oauth_credential_id_from_row(conn_row: Mapping[str, Any] | None) -> str:
    """Compute the exact non-secret credential identity used by ``model_resilience.quota_redis_key``."""
    if not isinstance(conn_row, Mapping):
        return ""
    from src.dalton.llm.oauth_refresh import credential_identity

    account_id = str(conn_row.get("account_id") or "").strip()
    cred_id = credential_identity(
        account_id=account_id,
        email=conn_row.get("email"),
        updated_at=conn_row.get("updated_at"),
    )
    if not cred_id and account_id and not account_id.startswith("muse-api-key"):
        cred_id = account_id
    return cred_id


def build_quota_cooldown_key(
    *,
    user_id: str,
    model_name: str,
    provider: str,
    is_custom: bool = False,
    credential_id: str = "",
    account_id: str = "",
) -> str | None:
    """Build the Redis cooldown coordination key shared by pre-dispatch probe and runtime benching."""
    if not user_id or not provider or not model_name:
        return None
    from src.dalton.credentials import credential_user_id
    from src.dalton.llm.antigravity_series import quota_cooldown_target
    from src.ptc_agent.agent.middleware.model_resilience import _QUOTA_EXHAUSTED_REDIS_PREFIX

    if str(user_id) == "platform":
        cred_user = "platform"
    else:
        try:
            cred_user = credential_user_id(str(user_id))
        except Exception:
            cred_user = str(user_id)
    if not cred_user:
        return None
    eff_cred_id = (credential_id or "").strip()
    if not eff_cred_id:
        acct = (account_id or "").strip()
        if acct and not acct.startswith("muse-api-key"):
            eff_cred_id = acct
    scope_kind, target = quota_cooldown_target(provider, model_name, is_custom=is_custom)
    base = f"{_QUOTA_EXHAUSTED_REDIS_PREFIX}:{cred_user}:{provider}:{scope_kind}:{target}"
    return f"{base}:{eff_cred_id}" if eff_cred_id else base


def _compute_cooldown_redis_key_without_client(
    *,
    user_id: str,
    model_name: str,
    provider: str,
    model_id: str = "",
    is_custom: bool = False,
    credential_id: str = "",
    account_id: str = "",
) -> str | None:
    # Always key by manifest/catalog ``model_name`` (matching ``model_resilience.quota_redis_key``
    # and ``quota_cooldown_target``), falling back to ``model_id`` only if ``model_name`` is blank.
    eff_name = (model_name or model_id or "").strip()
    return build_quota_cooldown_key(
        user_id=user_id,
        model_name=eff_name,
        provider=provider,
        is_custom=is_custom,
        credential_id=credential_id,
        account_id=account_id,
    )


def _resolve_model_subscription_info(
    model_name: str,
    *,
    prefs: Mapping[str, Any] | None = None,
    ignore_custom_models: bool = False,
) -> tuple[str | None, str, str, bool]:
    """Return ``(subscription_provider_id, catalog_provider, model_id, is_custom)``."""
    try:
        from src.llms.llm import LLM as LLMFactory
        from src.server.services.llm import user_models
        from src.server.services.llm.clients import subscription_route

        mc = LLMFactory.get_model_config()
        custom_entry = None
        if not ignore_custom_models and isinstance(prefs, Mapping):
            custom_entry = user_models.custom_model(dict(prefs), model_name)
        route = subscription_route(model_name, mc, custom_entry=custom_entry)
        entry = custom_entry if isinstance(custom_entry, Mapping) else mc.get_model_config(model_name)
        prov = str((entry or {}).get("provider") or "").strip() if isinstance(entry, Mapping) else ""
        mid = str((entry or {}).get("model_id") or model_name).strip() if isinstance(entry, Mapping) else model_name
        sub_id = route.subscription.id if route is not None else None
        return sub_id, prov or (sub_id or ""), mid, custom_entry is not None
    except Exception:
        return None, "", model_name, False


async def inspect_subscription_availability(
    user_id: str,
    model_name: str,
    *,
    client: Any = None,
    prefs: Mapping[str, Any] | None = None,
    ignore_custom_models: bool = False,
    now: datetime | None = None,
) -> SubscriptionAvailability:
    """Probe OAuth authorization health and Redis quota cooldown for ``(user_id, model_name)``."""
    now_dt = now or datetime.now(timezone.utc)
    now_ts = now_dt.timestamp()
    mk = str(model_name or "").strip()
    if not mk:
        return SubscriptionAvailability(available=True, model_name="")

    sub_provider, catalog_provider, model_id, is_custom = _resolve_model_subscription_info(
        mk, prefs=prefs, ignore_custom_models=ignore_custom_models
    )
    provider = sub_provider or catalog_provider or None

    # 1. Check OAuth subscription auth status using real oauth_tokens / oauth_health APIs
    #    and extract the exact credential_id used by model_resilience.quota_redis_key.
    resolved_credential_id = ""
    resolved_account_id = ""
    if sub_provider and user_id:
        try:
            from src.server.database.oauth_tokens import get_oauth_connection
            from src.server.services.oauth_health import connection_state

            conn_row = await get_oauth_connection(str(user_id), sub_provider)
            state = connection_state(conn_row)
            if state == "needs_reauth" and isinstance(conn_row, Mapping):
                code = str(conn_row.get("last_error_code") or "oauth_reauth_required")
                return SubscriptionAvailability(
                    available=False,
                    kind="auth_unavailable",
                    refusal_code=code,
                    provider=provider,
                    reset_at=None,
                    cooldown_key=None,
                    message=format_unattended_quota_notice(
                        "oauth_auth_unavailable", model_name=mk
                    ),
                    model_name=mk,
                )
            if state == "disconnected":
                return SubscriptionAvailability(
                    available=False,
                    kind="auth_unavailable",
                    refusal_code="oauth_required",
                    provider=provider,
                    reset_at=None,
                    cooldown_key=None,
                    message=format_unattended_quota_notice(
                        "oauth_auth_unavailable", model_name=mk
                    ),
                    model_name=mk,
                )
            if isinstance(conn_row, Mapping):
                resolved_credential_id = resolve_oauth_credential_id_from_row(conn_row)
                resolved_account_id = str(conn_row.get("account_id") or "").strip()
        except Exception:
            logger.debug(
                "[UNATTENDED_FALLBACK] OAuth health probe skipped for model=%s",
                mk,
                exc_info=True,
            )

    # 2. Compute Redis cooldown coordination key (sharing exact credential_id & model_name rules)
    cooldown_key: str | None = None
    if client is not None:
        try:
            from src.ptc_agent.agent.middleware.model_resilience import quota_redis_key

            cooldown_key = quota_redis_key(mk, client, user_id=user_id)
        except Exception:
            cooldown_key = None
    if cooldown_key is None and provider and user_id:
        cooldown_key = _compute_cooldown_redis_key_without_client(
            user_id=user_id,
            model_name=mk,
            provider=provider,
            model_id=model_id,
            is_custom=is_custom,
            credential_id=resolved_credential_id,
            account_id=resolved_account_id,
        )

    if not cooldown_key:
        return SubscriptionAvailability(
            available=True,
            provider=provider,
            cooldown_key=None,
            model_name=mk,
        )

    # 3. Read structured cooldown record from Redis
    try:
        from src.utils.cache.redis_cache import get_cache_client

        data = await get_cache_client().get(cooldown_key)
    except Exception:
        logger.debug(
            "[UNATTENDED_FALLBACK] Redis cooldown probe unavailable for key=%s; failing open",
            cooldown_key,
        )
        return SubscriptionAvailability(
            available=True,
            provider=provider,
            cooldown_key=cooldown_key,
            model_name=mk,
        )

    if not isinstance(data, Mapping):
        return SubscriptionAvailability(
            available=True,
            provider=provider,
            cooldown_key=cooldown_key,
            model_name=mk,
        )

    expires_at_raw = data.get("expires_at")
    if isinstance(expires_at_raw, (int, float)) and float(expires_at_raw) <= now_ts:
        return SubscriptionAvailability(
            available=True,
            provider=provider,
            cooldown_key=cooldown_key,
            model_name=mk,
        )

    refusal_raw = data.get("refusal")
    refusal_code: str | None = None
    reset_at: datetime | None = _parse_iso_or_epoch(expires_at_raw)
    if isinstance(refusal_raw, Mapping):
        raw_reason = refusal_raw.get("reason")
        if isinstance(raw_reason, str) and raw_reason.strip():
            refusal_code = raw_reason.strip()
        if "until" in refusal_raw:
            reset_at = _parse_iso_or_epoch(refusal_raw.get("until"))
        elif refusal_code == "plan_limit":
            # plan_limit with no explicit until means unknown reset time
            reset_at = None

    # Plain 429 rate-limit backoff (e.g. long Retry-After) is NOT terminal subscription exhaustion.
    if refusal_code is not None and refusal_code not in QUOTA_OR_AUTH_REASONS:
        return SubscriptionAvailability(
            available=True,
            provider=provider,
            cooldown_key=cooldown_key,
            model_name=mk,
        )

    return SubscriptionAvailability(
        available=False,
        kind="quota_cooldown",
        refusal_code=refusal_code or "quota_exceeded",
        provider=provider,
        reset_at=reset_at,
        cooldown_key=cooldown_key,
        message=format_unattended_quota_notice(
            "subscription_quota_exhausted", model_name=mk, reset_at=reset_at
        ),
        model_name=mk,
    )


# ---------------------------------------------------------------------------
# 3. Candidate chain resolution (profile-aware, $0 subscription default)
# ---------------------------------------------------------------------------


def _classify_candidate_billing(
    model_name: str,
    *,
    prefs: Mapping[str, Any] | None = None,
    ignore_custom_models: bool = False,
) -> tuple[str, str]:
    """Return ``(billing_kind, provider)`` where ``billing_kind`` is ``'oauth'``, ``'cash'``, or ``'platform'``."""
    try:
        from src.dalton.llm.complete import resolve_model_billing_and_provider

        billing, prov = resolve_model_billing_and_provider(model_name)
        b_norm = str(billing or "cash").strip().lower()
        if b_norm in ("oauth", "subscription"):
            return "oauth", prov
        if b_norm == "platform":
            return "platform", prov
        return "cash", prov
    except Exception:
        sub_id, prov, _, _ = _resolve_model_subscription_info(
            model_name, prefs=prefs, ignore_custom_models=ignore_custom_models
        )
        if sub_id:
            return "oauth", sub_id
        return "cash", prov


def get_profile_default_fallback_chain() -> list[str]:
    """Return default fallback models for the active deployment profile (``everflow`` vs ``core``)."""
    profile = resolve_profile()
    from src.server.services.llm.config import deployment_llm

    llm_cfg = deployment_llm()
    configured = [
        str(m).strip()
        for m in (llm_cfg.fallback if llm_cfg and llm_cfg.fallback else [])
        if isinstance(m, str) and m.strip()
    ]
    if profile is DeploymentProfile.CORE:
        return configured
    # Everflow profile uses deployment fallback chain (never injecting non-configured models)
    return configured


def resolve_unattended_fallback_chain(
    *,
    task_fallbacks: Sequence[str] | None = None,
    user_prefs: Mapping[str, Any] | None = None,
    primary_model: str | None = None,
    allowed_pool: Iterable[str] | None = None,
    skip_cooldown_keys: Iterable[str] | None = None,
    user_id: str | None = None,
    ignore_custom_models: bool = False,
) -> list[str]:
    """Resolve ordered ``$0`` OAuth subscription fallback candidates for an unattended turn.

    Order:
    1. Explicit ``task_fallbacks`` (when non-empty).
    2. Owner ``user_prefs["fallback_models"]`` (skipped ONLY when ``disable_fallback: true``).
    3. Active profile (``resolve_profile()``) default fallback chain (always available as the
       tail ``$0`` subscription pool when ``disable_fallback: true`` or personal candidates are
       filtered empty).

    Strictly filters out ``primary_model``, all non-OAuth (cash/platform) models, candidates
    outside ``allowed_pool``, and candidates sharing a cooldown key in ``skip_cooldown_keys``.
    """
    prefs = user_prefs if isinstance(user_prefs, Mapping) else {}
    disable_personal = prefs.get("disable_fallback") is True

    raw_chain: list[str] = []
    if task_fallbacks:
        raw_chain.extend(str(m).strip() for m in task_fallbacks if isinstance(m, str) and m.strip())

    if not disable_personal:
        saved = prefs.get("fallback_models")
        if isinstance(saved, list) and saved:
            raw_chain.extend(str(m).strip() for m in saved if isinstance(m, str) and m.strip())

    # Always append the active profile's default chain as the tail fallback layer so
    # ``disable_fallback: true`` only disables the personal list and a personal list
    # filtered empty (e.g. cash-only) still falls back to the $0 subscription pool.
    raw_chain.extend(get_profile_default_fallback_chain())

    pool_set = (
        {str(m).strip() for m in allowed_pool if str(m).strip()}
        if allowed_pool is not None
        else None
    )
    skip_keys = {str(k).strip() for k in (skip_cooldown_keys or ()) if str(k).strip()}

    primary_norm = str(primary_model or "").strip()
    seen_models: set[str] = set()
    seen_cooldown_keys: set[str] = set(skip_keys)
    result: list[str] = []

    for cand in raw_chain:
        if not cand or cand == primary_norm or cand in seen_models:
            continue
        if pool_set is not None and cand not in pool_set:
            continue

        billing, _ = _classify_candidate_billing(
            cand, prefs=prefs, ignore_custom_models=ignore_custom_models
        )
        if billing != "oauth":
            continue

        if user_id:
            sub_id, prov, mid, is_custom = _resolve_model_subscription_info(
                cand, prefs=prefs, ignore_custom_models=ignore_custom_models
            )
            ckey = _compute_cooldown_redis_key_without_client(
                user_id=user_id,
                model_name=cand,
                provider=sub_id or prov,
                model_id=mid,
                is_custom=is_custom,
            )
            if ckey and ckey in seen_cooldown_keys:
                continue
            if ckey:
                seen_cooldown_keys.add(ckey)

        seen_models.add(cand)
        result.append(cand)

    return result


# ---------------------------------------------------------------------------
# 4. Switch-trigger classification & terminal quota detection
# ---------------------------------------------------------------------------


def stamp_quota_or_auth_only(
    client: Any,
    *,
    candidate_name: str,
    subject_user_id: str,
    payer_user_id: str | None = None,
    run_id: str | None = None,
    ignore_custom_models: bool = False,
    role: str = "analyst",
    require_vision: bool = False,
    credential_source: str = "oauth",
    catalog_snapshot: Mapping[str, Any] | None = None,
) -> Any:
    """Mark a fallback LLM client so ModelResilienceMiddleware restricts switches to quota/auth."""
    if client is None:
        return client
    meta: dict[str, Any] = {
        "candidate_name": candidate_name,
        "subject_user_id": subject_user_id,
        "payer_user_id": payer_user_id or subject_user_id,
        "run_id": run_id,
        "ignore_custom_models": bool(ignore_custom_models),
        "role": role,
        "require_vision": bool(require_vision),
        "credential_source": str(credential_source or "oauth"),
    }
    if isinstance(catalog_snapshot, Mapping) and catalog_snapshot.get("models"):
        meta["catalog_snapshot"] = copy.deepcopy(dict(catalog_snapshot))
    try:
        object.__setattr__(client, SWITCH_POLICY_ATTR, SWITCH_POLICY_QUOTA_OR_AUTH)
        object.__setattr__(client, UNATTENDED_FALLBACK_META_ATTR, meta)
    except Exception:
        try:
            setattr(client, SWITCH_POLICY_ATTR, SWITCH_POLICY_QUOTA_OR_AUTH)
            setattr(client, UNATTENDED_FALLBACK_META_ATTR, meta)
        except Exception:
            pass
    return client


def bind_unattended_fallback_run_context(
    config: Any,
    *,
    run_id: str | None,
    payer_user_id: str | None = None,
) -> None:
    """Attach the canonical ``run_id`` / ``payer_user_id`` to stamped fallback clients on ``config``."""
    clients = getattr(config, "fallback_llm_clients", None)
    if not isinstance(clients, list):
        return
    for client in clients:
        meta = getattr(client, UNATTENDED_FALLBACK_META_ATTR, None)
        if not isinstance(meta, dict):
            continue
        if run_id and not meta.get("run_id"):
            meta["run_id"] = str(run_id)
        if payer_user_id and not meta.get("payer_user_id"):
            meta["payer_user_id"] = str(payer_user_id)


def is_quota_or_auth_only_client(client: Any) -> bool:
    """Return True when *client* carries ``switch_policy == 'quota_or_auth_only'``."""
    return getattr(client, SWITCH_POLICY_ATTR, None) == SWITCH_POLICY_QUOTA_OR_AUTH


def get_unattended_fallback_meta(client: Any) -> dict[str, Any] | None:
    meta = getattr(client, UNATTENDED_FALLBACK_META_ATTR, None)
    return dict(meta) if isinstance(meta, Mapping) else None


def is_quota_or_auth_switch_trigger(
    exc: BaseException | None,
    *,
    quota_benched: bool = False,
) -> bool:
    """Return True ONLY when *exc* is a confirmed terminal quota refusal or OAuth auth failure.

    Transient network errors, timeouts, generic 429 rate limits (even with long ``Retry-After``
    that benched the candidate locally), and 5xx server errors return ``False`` so
    ``quota_or_auth_only`` fallback candidates are never activated on non-quota faults.
    """
    _ = quota_benched  # Retained for call-site compatibility; plain rate_limited bench never triggers.
    if exc is None:
        return False

    from src.dalton.services.automation_subscription_fallback import (
        is_subscription_unavailable_error,
    )
    from src.ptc_agent.agent.middleware.model_resilience import _RemoteQuotaExhaustedError
    from src.server.services.oauth_health import OAuthReauthRequired, OAuthRefreshUnavailable
    from src.server.services.oauth_protocol import OAuthPlanNoApiAccess

    if isinstance(exc, _RemoteQuotaExhaustedError):
        refusal = read_refusal(exc)
        if refusal is not None:
            return refusal.reason in QUOTA_OR_AUTH_REASONS
        return True
    if isinstance(exc, (OAuthReauthRequired, OAuthRefreshUnavailable, OAuthPlanNoApiAccess)):
        return True
    if is_subscription_unavailable_error(exc):
        return True

    refusal = read_refusal(exc)
    if refusal is not None and refusal.reason in QUOTA_OR_AUTH_REASONS:
        return True

    # Explicit 401/402 HTTP status after OAuth refresh already failed
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in (401, 402):
        return True

    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, BaseException) and cause is not exc:
        if is_quota_or_auth_switch_trigger(cause):
            return True
    return False


def is_complete_result_quota_or_auth_trigger(result: Any) -> bool:
    """Return True ONLY when a ``DaltonCompleteResult`` carries structured terminal quota/auth metadata.

    Never matches arbitrary substrings like ``'429'``, ``'quota'``, ``'rate_limit'``,
    ``'oauth'``, or ``'billing'`` inside free-form error messages.
    """
    if result is None or getattr(result, "ok", False):
        return False
    if getattr(result, "quota_or_auth_trigger", False) is True:
        return True
    refusal_reason = getattr(result, "refusal_reason", None)
    if isinstance(refusal_reason, str) and refusal_reason.strip() in QUOTA_OR_AUTH_REASONS:
        return True
    if getattr(result, "error_kind", None) == "auth_expired":
        return True
    return False


def is_terminal_quota_refusal(exc: BaseException | None) -> bool:
    """Return True when *exc* is an exhausted terminal quota/auth refusal that must skip outer auto-retry."""
    if exc is None:
        return False

    from src.dalton.services.automation_subscription_fallback import (
        is_subscription_unavailable_error,
    )
    from src.ptc_agent.agent.middleware.model_resilience import _RemoteQuotaExhaustedError
    from src.server.services.oauth_health import OAuthReauthRequired, OAuthRefreshUnavailable
    from src.server.services.oauth_protocol import OAuthPlanNoApiAccess

    if isinstance(exc, _RemoteQuotaExhaustedError):
        refusal = read_refusal(exc)
        if refusal is not None:
            return refusal.reason in TERMINAL_QUOTA_REASONS
        return True
    if isinstance(exc, (OAuthReauthRequired, OAuthRefreshUnavailable, OAuthPlanNoApiAccess)):
        return True
    if is_subscription_unavailable_error(exc):
        return True

    refusal = read_refusal(exc)
    if refusal is not None and refusal.reason in TERMINAL_QUOTA_REASONS:
        return True

    # Unwrap common chained causes (e.g., ModelResilience exhaustion wrapper)
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, BaseException) and cause is not exc:
        if is_terminal_quota_refusal(cause):
            return True

    return False


# ---------------------------------------------------------------------------
# 5. Live 4-gate admission re-verification before switching
# ---------------------------------------------------------------------------


def _policy_allows_model(
    policy: Any,
    model_name: str,
    billing: str,
    *,
    model_pref: Mapping[str, Any] | None = None,
    ignore_custom_models: bool = False,
) -> tuple[bool, str | None]:
    """Re-verify ``model_name`` against ``policy`` using canonical ``availability.policy_refuses``."""
    if policy is None:
        return True, None
    from src.server.services.llm.availability import policy_refuses
    from src.server.services.policy import OWN_SUBSCRIPTIONS, Policy

    if isinstance(policy, Policy):
        pref_dict = dict(model_pref) if isinstance(model_pref, Mapping) else {}
        if ignore_custom_models:
            pref_dict = {
                k: v
                for k, v in pref_dict.items()
                if k not in ("custom_models", "custom_providers")
            }
        refused_rule = policy_refuses(policy, pref_dict, model_name)
        if refused_rule is not None:
            return False, f"policy_denied:{refused_rule}"
        if billing == "oauth" and not policy.allows(OWN_SUBSCRIPTIONS):
            return False, f"policy_denied:{OWN_SUBSCRIPTIONS}"
        return True, None

    # Fallback for duck-typed policy objects with Policy-compatible methods
    allows_model_fn = getattr(policy, "allows_model", None)
    if callable(allows_model_fn) and not allows_model_fn(model_name):
        return False, "policy_denied:models.allowed"
    if billing == "oauth":
        allows_fn = getattr(policy, "allows", None)
        if callable(allows_fn) and not allows_fn(OWN_SUBSCRIPTIONS):
            return False, f"policy_denied:{OWN_SUBSCRIPTIONS}"
    return True, None


def route_fingerprint_to_dict(route: RouteFingerprint) -> dict[str, Any]:
    """Serialize a 6-tuple :class:`RouteFingerprint` for durable ``active_fallback_route`` storage."""
    return {
        "model_key": route.model_key,
        "provider": route.provider,
        "model_id": route.model_id,
        "bound_base_url": route.bound_base_url,
        "key_fingerprint": route.key_fingerprint,
        "billing": route.billing,
    }


async def persist_active_fallback_route(
    run_id: str,
    route: RouteFingerprint,
    *,
    conn: Any = None,
) -> bool:
    """Persist ``active_fallback_route`` onto ``conversation_responses.metadata`` for resume verification."""
    cleaned_run_id = str(run_id or "").strip()
    if not cleaned_run_id or not route.model_key:
        return False
    payload = route_fingerprint_to_dict(route)
    sql = """
        UPDATE conversation_responses
        SET metadata = jsonb_set(
            COALESCE(metadata, '{}'::jsonb),
            '{active_fallback_route}',
            %s::jsonb,
            true
        )
        WHERE conversation_response_id = %s
    """
    if conn is not None:
        async with conn.cursor() as cur:
            await cur.execute(sql, (SafeJson(payload), cleaned_run_id))
            return cur.rowcount > 0
    async with get_db_connection() as own_conn:
        async with own_conn.transaction():
            async with own_conn.cursor() as cur:
                await cur.execute(sql, (SafeJson(payload), cleaned_run_id))
                return cur.rowcount > 0


async def acquire_fallback_candidate_live(
    *,
    subject_user_id: str,
    candidate_model: str,
    run_id: str | None,
    payer_user_id: str | None = None,
    call_id: str | None = None,
    role: str = "analyst",
    require_vision: bool = False,
    ignore_custom_models: bool = False,
    snapshot: Mapping[str, Any] | None = None,
    policy: Any = None,
    model_pref: Mapping[str, Any] | None = None,
    conn: Any = None,
    require_policy_fn: Any = None,
    availability_fn: Any = None,
    resolve_route_fn: Any = None,
) -> FallbackAdmissionResult:
    """Execute live admission gates immediately prior to switching to ``candidate_model``.

    Strictly confined to ``$0`` OAuth subscription candidates:
    1. Role modality, role-policy prohibition, system-catalog isolation
       (``ignore_custom_models=True`` rejects user custom models), and original snapshot
       ``RouteFingerprint`` consistency.
    2. Fresh org policy (``require_policy(subject_user_id)`` + ``policy_refuses``) with real
       model preferences and ``OWN_SUBSCRIPTIONS`` check.
    3. Strict admission-subject / billing-payer identity unity and ``$0`` OAuth-only gate
       (cash/platform candidates are unconditionally rejected).
    4. Live OAuth subscription availability probe (``inspect_subscription_availability``).
    """
    _ = (run_id, call_id, role)
    mk = str(candidate_model or "").strip()
    subj = str(subject_user_id or "").strip()
    if not mk or not subj:
        return FallbackAdmissionResult(
            admitted=False, model_name=mk, reason="invalid_candidate_or_subject"
        )

    eff_payer = str(payer_user_id or subj).strip()

    # Gate 1a: Role policy forbidden variants (L21)
    try:
        from src.dalton.llm.role_policy import is_forbidden_for_roles

        if is_forbidden_for_roles(mk):
            return FallbackAdmissionResult(
                admitted=False, model_name=mk, reason="forbidden_for_roles"
            )
    except Exception:
        pass

    # Gate 1b: Load model preferences when omitted & enforce system catalog isolation
    if isinstance(model_pref, Mapping):
        prefs: dict[str, Any] = dict(model_pref)
    elif not ignore_custom_models:
        try:
            from src.server.services.llm import user_models

            loaded_pref = await user_models.get_model_preference(subj)
            prefs = dict(loaded_pref) if isinstance(loaded_pref, Mapping) else {}
        except Exception:
            prefs = {}
    else:
        prefs = {}

    if ignore_custom_models:
        try:
            from src.llms.llm import LLM as LLMFactory

            mc = LLMFactory.get_model_config()
            if mc.get_model_config(mk) is None:
                return FallbackAdmissionResult(
                    admitted=False, model_name=mk, reason="custom_model_isolated"
                )
        except Exception:
            pass

    # Gate 1c: Vision modality check if required by current request
    if require_vision:
        try:
            from src.llms.llm import LLM as LLMFactory

            entry = LLMFactory.get_model_config().get_model_config(mk) or {}
            mods = entry.get("input_modalities") or ["text"]
            if "image" not in mods:
                return FallbackAdmissionResult(
                    admitted=False, model_name=mk, reason="modality_mismatch"
                )
        except Exception:
            pass

    # Gate 1d: Snapshot RouteFingerprint consistency check (explicit -> resume target -> active)
    active_snap: Mapping[str, Any] | None = snapshot
    if active_snap is None:
        try:
            active_snap = await load_resume_source_snapshot(conn=conn)
        except Exception:
            active_snap = None
    if active_snap is None:
        active_snap = get_active_catalog_snapshot()

    has_snap_models = (
        isinstance(active_snap, Mapping)
        and isinstance(active_snap.get("models"), Mapping)
        and bool(active_snap.get("models"))
    )

    live_route: RouteFingerprint | None = None
    try:
        _resolve_route = resolve_route_fn or resolve_live_route_fingerprint
        live_route = await _resolve_route(
            mk,
            user_id=subj,
            model_pref=prefs or None,
            ignore_custom_models=ignore_custom_models,
            conn=conn,
        )
    except HTTPException as exc:
        return FallbackAdmissionResult(
            admitted=False,
            model_name=mk,
            reason=str(
                (exc.detail or {}).get("code")
                if isinstance(exc.detail, Mapping)
                else "route_check_failed"
            ),
        )
    except Exception:
        live_route = None

    if has_snap_models:
        if live_route is None:
            return FallbackAdmissionResult(
                admitted=False,
                model_name=mk,
                reason="route_check_failed",
            )
        ok_route, _diffs = await verify_candidate_route_against_snapshot(
            mk,
            live_route,
            snapshot=active_snap,
            conn=conn,
            allow_unrecorded=False,
        )
        if not ok_route:
            return FallbackAdmissionResult(
                admitted=False,
                model_name=mk,
                reason="catalog_route_changed",
                route=live_route,
            )

    # Gate 2: Fresh org policy via canonical policy_refuses
    effective_policy = policy
    if effective_policy is None:
        try:
            if require_policy_fn is not None:
                effective_policy = await require_policy_fn(subj)
            else:
                from src.server.services.account import require_policy

                effective_policy = await require_policy(subj)
        except Exception:
            return FallbackAdmissionResult(
                admitted=False, model_name=mk, reason="policy_unavailable"
            )

    billing, _ = _classify_candidate_billing(
        mk, prefs=prefs, ignore_custom_models=ignore_custom_models
    )
    if live_route is not None and live_route.billing:
        rb = live_route.billing.lower()
        if rb == "subscription":
            billing = "oauth"
        elif rb in ("cash", "platform", "oauth"):
            billing = rb

    allowed_ok, policy_reason = _policy_allows_model(
        effective_policy,
        mk,
        billing,
        model_pref=prefs,
        ignore_custom_models=ignore_custom_models,
    )
    if not allowed_ok:
        return FallbackAdmissionResult(
            admitted=False,
            model_name=mk,
            reason=policy_reason,
            billing=billing,
            route=live_route,
        )

    # Gate 3: Strict admission-subject / billing-payer unity & $0 OAuth-only gate
    if payer_user_id is not None and eff_payer != subj:
        return FallbackAdmissionResult(
            admitted=False,
            model_name=mk,
            reason="payer_subject_mismatch",
            billing=billing,
            route=live_route,
        )

    if billing != "oauth":
        return FallbackAdmissionResult(
            admitted=False,
            model_name=mk,
            reason=(
                "cash_fallback_disabled"
                if billing == "cash"
                else "non_subscription_fallback_disabled"
            ),
            billing=billing,
            route=live_route,
        )

    # Gate 4: OAuth subscription candidate — verify not auth-broken/disconnected or cooled down
    _avail_fn = availability_fn or inspect_subscription_availability
    avail = await _avail_fn(
        subj,
        mk,
        prefs=prefs or None,
        ignore_custom_models=ignore_custom_models,
    )
    if not avail.available:
        return FallbackAdmissionResult(
            admitted=False,
            model_name=mk,
            reason=avail.refusal_code or avail.kind or "subscription_unavailable",
            billing=billing,
            route=live_route,
        )

    return FallbackAdmissionResult(
        admitted=True,
        model_name=mk,
        billing=billing,
        route=live_route,
    )


async def resolve_unattended_fallback_clients(
    config: Any,
    user_id: str,
    model_pref: dict[str, Any],
    byok_cache: dict[str, Any],
    *,
    is_byok: bool,
    cache_key: str | None,
    policy: Any = None,
    reasoning_effort: str | None = None,
    fast_mode: bool | None = None,
    ignore_custom_models: bool = False,
) -> bool:
    """Build ``$0`` OAuth unattended fallback clients on ``config`` when switch & context are active.

    Returns ``True`` if handled (caller should return), ``False`` to run upstream logic.
    - Passes ONLY explicit ``ctx.task_fallbacks`` (never ``config.llm.fallback``, which already
      carries personal ``model_pref["fallback_models"]``) so ``disable_fallback: true`` is honored.
    - On resume, verifies every candidate against the original interrupted run's snapshot
      BEFORE building its client, binds the verified variant during client construction, and
      carries the original snapshot onto ``config`` and each stamped client.
    """
    if not is_unattended_quota_fallback_enabled():
        return False
    ctx = get_active_unattended_turn_context()
    if ctx is None or not ctx.is_unattended:
        return False

    primary_name = getattr(getattr(config, "llm", None), "name", None)
    eff_user_id = ctx.subject_user_id or user_id
    eff_payer_id = ctx.payer_user_id or eff_user_id
    eff_ignore_custom = bool(ignore_custom_models or ctx.ignore_custom_models)

    candidates = resolve_unattended_fallback_chain(
        task_fallbacks=ctx.task_fallbacks,
        user_prefs=model_pref,
        primary_model=primary_name,
        user_id=eff_user_id,
        ignore_custom_models=eff_ignore_custom,
    )

    # Load original interrupted run's snapshot when resuming so runtime fallbacks are built
    # and later verified strictly against the original snapshot's candidate fingerprints.
    resume_snap: dict[str, Any] | None = None
    try:
        resume_snap = await load_resume_source_snapshot()
    except Exception:
        resume_snap = None
    if resume_snap is None:
        active_snap = get_active_catalog_snapshot()
        if isinstance(active_snap, Mapping):
            resume_snap = copy.deepcopy(dict(active_snap))

    has_snap_models = (
        isinstance(resume_snap, Mapping)
        and isinstance(resume_snap.get("models"), Mapping)
        and bool(resume_snap.get("models"))
    )
    if has_snap_models:
        try:
            setattr(config, "_dalton_resume_catalog_snapshot", copy.deepcopy(dict(resume_snap)))
        except Exception:
            pass

    if not candidates:
        config.fallback_llm_clients = []
        config.fallback_llm_names = []
        return True

    from ptc_agent.config.agent import CredentialSource
    from src.llms.preferences import resolve_turn_tuning
    from src.server.services.llm.clients import _stamp, _tier_for, resolve_model_client

    merged_clients: list[Any] = []
    merged_names: list[str] = []

    for model_name in candidates:
        verified_route: RouteFingerprint | None = None
        if has_snap_models:
            try:
                live_route = await resolve_live_route_fingerprint(
                    model_name,
                    user_id=eff_user_id,
                    model_pref=model_pref,
                    ignore_custom_models=eff_ignore_custom,
                )
            except Exception:
                logger.info(
                    "[UNATTENDED_FALLBACK] Skipping resume fallback %s: route lookup failed",
                    model_name,
                )
                continue
            ok_route, diffs = await verify_candidate_route_against_snapshot(
                model_name,
                live_route,
                snapshot=resume_snap,
                allow_unrecorded=False,
            )
            if not ok_route:
                logger.warning(
                    "[UNATTENDED_FALLBACK] Skipping resume fallback %s: route changed vs original snapshot (%s)",
                    model_name,
                    sorted(diffs.keys()),
                )
                continue
            verified_route = live_route

        try:
            tuning = resolve_turn_tuning(
                model_pref,
                model_name,
                reasoning_effort=reasoning_effort,
                fast_mode=fast_mode,
            )
            with (
                bind_verified_resume_variant_binding(verified_route)
                if verified_route is not None
                else nullcontext()
            ):
                fc = await resolve_model_client(
                    eff_user_id,
                    model_name,
                    is_byok=is_byok,
                    cache_key=cache_key,
                    allow_platform_fallback=False,
                    service_tier=_tier_for(tuning.fast_mode),
                    reasoning_effort=tuning.reasoning_effort,
                    policy=policy,
                    _pref_cache=model_pref,
                    _byok_cache=byok_cache,
                    **({"ignore_custom_models": True} if eff_ignore_custom else {}),
                )
        except Exception:
            logger.debug(
                "[UNATTENDED_FALLBACK] Skipping candidate %s during client build",
                model_name,
                exc_info=True,
            )
            continue
        if fc is None or fc.client is None:
            continue
        cred_src = getattr(fc, "credential_source", None)
        if cred_src is not None and cred_src != CredentialSource.OAUTH:
            logger.info(
                "[UNATTENDED_FALLBACK] Skipping non-OAuth fallback client %s (credential_source=%s)",
                model_name,
                cred_src,
            )
            continue
        call_meta = getattr(fc.client, "_call_metadata", None)
        if isinstance(call_meta, Mapping):
            bt = call_meta.get("billing_type")
            if bt is not None and str(bt).strip().lower() != "oauth":
                logger.info(
                    "[UNATTENDED_FALLBACK] Skipping non-OAuth fallback client %s (billing_type=%s)",
                    model_name,
                    bt,
                )
                continue
        _stamp(config, fc.client, getattr(config, "prompt_guidance", None))
        stamp_quota_or_auth_only(
            fc.client,
            candidate_name=model_name,
            subject_user_id=eff_user_id,
            payer_user_id=eff_payer_id,
            run_id=ctx.run_id,
            ignore_custom_models=eff_ignore_custom,
            role=ctx.role,
            require_vision=ctx.require_vision,
            credential_source="oauth",
            catalog_snapshot=resume_snap if has_snap_models else None,
        )
        merged_clients.append(fc.client)
        merged_names.append(model_name)

    config.fallback_llm_clients = merged_clients
    config.fallback_llm_names = merged_names
    try:
        setattr(config, "_dalton_unattended_fallback_names", list(merged_names))
    except Exception:
        pass
    return True


async def verify_middleware_fallback_switch(
    *,
    candidate_name: str,
    candidate_client: Any,
    last_exc: BaseException | None,
    primary_quota_skipped: bool,
    quota_benched: bool = False,
    call_id: str | None = None,
    credit_lease: Any = None,
) -> bool:
    """Gate switching to a ``quota_or_auth_only`` client inside ``ModelResilienceMiddleware``."""
    _ = (quota_benched, credit_lease)
    if not is_quota_or_auth_only_client(candidate_client):
        return True
    # Every switch must re-verify the immediately preceding failure; primary_quota_skipped
    # only applies when no prior candidate actually ran and failed on this call.
    if last_exc is not None:
        if not is_quota_or_auth_switch_trigger(last_exc):
            return False
    elif not primary_quota_skipped:
        return False

    meta = get_unattended_fallback_meta(candidate_client) or {}
    cred_src = meta.get("credential_source")
    if cred_src is not None and str(cred_src).strip().lower() != "oauth":
        return False
    call_meta = getattr(candidate_client, "_call_metadata", None)
    if isinstance(call_meta, Mapping):
        bt = call_meta.get("billing_type")
        if bt is not None and str(bt).strip().lower() != "oauth":
            return False

    subject_user_id = str(meta.get("subject_user_id") or "").strip()
    if not subject_user_id:
        return True

    eff_run_id = meta.get("run_id")
    snap_from_meta = meta.get("catalog_snapshot")
    eff_snapshot = snap_from_meta if isinstance(snap_from_meta, Mapping) else None

    result = await acquire_fallback_candidate_live(
        subject_user_id=subject_user_id,
        payer_user_id=meta.get("payer_user_id"),
        candidate_model=candidate_name,
        run_id=eff_run_id,
        call_id=call_id,
        role=str(meta.get("role") or "analyst"),
        require_vision=bool(meta.get("require_vision", False)),
        ignore_custom_models=bool(meta.get("ignore_custom_models", False)),
        snapshot=eff_snapshot,
    )
    if not result.admitted:
        logger.info(
            "[UNATTENDED_FALLBACK] Live admission rejected candidate=%s reason=%s",
            candidate_name,
            result.reason,
        )
        return False

    # Persist active_fallback_route for crash/wakeup checkpoint resume verification.
    if result.route is not None:
        try:
            meta_dict = getattr(candidate_client, UNATTENDED_FALLBACK_META_ATTR, None)
            if isinstance(meta_dict, dict):
                meta_dict["active_fallback_route"] = route_fingerprint_to_dict(result.route)
        except Exception:
            pass
        if eff_run_id:
            try:
                await persist_active_fallback_route(str(eff_run_id), result.route)
            except Exception:
                logger.debug(
                    "[UNATTENDED_FALLBACK] Failed persisting active_fallback_route for run=%s candidate=%s",
                    eff_run_id,
                    candidate_name,
                    exc_info=True,
                )
    return True


# ---------------------------------------------------------------------------
# 6. Copy formatting & Item 3 failure-notice call-site stub
# ---------------------------------------------------------------------------


def format_unattended_quota_notice(
    kind: str,
    *,
    model_name: str,
    reset_at: datetime | None = None,
    fallback_model: str | None = None,
) -> str:
    """Format accurate user-visible notice distinguishing auth failure vs quota cooldown."""
    label = model_name or "当前订阅模型"
    if kind == "oauth_auth_unavailable":
        if fallback_model:
            return f"{label} 的登录授权已失效，本次已改用 {fallback_model}；请到设置里重新连接授权。"
        return f"{label} 的登录授权已失效，需要到设置里重新连接授权后才能继续运行。"
    if reset_at is not None:
        when = reset_at.astimezone(_HKT).strftime("%Y-%m-%d %H:%M") + " HKT"
        if fallback_model:
            return f"{label} 订阅额度已用完（预计 {when} 恢复），本次已改用 {fallback_model}。"
        return f"{label} 订阅额度已用完，预计 {when} 恢复。"
    if fallback_model:
        return f"{label} 订阅额度已用完（恢复时间未知），本次已改用 {fallback_model}。"
    return f"{label} 订阅额度已用完（恢复时间未知），请稍后重试或切换可用模型。"


async def _maybe_enqueue_failure_notice_in_tx(
    conn: Any,
    *,
    subject_kind: Literal["personal_automation", "company_automation", "data_task", "wakeup"],
    subject_id: str,
    source_key: str,
    recipient_user_id: str,
    failure_reason: str,
    summary: Mapping[str, Any],
    dedup_window_key: str | None = None,
) -> bool:
    """Optional call-site into Item 3 (``dalton_failure_notices``).

    Per design & Lead Decision #3: if Item 3's module does not exist yet or its
    switch is off, this call cleanly no-ops and returns ``False``.
    """
    # DALTON-HOOK(quota-exhaustion): call-site for Item 3 failure notice receipts
    try:
        from src.dalton import settings as dalton_settings

        enabled_fn = getattr(dalton_settings, "is_failure_notice_enabled", None)
        if callable(enabled_fn) and not enabled_fn():
            return False
        from src.dalton.notify import failure_receipts  # type: ignore[attr-defined]

        enqueue_fn = getattr(failure_receipts, "enqueue_failure_notice_in_tx", None)
        if not callable(enqueue_fn):
            return False
    except ImportError:
        return False

    await enqueue_fn(
        conn,
        subject_kind=subject_kind,
        subject_id=subject_id,
        source_key=source_key,
        recipient_user_id=recipient_user_id,
        failure_reason=failure_reason,
        summary=dict(summary),
        dedup_window_key=dedup_window_key,
    )
    return True


async def maybe_notify_shared_cooldown_admin(
    conn: Any,
    *,
    cooldown_key: str,
    provider: str | None,
    model_name: str,
    reset_at: datetime | None = None,
) -> bool:
    """Notify primary admin at most once per shared-credential cooldown window via Item 3."""
    if not cooldown_key:
        return False
    try:
        from src.dalton.access_tiers import get_primary_admin_user_id

        admin_id = await get_primary_admin_user_id()
    except Exception:
        admin_id = None
    if not admin_id:
        return False
    return await _maybe_enqueue_failure_notice_in_tx(
        conn,
        subject_kind="company_automation",
        subject_id=f"admin_cooldown:{cooldown_key}",
        source_key=provider or model_name,
        recipient_user_id=admin_id,
        failure_reason="subscription_quota_exhausted",
        summary={
            "model": model_name,
            "provider": provider,
            "reset_at": reset_at.isoformat() if reset_at else None,
            "message": format_unattended_quota_notice(
                "subscription_quota_exhausted",
                model_name=model_name,
                reset_at=reset_at,
            ),
        },
        dedup_window_key=f"admin_cooldown:{cooldown_key}",
    )


# ---------------------------------------------------------------------------
# 7. Wakeup shared quota deferral & mid-turn checkpoint-resume ledger
# ---------------------------------------------------------------------------


def _select_earliest_cooldown(
    availabilities: Sequence[SubscriptionAvailability],
    *,
    now: datetime,
) -> tuple[bool, datetime | None, bool, str | None, str | None]:
    """Inspect all candidate availabilities -> ``(pure_auth_failure, effective_reset_at, held_known, primary_model, cooldown_key)``."""
    cooldowns = [a for a in availabilities if not a.available and a.kind == "quota_cooldown"]
    auth_fails = [a for a in availabilities if not a.available and a.kind == "auth_unavailable"]
    primary_model = (availabilities[0].model_name if availabilities else "") or "当前订阅模型"

    if not cooldowns and auth_fails:
        return True, None, False, primary_model, None

    if not cooldowns:
        # Unknown non-auth refusal treated as unknown-reset cooldown
        return (
            False,
            now + timedelta(minutes=DEFAULT_UNKNOWN_RESET_DELAY_MINUTES),
            False,
            primary_model,
            None,
        )

    known_resets = [a for a in cooldowns if a.reset_at is not None]
    if known_resets:
        earliest = min(known_resets, key=lambda a: a.reset_at or now)
        return False, earliest.reset_at, True, primary_model, earliest.cooldown_key

    return (
        False,
        now + timedelta(minutes=DEFAULT_UNKNOWN_RESET_DELAY_MINUTES),
        False,
        primary_model,
        cooldowns[0].cooldown_key,
    )


def deterministic_wakeup_resume_request_key(root_job_id: str, defer_count: int) -> str:
    """Return a deterministic UUID string accepted by ``RetryRequest.request_key``."""
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"dalton:wakeup_resume:{str(root_job_id).strip()}:{int(defer_count)}",
        )
    )


def build_wakeup_quota_start_metadata(
    request: Any,
    *,
    user_id: str,
    workspace_id: str | None = None,
    effective_model: str | None = None,
) -> dict[str, Any]:
    """Build START-time wakeup quota continuity metadata stored on ``conversation_responses.metadata``.

    Returns ``{}`` when the main switch is off or ``request`` is not a wakeup turn.
    """
    if not is_unattended_quota_fallback_enabled():
        return {}
    if getattr(request, "query_type", None) != "wakeup":
        return {}
    wakeup_meta = getattr(request, "wakeup", None)
    if not isinstance(wakeup_meta, Mapping):
        return {}

    root_job_id = str(
        wakeup_meta.get("root_job_id")
        or wakeup_meta.get("job_id")
        or getattr(request, "request_key", None)
        or ""
    ).strip()
    notices = wakeup_meta.get("notices") or []
    wakeup_id = str(wakeup_meta.get("wakeup_id") or "").strip()
    if not wakeup_id and isinstance(notices, list) and notices:
        first_notice = notices[0]
        if isinstance(first_notice, Mapping):
            wakeup_id = str(first_notice.get("wakeup_id") or first_notice.get("id") or "").strip()

    first_due = wakeup_meta.get("first_due_at")
    first_due_iso = (
        first_due.isoformat()
        if isinstance(first_due, datetime)
        else (first_due.strip() if isinstance(first_due, str) and first_due.strip() else None)
    )
    ctx: dict[str, Any] = {
        "root_job_id": root_job_id or None,
        "wakeup_id": wakeup_id or root_job_id or None,
        "owner_user_id": str(user_id),
        "workspace_id": str(workspace_id) if workspace_id else None,
        "first_due_at": first_due_iso,
        "llm_model": effective_model or getattr(request, "llm_model", None),
    }
    return {"dalton_wakeup_quota_ctx": ctx}


def extract_single_candidate_quota_failure(
    exc: BaseException | None,
    *,
    model_name: str | None = None,
) -> dict[str, Any] | None:
    """Extract structured quota/auth refusal metadata from a single candidate exception."""
    if exc is None or not is_terminal_quota_refusal(exc):
        return None

    from src.dalton.services.automation_subscription_fallback import (
        is_subscription_unavailable_error,
    )
    from src.server.services.oauth_health import OAuthReauthRequired, OAuthRefreshUnavailable
    from src.server.services.oauth_protocol import OAuthPlanNoApiAccess

    refusal = read_refusal(exc)
    reset_at = refusal.until if refusal is not None else None
    if reset_at is None:
        exp_ts = getattr(exc, "expires_at", None)
        if isinstance(exp_ts, (int, float)) and exp_ts > 0:
            reset_at = datetime.fromtimestamp(float(exp_ts), tz=timezone.utc)
        else:
            cause = getattr(exc, "__cause__", None)
            if isinstance(cause, BaseException):
                c_exp = getattr(cause, "expires_at", None)
                if isinstance(c_exp, (int, float)) and c_exp > 0:
                    reset_at = datetime.fromtimestamp(float(c_exp), tz=timezone.utc)

    is_auth = is_subscription_unavailable_error(exc) or isinstance(
        exc, (OAuthReauthRequired, OAuthRefreshUnavailable, OAuthPlanNoApiAccess)
    )
    kind: Literal["auth_unavailable", "quota_cooldown"] = (
        "auth_unavailable" if is_auth else "quota_cooldown"
    )
    return {
        "kind": kind,
        "refusal_code": (
            refusal.reason
            if refusal is not None
            else ("oauth_reauth_required" if is_auth else "quota_exceeded")
        ),
        "reset_at": reset_at.isoformat() if reset_at is not None else None,
        "model_name": str(model_name or ""),
    }


def extract_mid_turn_quota_failure_payload(
    exc: BaseException | None,
    *,
    model_name: str | None = None,
) -> dict[str, Any] | None:
    """Build structured failure facts across ALL candidates for atomic wakeup finalize settlement."""
    if not is_unattended_quota_fallback_enabled():
        return None
    if exc is None:
        return None

    raw_candidates = getattr(exc, DALTON_CANDIDATE_FAILURES_ATTR, None)
    if not isinstance(raw_candidates, list):
        cause = getattr(exc, "__cause__", None)
        if isinstance(cause, BaseException):
            raw_candidates = getattr(cause, DALTON_CANDIDATE_FAILURES_ATTR, None)

    candidates: list[dict[str, Any]] = []
    if isinstance(raw_candidates, list):
        for item in raw_candidates:
            if isinstance(item, Mapping):
                candidates.append(dict(item))

    primary = extract_single_candidate_quota_failure(exc, model_name=model_name)
    if primary is None and not candidates:
        return None

    if primary is not None:
        if not candidates:
            candidates = [primary]
        elif primary.get("model_name") and not any(
            c.get("model_name") == primary.get("model_name") for c in candidates
        ):
            candidates.insert(0, primary)

    head = primary or candidates[0]
    return {
        "kind": head.get("kind", "quota_cooldown"),
        "refusal_code": head.get("refusal_code", "quota_exceeded"),
        "reset_at": head.get("reset_at"),
        "model_name": str(head.get("model_name") or model_name or ""),
        "candidates": candidates,
    }


async def evaluate_and_record_wakeup_deferral_tx(
    conn: Any,
    *,
    root_job_id: str,
    wakeup_id: str,
    thread_id: str,
    owner_user_id: str,
    step_key: str,
    availabilities: Sequence[SubscriptionAvailability],
    first_due_at: datetime | None = None,
    now: datetime | None = None,
    resume_from_run_id: str | None = None,
    resume_checkpoint_id: str | None = None,
    turn_index: int | None = None,
    workspace_id: str | None = None,
    enqueue_resume_hook: bool = False,
) -> WakeupDeferDecision:
    """Atomically evaluate and persist a wakeup quota deferral or checkpoint-resume inside ``conn``'s tx.

    Enforces:
    - Transaction-scoped advisory lock per ``root_job_id`` so concurrent workers racing on
      first row insertion serialize cleanly before ``SELECT ... FOR UPDATE``.
    - Full step-key history in ``seen_step_keys`` (plus ``last_step_key``) so replaying ANY
      prior step key is idempotent without incrementing ``defer_count``.
    - Pure ``auth_unavailable`` fails immediately without deferring and notifies ``owner_user_id``.
    - Unknown ``reset_at`` uses ``now + 30m`` with ``held_known=False``.
    - Shared dual gate: ``defer_count + 1 <= 3`` AND ``effective_reset_at + 1m <= first_due_at + 24h``.
    - When ``enqueue_resume_hook=True`` (mid-turn exhaustion), inserts a ``wakeup_quota_resume``
      job into ``hook_outbox`` in the same transaction with a deterministic UUID ``request_key``,
      ``workspace_id``, and constant ``first_due_at``.
    """
    from src.server.database.sql_fences import advisory_key

    now_dt = now or datetime.now(timezone.utc)
    eff_first_due = first_due_at or now_dt
    eff_deadline = eff_first_due + timedelta(hours=WAKEUP_QUOTA_DEADLINE_HOURS)
    root_uuid = normalize_uuid(str(root_job_id)) or str(root_job_id)
    thread_uuid = normalize_uuid(str(thread_id)) or str(thread_id)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (advisory_key("WAKEUP_QUOTA_DEFER", root_uuid),),
        )
        await cur.execute(
            """
            SELECT root_job_id, wakeup_id, thread_id, owner_user_id,
                   first_due_at, deadline_at, defer_count, last_step_key,
                   seen_step_keys, resume_from_run_id, resume_checkpoint_id,
                   last_reset_at, last_reason, status
            FROM dalton_wakeup_quota_deferrals
            WHERE root_job_id = %s::uuid
            FOR UPDATE
            """,
            (root_uuid,),
        )
        row = await cur.fetchone()

    seen_steps: list[str] = []
    if row is not None:
        row = dict(row)
        existing_status = str(row["status"])
        existing_count = int(row["defer_count"] or 0)
        existing_deadline = _parse_iso_or_epoch(row.get("deadline_at")) or eff_deadline
        existing_reset = _parse_iso_or_epoch(row.get("last_reset_at"))
        raw_seen = row.get("seen_step_keys")
        if isinstance(raw_seen, list):
            seen_steps = [str(s) for s in raw_seen if isinstance(s, str) and s]
        last_sk = str(row.get("last_step_key") or "")
        if last_sk and last_sk not in seen_steps:
            seen_steps.append(last_sk)

        if step_key and (last_sk == step_key or step_key in seen_steps):
            sched = (
                existing_reset + timedelta(minutes=1)
                if existing_reset is not None
                else None
            )
            action: Literal["defer", "exhausted_failed", "already_settled"] = (
                "defer"
                if existing_status == "deferred"
                else (
                    "already_settled"
                    if existing_status == "settled"
                    else "exhausted_failed"
                )
            )
            return WakeupDeferDecision(
                action=action,
                root_job_id=str(row["root_job_id"]),
                defer_count=existing_count,
                effective_reset_at=existing_reset,
                held_known=row.get("last_reason") != "subscription_quota_unknown_reset",
                scheduled_wake_at=sched,
                deadline_at=existing_deadline,
                reason=str(row.get("last_reason") or "subscription_quota_exhausted"),
                idempotent_replay=True,
                resume_checkpoint_id=row.get("resume_checkpoint_id"),
            )

        if existing_status == "settled":
            return WakeupDeferDecision(
                action="already_settled",
                root_job_id=str(row["root_job_id"]),
                defer_count=existing_count,
                deadline_at=existing_deadline,
                reason="already_settled",
            )
        if existing_status == "exhausted_failed":
            return WakeupDeferDecision(
                action="exhausted_failed",
                root_job_id=str(row["root_job_id"]),
                defer_count=existing_count,
                deadline_at=existing_deadline,
                reason=str(row.get("last_reason") or "subscription_quota_exhausted"),
            )

        cur_count = existing_count
        eff_first_due = _parse_iso_or_epoch(row.get("first_due_at")) or eff_first_due
        eff_deadline = existing_deadline
    else:
        cur_count = 0

    next_seen_steps = list(seen_steps)
    if step_key and step_key not in next_seen_steps:
        next_seen_steps.append(step_key)

    pure_auth, effective_reset_at, held_known, primary_model, cooldown_key = (
        _select_earliest_cooldown(availabilities, now=now_dt)
    )

    if pure_auth:
        failure_reason = "oauth_auth_unavailable"
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_wakeup_quota_deferrals (
                    root_job_id, wakeup_id, thread_id, owner_user_id,
                    first_due_at, deadline_at, defer_count, last_step_key,
                    seen_step_keys, resume_from_run_id, resume_checkpoint_id,
                    last_reset_at, last_reason, status, updated_at
                )
                VALUES (
                    %s::uuid, %s, %s::uuid, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    NULL, %s, 'exhausted_failed', NOW()
                )
                ON CONFLICT (root_job_id) DO UPDATE
                SET last_step_key = EXCLUDED.last_step_key,
                    seen_step_keys = EXCLUDED.seen_step_keys,
                    resume_from_run_id = COALESCE(EXCLUDED.resume_from_run_id, dalton_wakeup_quota_deferrals.resume_from_run_id),
                    resume_checkpoint_id = COALESCE(EXCLUDED.resume_checkpoint_id, dalton_wakeup_quota_deferrals.resume_checkpoint_id),
                    last_reason = EXCLUDED.last_reason,
                    status = 'exhausted_failed',
                    updated_at = NOW()
                """,
                (
                    root_uuid,
                    str(wakeup_id),
                    thread_uuid,
                    owner_user_id,
                    eff_first_due,
                    eff_deadline,
                    cur_count,
                    step_key,
                    SafeJson(next_seen_steps),
                    resume_from_run_id,
                    resume_checkpoint_id,
                    failure_reason,
                ),
            )
        await _maybe_enqueue_failure_notice_in_tx(
            conn,
            subject_kind="wakeup",
            subject_id=str(root_uuid),
            source_key=str(wakeup_id),
            recipient_user_id=owner_user_id,
            failure_reason=failure_reason,
            summary={
                "wakeup_id": str(wakeup_id),
                "thread_id": str(thread_uuid),
                "model": primary_model,
                "defer_count": cur_count,
                "message": format_unattended_quota_notice(
                    failure_reason, model_name=primary_model
                ),
            },
        )
        return WakeupDeferDecision(
            action="exhausted_failed",
            root_job_id=str(root_uuid),
            defer_count=cur_count,
            effective_reset_at=None,
            held_known=False,
            scheduled_wake_at=None,
            deadline_at=eff_deadline,
            reason=failure_reason,
        )

    assert effective_reset_at is not None
    scheduled_wake_at = effective_reset_at + timedelta(minutes=1)
    next_count = cur_count + 1
    within_budget = (
        next_count <= MAX_WAKEUP_QUOTA_DEFERS and scheduled_wake_at <= eff_deadline
    )

    if not within_budget:
        failure_reason = (
            "wakeup_quota_defer_limit_exceeded"
            if next_count > MAX_WAKEUP_QUOTA_DEFERS
            else "wakeup_quota_deadline_exceeded"
        )
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_wakeup_quota_deferrals (
                    root_job_id, wakeup_id, thread_id, owner_user_id,
                    first_due_at, deadline_at, defer_count, last_step_key,
                    seen_step_keys, resume_from_run_id, resume_checkpoint_id,
                    last_reset_at, last_reason, status, updated_at
                )
                VALUES (
                    %s::uuid, %s, %s::uuid, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, 'exhausted_failed', NOW()
                )
                ON CONFLICT (root_job_id) DO UPDATE
                SET last_step_key = EXCLUDED.last_step_key,
                    seen_step_keys = EXCLUDED.seen_step_keys,
                    resume_from_run_id = COALESCE(EXCLUDED.resume_from_run_id, dalton_wakeup_quota_deferrals.resume_from_run_id),
                    resume_checkpoint_id = COALESCE(EXCLUDED.resume_checkpoint_id, dalton_wakeup_quota_deferrals.resume_checkpoint_id),
                    last_reset_at = EXCLUDED.last_reset_at,
                    last_reason = EXCLUDED.last_reason,
                    status = 'exhausted_failed',
                    updated_at = NOW()
                """,
                (
                    root_uuid,
                    str(wakeup_id),
                    thread_uuid,
                    owner_user_id,
                    eff_first_due,
                    eff_deadline,
                    cur_count,
                    step_key,
                    SafeJson(next_seen_steps),
                    resume_from_run_id,
                    resume_checkpoint_id,
                    effective_reset_at,
                    failure_reason,
                ),
            )
        await _maybe_enqueue_failure_notice_in_tx(
            conn,
            subject_kind="wakeup",
            subject_id=str(root_uuid),
            source_key=str(wakeup_id),
            recipient_user_id=owner_user_id,
            failure_reason="subscription_quota_exhausted",
            summary={
                "wakeup_id": str(wakeup_id),
                "thread_id": str(thread_uuid),
                "model": primary_model,
                "defer_count": cur_count,
                "reset_at": effective_reset_at.isoformat() if held_known else None,
                "limit_reason": failure_reason,
                "message": format_unattended_quota_notice(
                    "subscription_quota_exhausted",
                    model_name=primary_model,
                    reset_at=effective_reset_at if held_known else None,
                ),
            },
            dedup_window_key=f"wakeup_quota:{cooldown_key}" if cooldown_key else None,
        )
        return WakeupDeferDecision(
            action="exhausted_failed",
            root_job_id=str(root_uuid),
            defer_count=cur_count,
            effective_reset_at=effective_reset_at,
            held_known=held_known,
            scheduled_wake_at=scheduled_wake_at,
            deadline_at=eff_deadline,
            reason=failure_reason,
        )

    defer_reason = (
        "subscription_quota_cooldown"
        if held_known
        else "subscription_quota_unknown_reset"
    )
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO dalton_wakeup_quota_deferrals (
                root_job_id, wakeup_id, thread_id, owner_user_id,
                first_due_at, deadline_at, defer_count, last_step_key,
                seen_step_keys, resume_from_run_id, resume_checkpoint_id,
                last_reset_at, last_reason, status, updated_at
            )
            VALUES (
                %s::uuid, %s, %s::uuid, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, 'deferred', NOW()
            )
            ON CONFLICT (root_job_id) DO UPDATE
            SET defer_count = EXCLUDED.defer_count,
                last_step_key = EXCLUDED.last_step_key,
                seen_step_keys = EXCLUDED.seen_step_keys,
                resume_from_run_id = COALESCE(EXCLUDED.resume_from_run_id, dalton_wakeup_quota_deferrals.resume_from_run_id),
                resume_checkpoint_id = COALESCE(EXCLUDED.resume_checkpoint_id, dalton_wakeup_quota_deferrals.resume_checkpoint_id),
                last_reset_at = EXCLUDED.last_reset_at,
                last_reason = EXCLUDED.last_reason,
                status = 'deferred',
                updated_at = NOW()
            """,
            (
                root_uuid,
                str(wakeup_id),
                thread_uuid,
                owner_user_id,
                eff_first_due,
                eff_deadline,
                next_count,
                step_key,
                SafeJson(next_seen_steps),
                resume_from_run_id,
                resume_checkpoint_id,
                effective_reset_at,
                defer_reason,
            ),
        )

        if enqueue_resume_hook and resume_from_run_id:
            idem_key = f"wakeup_resume:{root_uuid}:{next_count}"
            req_uuid = deterministic_wakeup_resume_request_key(str(root_uuid), next_count)
            payload = {
                "root_job_id": str(root_uuid),
                "wakeup_id": str(wakeup_id),
                "owner_user_id": owner_user_id,
                "workspace_id": str(workspace_id) if workspace_id else None,
                "failed_run_id": str(resume_from_run_id),
                "turn_index": turn_index,
                "resume_checkpoint_id": resume_checkpoint_id,
                "defer_count": next_count,
                "first_due_at": eff_first_due.isoformat(),
                "request_key": req_uuid,
            }
            await cur.execute(
                """
                INSERT INTO hook_outbox (
                    run_id, conversation_thread_id, hook_type,
                    payload, ordering_key, idempotency_key,
                    created_at, next_retry_at
                )
                VALUES (
                    %s::uuid, %s::uuid, %s,
                    %s, %s, %s,
                    clock_timestamp(), %s
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (
                    str(resume_from_run_id),
                    thread_uuid,
                    WAKEUP_QUOTA_RESUME_HOOK,
                    SafeJson(payload),
                    f"{WAKEUP_QUOTA_RESUME_HOOK}:{thread_uuid}",
                    idem_key,
                    scheduled_wake_at,
                ),
            )

    return WakeupDeferDecision(
        action="defer",
        root_job_id=str(root_uuid),
        defer_count=next_count,
        effective_reset_at=effective_reset_at,
        held_known=held_known,
        scheduled_wake_at=scheduled_wake_at,
        deadline_at=eff_deadline,
        reason=defer_reason,
        resume_checkpoint_id=resume_checkpoint_id,
    )


async def evaluate_and_record_wakeup_deferral(
    *,
    root_job_id: str,
    wakeup_id: str,
    thread_id: str,
    owner_user_id: str,
    step_key: str,
    availabilities: Sequence[SubscriptionAvailability],
    first_due_at: datetime | None = None,
    now: datetime | None = None,
    resume_from_run_id: str | None = None,
    resume_checkpoint_id: str | None = None,
    turn_index: int | None = None,
    workspace_id: str | None = None,
    enqueue_resume_hook: bool = False,
    conn: Any = None,
) -> WakeupDeferDecision:
    """Convenience wrapper around :func:`evaluate_and_record_wakeup_deferral_tx`."""
    if conn is not None:
        return await evaluate_and_record_wakeup_deferral_tx(
            conn,
            root_job_id=root_job_id,
            wakeup_id=wakeup_id,
            thread_id=thread_id,
            owner_user_id=owner_user_id,
            step_key=step_key,
            availabilities=availabilities,
            first_due_at=first_due_at,
            now=now,
            resume_from_run_id=resume_from_run_id,
            resume_checkpoint_id=resume_checkpoint_id,
            turn_index=turn_index,
            workspace_id=workspace_id,
            enqueue_resume_hook=enqueue_resume_hook,
        )
    async with get_db_connection() as own_conn:
        async with own_conn.transaction():
            return await evaluate_and_record_wakeup_deferral_tx(
                own_conn,
                root_job_id=root_job_id,
                wakeup_id=wakeup_id,
                thread_id=thread_id,
                owner_user_id=owner_user_id,
                step_key=step_key,
                availabilities=availabilities,
                first_due_at=first_due_at,
                now=now,
                resume_from_run_id=resume_from_run_id,
                resume_checkpoint_id=resume_checkpoint_id,
                turn_index=turn_index,
                workspace_id=workspace_id,
                enqueue_resume_hook=enqueue_resume_hook,
            )


async def record_wakeup_mid_turn_quota_exhaustion_tx(
    conn: Any,
    *,
    thread_id: str,
    run_id: str,
    owner_user_id: str,
    root_job_id: str,
    wakeup_id: str,
    turn_index: int | None,
    availabilities: Sequence[SubscriptionAvailability],
    latest_checkpoint_id: str | None = None,
    first_due_at: datetime | None = None,
    workspace_id: str | None = None,
    now: datetime | None = None,
) -> WakeupDeferDecision:
    """Record mid-turn wakeup quota exhaustion and enqueue ``wakeup_quota_resume`` in ``conn``'s tx."""
    return await evaluate_and_record_wakeup_deferral_tx(
        conn,
        root_job_id=root_job_id,
        wakeup_id=wakeup_id,
        thread_id=thread_id,
        owner_user_id=owner_user_id,
        step_key=f"mid:{run_id}",
        availabilities=availabilities,
        first_due_at=first_due_at,
        now=now,
        resume_from_run_id=run_id,
        resume_checkpoint_id=latest_checkpoint_id,
        turn_index=turn_index,
        workspace_id=workspace_id,
        enqueue_resume_hook=True,
    )


async def maybe_record_wakeup_mid_turn_resume_in_tx(
    conn: Any,
    *,
    run_row: Mapping[str, Any],
    thread_id: str,
    run_id: str,
    newest_checkpoint_id: str | None = None,
    quota_failure: Mapping[str, Any] | None = None,
) -> WakeupDeferDecision | None:
    """Atomic finalize-transaction hook recording mid-turn wakeup exhaustion & ``wakeup_quota_resume``.

    Called inside ``lifecycle.finalize_run``'s transaction (same commit as ``status='error'``
    and terminal ``hook_outbox`` jobs). Cleanly no-ops when the main switch is off, the run
    is not a wakeup turn, or ``quota_failure`` is absent.
    """
    if not is_unattended_quota_fallback_enabled():
        return None
    if not isinstance(quota_failure, Mapping) or not quota_failure:
        return None

    run_meta = run_row.get("metadata") if isinstance(run_row, Mapping) else None
    if not isinstance(run_meta, Mapping):
        return None
    wake_ctx = run_meta.get("dalton_wakeup_quota_ctx")
    if not isinstance(wake_ctx, Mapping):
        return None

    owner_user_id = str(
        wake_ctx.get("owner_user_id")
        or run_meta.get("user_id")
        or run_row.get("user_id")
        or ""
    ).strip()
    if not owner_user_id:
        return None

    raw_root = str(wake_ctx.get("root_job_id") or "").strip()
    root_job_id = (
        normalize_uuid(raw_root)
        or str(uuid.uuid5(uuid.NAMESPACE_URL, f"wakeup:{thread_id}:{run_id}"))
    )
    wakeup_id = str(wake_ctx.get("wakeup_id") or root_job_id).strip()
    workspace_id = str(
        wake_ctx.get("workspace_id") or run_meta.get("workspace_id") or ""
    ).strip() or None
    first_due_at = _parse_iso_or_epoch(wake_ctx.get("first_due_at"))
    turn_index = run_row.get("turn_index")

    raw_candidates = quota_failure.get("candidates")
    availabilities: list[SubscriptionAvailability] = []
    if isinstance(raw_candidates, list) and raw_candidates:
        for entry in raw_candidates:
            if not isinstance(entry, Mapping):
                continue
            e_kind: Literal["auth_unavailable", "quota_cooldown"] = (
                "auth_unavailable"
                if str(entry.get("kind") or "") == "auth_unavailable"
                else "quota_cooldown"
            )
            availabilities.append(
                SubscriptionAvailability(
                    available=False,
                    kind=e_kind,
                    refusal_code=str(entry.get("refusal_code") or "quota_exceeded"),
                    reset_at=_parse_iso_or_epoch(entry.get("reset_at")),
                    model_name=str(
                        entry.get("model_name")
                        or wake_ctx.get("llm_model")
                        or run_meta.get("llm_model")
                        or ""
                    ),
                )
            )
    if not availabilities:
        raw_kind = str(quota_failure.get("kind") or "quota_cooldown")
        kind: Literal["auth_unavailable", "quota_cooldown"] = (
            "auth_unavailable" if raw_kind == "auth_unavailable" else "quota_cooldown"
        )
        availabilities = [
            SubscriptionAvailability(
                available=False,
                kind=kind,
                refusal_code=str(quota_failure.get("refusal_code") or "quota_exceeded"),
                reset_at=_parse_iso_or_epoch(quota_failure.get("reset_at")),
                model_name=str(
                    quota_failure.get("model_name")
                    or wake_ctx.get("llm_model")
                    or run_meta.get("llm_model")
                    or ""
                ),
            )
        ]

    return await record_wakeup_mid_turn_quota_exhaustion_tx(
        conn,
        thread_id=thread_id,
        run_id=run_id,
        owner_user_id=owner_user_id,
        root_job_id=root_job_id,
        wakeup_id=wakeup_id,
        turn_index=int(turn_index) if isinstance(turn_index, int) else None,
        availabilities=availabilities,
        latest_checkpoint_id=newest_checkpoint_id,
        first_due_at=first_due_at,
        workspace_id=workspace_id,
    )


async def mark_wakeup_deferral_settled(
    root_job_id: str,
    *,
    conn: Any = None,
) -> bool:
    """Mark a ``dalton_wakeup_quota_deferrals`` row as ``settled``."""
    root_uuid = normalize_uuid(str(root_job_id))
    if root_uuid is None:
        return False
    sql = """
        UPDATE dalton_wakeup_quota_deferrals
        SET status = 'settled',
            updated_at = NOW()
        WHERE root_job_id = %s::uuid
          AND status = 'deferred'
    """
    if conn is not None:
        async with conn.cursor() as cur:
            await cur.execute(sql, (root_uuid,))
            return cur.rowcount > 0
    async with get_db_connection() as own_conn:
        async with own_conn.transaction():
            async with own_conn.cursor() as cur:
                await cur.execute(sql, (root_uuid,))
                return cur.rowcount > 0


async def _check_and_enforce_resume_deadline(
    *,
    root_job_id: str,
    wakeup_id: str,
    thread_id: str,
    owner_user_id: str,
    now: datetime,
) -> tuple[bool, int | None, datetime | None, str | None]:
    """Re-verify the deferral row before dispatching a wakeup or ``/retry`` resume.

    Returns ``(should_dispatch, current_defer_count, first_due_at, terminal_reason)``.
    - If ``status`` is already terminal (``settled`` / ``exhausted_failed``), returns ``(False, ...)``.
    - If ``deadline_at <= now``, atomically marks the row ``exhausted_failed`` (with Item 3 notice)
      and returns ``(False, ..., 'wakeup_quota_deadline_exceeded')``.
    - Database errors propagate so callers fail closed and NEVER dispatch past an unverified deadline.
    """
    root_uuid = normalize_uuid(str(root_job_id))
    if root_uuid is None:
        return True, None, None, None
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT root_job_id, wakeup_id, thread_id, owner_user_id,
                           first_due_at, deadline_at, defer_count, last_reason, status
                    FROM dalton_wakeup_quota_deferrals
                    WHERE root_job_id = %s::uuid
                    FOR UPDATE
                    """,
                    (root_uuid,),
                )
                row = await cur.fetchone()
            if row is None:
                return True, None, None, None
            row = dict(row)
            st = str(row.get("status") or "")
            cur_count = int(row.get("defer_count") or 0)
            first_due = _parse_iso_or_epoch(row.get("first_due_at"))
            deadline = _parse_iso_or_epoch(row.get("deadline_at"))
            if st in ("settled", "exhausted_failed"):
                reason = str(row.get("last_reason") or ("already_settled" if st == "settled" else "subscription_quota_exhausted"))
                return False, cur_count, first_due, reason
            if deadline is not None and now >= deadline:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE dalton_wakeup_quota_deferrals
                        SET status = 'exhausted_failed',
                            last_reason = 'wakeup_quota_deadline_exceeded',
                            updated_at = NOW()
                        WHERE root_job_id = %s::uuid
                          AND status = 'deferred'
                        """,
                        (root_uuid,),
                    )
                await _maybe_enqueue_failure_notice_in_tx(
                    conn,
                    subject_kind="wakeup",
                    subject_id=str(root_uuid),
                    source_key=str(row.get("wakeup_id") or wakeup_id or root_uuid),
                    recipient_user_id=str(row.get("owner_user_id") or owner_user_id),
                    failure_reason="subscription_quota_exhausted",
                    summary={
                        "wakeup_id": str(row.get("wakeup_id") or wakeup_id or root_uuid),
                        "thread_id": str(thread_id),
                        "defer_count": cur_count,
                        "limit_reason": "wakeup_quota_deadline_exceeded",
                        "message": format_unattended_quota_notice(
                            "subscription_quota_exhausted",
                            model_name="当前订阅模型",
                        ),
                    },
                )
                return False, cur_count, first_due, "wakeup_quota_deadline_exceeded"
            return True, cur_count, first_due, None


async def check_wakeup_deferral_admission_deadline(
    *,
    root_job_id: str,
    wakeup_id: str,
    thread_id: str,
    owner_user_id: str,
    now: datetime | None = None,
) -> None:
    """Enforce the 24h wakeup deferral deadline at turn admission (for both normal wakeups and /retry).

    - Raises HTTP 400 ``dalton_wakeup_quota_exhausted`` if the ledger row is terminal or past deadline.
    - Raises HTTP 503 ``service_unavailable`` if the deferral ledger cannot be read (fail-closed;
      the caller defers/reschedules rather than dispatching without verifying the deadline).
    """
    now_dt = now or datetime.now(timezone.utc)
    try:
        should_dispatch, cur_count, _first_due, terminal_reason = (
            await _check_and_enforce_resume_deadline(
                root_job_id=root_job_id,
                wakeup_id=wakeup_id,
                thread_id=thread_id,
                owner_user_id=owner_user_id,
                now=now_dt,
            )
        )
    except Exception as exc:
        logger.warning(
            "[UNATTENDED_FALLBACK] Deferral ledger check failed at admission for root_job=%s: %s",
            root_job_id,
            exc,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "type": "service_unavailable",
                "message": "Wakeup quota deferral ledger temporarily unavailable.",
            },
        ) from exc

    if not should_dispatch:
        raise build_wakeup_cooldown_http_exception(
            WakeupDeferDecision(
                action="exhausted_failed",
                root_job_id=str(root_job_id),
                defer_count=int(cur_count or 0),
                reason=terminal_reason or "wakeup_quota_deadline_exceeded",
            )
        )


async def _settle_disabled_wakeup_resume(
    *,
    root_job_id: str,
    wakeup_id: str,
    thread_id: str,
    owner_user_id: str,
) -> None:
    """Settle a queued ``wakeup_quota_resume`` as failed when the main switch is turned off."""
    root_uuid = normalize_uuid(str(root_job_id))
    if root_uuid is None:
        return
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    UPDATE dalton_wakeup_quota_deferrals
                    SET status = 'exhausted_failed',
                        last_reason = 'unattended_quota_fallback_disabled',
                        updated_at = NOW()
                    WHERE root_job_id = %s::uuid
                      AND status = 'deferred'
                    RETURNING wakeup_id, owner_user_id, defer_count
                    """,
                    (root_uuid,),
                )
                updated = await cur.fetchone()
            if updated is not None:
                u_row = dict(updated)
                await _maybe_enqueue_failure_notice_in_tx(
                    conn,
                    subject_kind="wakeup",
                    subject_id=str(root_uuid),
                    source_key=str(u_row.get("wakeup_id") or wakeup_id or root_uuid),
                    recipient_user_id=str(u_row.get("owner_user_id") or owner_user_id),
                    failure_reason="subscription_quota_exhausted",
                    summary={
                        "wakeup_id": str(u_row.get("wakeup_id") or wakeup_id or root_uuid),
                        "thread_id": str(thread_id),
                        "defer_count": int(u_row.get("defer_count") or 0),
                        "limit_reason": "unattended_quota_fallback_disabled",
                        "message": format_unattended_quota_notice(
                            "subscription_quota_exhausted",
                            model_name="当前订阅模型",
                        ),
                    },
                )


def _is_own_dispatched_resume_row(
    row: Mapping[str, Any] | None,
    *,
    request_key: str,
    failed_run_id: str,
) -> bool:
    """Return True when ``row`` is the retry run already dispatched by this outbox job."""
    if not isinstance(row, Mapping):
        return False
    row_req = str(row.get("request_key") or "").strip()
    row_retry_of = str(row.get("retry_of_run_id") or "").strip()
    return bool(request_key and failed_run_id and row_req == request_key and row_retry_of == failed_run_id)


async def _has_dispatched_wakeup_resume_run(
    thread_id: str,
    *,
    request_key: str,
    failed_run_id: str,
) -> bool:
    """Return True if a retry run with ``(request_key, retry_of_run_id=failed_run_id)`` already exists."""
    if not thread_id or not request_key or not failed_run_id:
        return False
    failed_uuid = normalize_uuid(str(failed_run_id)) or str(failed_run_id)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1
                FROM conversation_responses
                WHERE conversation_thread_id = %s
                  AND request_key = %s
                  AND retry_of_run_id::text = %s
                LIMIT 1
                """,
                (str(thread_id), str(request_key), str(failed_uuid)),
            )
            return (await cur.fetchone()) is not None


async def execute_wakeup_quota_resume(job: Mapping[str, Any]) -> None:
    """Outbox executor for ``wakeup_quota_resume`` jobs across any active worker.

    - Checks ``is_unattended_quota_fallback_enabled()`` first: when OFF, settles the deferral
      row as ``exhausted_failed`` (keeping the original failure settled) without dispatching.
    - Re-checks the 24h deadline before dispatch AND before every internal HTTP retry via
      ``still_due`` (DB check failure raises to reschedule rather than dispatching).
    - Recognizes its own already-dispatched resume run by deterministic ``request_key`` and
      ``retry_of_run_id == failed_run_id`` so re-claiming an unacked job settles ONLY the old
      outbox job and never marks the shared deferral row ``settled``.
    """
    payload = job.get("payload") or {}
    thread_id = str(job.get("conversation_thread_id") or payload.get("thread_id") or "").strip()
    job_id = str(job.get("hook_outbox_id") or "").strip()
    attempts = int(job.get("attempts") or 1)
    root_job_id = str(payload.get("root_job_id") or "").strip()
    wakeup_id = str(payload.get("wakeup_id") or root_job_id).strip()
    failed_run_id = str(payload.get("failed_run_id") or job.get("run_id") or "").strip()
    owner_user_id = str(payload.get("owner_user_id") or "").strip()
    resume_checkpoint_id = payload.get("resume_checkpoint_id")
    payload_defer_count = int(payload.get("defer_count") or 1)
    defer_count = payload_defer_count
    first_due_at = _parse_iso_or_epoch(payload.get("first_due_at"))

    if not thread_id or not failed_run_id or not owner_user_id:
        logger.warning(
            "[UNATTENDED_FALLBACK] Malformed wakeup_quota_resume job=%s; dropping",
            job.get("hook_outbox_id"),
        )
        return

    # Main switch OFF: settle original failure cleanly without dispatching any resume.
    if not is_unattended_quota_fallback_enabled():
        logger.info(
            "[UNATTENDED_FALLBACK] Main switch disabled; settling queued wakeup_quota_resume root_job=%s job=%s without dispatch",
            root_job_id,
            job_id,
        )
        if root_job_id:
            await _settle_disabled_wakeup_resume(
                root_job_id=root_job_id,
                wakeup_id=wakeup_id,
                thread_id=thread_id,
                owner_user_id=owner_user_id,
            )
        return

    # Immutable request_key for this specific outbox job (survives later defer_count increments).
    req_key = str(payload.get("request_key") or "").strip() or deterministic_wakeup_resume_request_key(
        root_job_id or failed_run_id, payload_defer_count
    )

    # Enforce 24h deadline & ledger status first (DB errors propagate so outbox reschedules, never dispatches).
    now_dt = datetime.now(timezone.utc)
    if root_job_id:
        should_dispatch, db_defer_count, db_first_due, _term_reason = (
            await _check_and_enforce_resume_deadline(
                root_job_id=root_job_id,
                wakeup_id=wakeup_id,
                thread_id=thread_id,
                owner_user_id=owner_user_id,
                now=now_dt,
            )
        )
        if not should_dispatch:
            logger.info(
                "[UNATTENDED_FALLBACK] Wakeup resume root_job=%s already terminal or past 24h deadline; settling outbox job=%s",
                root_job_id,
                job_id,
            )
            return
        if db_defer_count is not None and db_defer_count > defer_count:
            defer_count = db_defer_count
        if db_first_due is not None:
            first_due_at = db_first_due

    from src.server.database import conversation as conv_db
    from src.server.database.runs import lifecycle as tl_db

    # Check if this outbox job ALREADY dispatched its retry before worker restart / lease loss.
    latest = await tl_db.get_latest_attempt(thread_id)
    if _is_own_dispatched_resume_row(
        latest, request_key=req_key, failed_run_id=failed_run_id
    ):
        logger.info(
            "[UNATTENDED_FALLBACK] Wakeup resume job=%s already dispatched run=%s (request_key=%s); acking old outbox job only",
            job_id,
            latest.get("conversation_response_id") if isinstance(latest, Mapping) else None,
            req_key,
        )
        return

    if latest is None or str(latest.get("conversation_response_id") or "") != failed_run_id:
        # Before assuming another turn superseded us, verify whether our own retry run exists
        # earlier in the thread history (e.g. it ran, exhausted again, and created a newer retry).
        if await _has_dispatched_wakeup_resume_run(
            thread_id, request_key=req_key, failed_run_id=failed_run_id
        ):
            logger.info(
                "[UNATTENDED_FALLBACK] Wakeup resume job=%s already dispatched earlier (request_key=%s); acking old outbox job only",
                job_id,
                req_key,
            )
            return
        # User or another turn genuinely advanced this thread; safely yield and settle.
        logger.info(
            "[UNATTENDED_FALLBACK] Thread %s moved past failed_run=%s; marking deferral settled",
            thread_id,
            failed_run_id,
        )
        if root_job_id:
            await mark_wakeup_deferral_settled(root_job_id)
        return

    if str(latest.get("status") or "") not in ("error", "failed"):
        if root_job_id:
            await mark_wakeup_deferral_settled(root_job_id)
        return

    workspace_id = str(payload.get("workspace_id") or "").strip()
    if not workspace_id:
        latest_meta = latest.get("metadata") if isinstance(latest, Mapping) else None
        if isinstance(latest_meta, Mapping) and latest_meta.get("workspace_id"):
            workspace_id = str(latest_meta["workspace_id"]).strip()
    if not workspace_id:
        thread_rec = await conv_db.get_thread_by_id(thread_id)
        if isinstance(thread_rec, Mapping) and thread_rec.get("workspace_id"):
            workspace_id = str(thread_rec["workspace_id"]).strip()
    if not workspace_id:
        logger.warning(
            "[UNATTENDED_FALLBACK] Thread %s has no workspace_id for wakeup_quota_resume job=%s; dropping",
            thread_id,
            job_id,
        )
        if root_job_id:
            await mark_wakeup_deferral_settled(root_job_id)
        return

    resume_meta: dict[str, Any] = {
        "root_job_id": root_job_id or failed_run_id,
        "job_id": job_id or root_job_id or failed_run_id,
        "attempts": attempts,
        "wakeup_id": wakeup_id or root_job_id or failed_run_id,
    }
    if first_due_at is not None:
        resume_meta["first_due_at"] = first_due_at.isoformat()

    retry_body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "run_id": failed_run_id,
        "checkpoint_id": resume_checkpoint_id,
        "request_key": req_key,
        "wakeup_resume": resume_meta,
    }

    from src.server.services.report_back.notify_turn import post_notification_turn

    plan_hold: dict[str, Any] = {}
    stop_state: dict[str, Any] = {}

    async def _on_plan_limited(detail: dict[str, Any]) -> None:
        plan_hold.update(detail)

    async def _still_due() -> bool:
        if not is_unattended_quota_fallback_enabled():
            stop_state["reason"] = "switch_disabled"
            if root_job_id:
                try:
                    await _settle_disabled_wakeup_resume(
                        root_job_id=root_job_id,
                        wakeup_id=wakeup_id,
                        thread_id=thread_id,
                        owner_user_id=owner_user_id,
                    )
                except Exception as exc:
                    stop_state["db_error"] = exc
            return False
        if root_job_id:
            try:
                ok_dispatch, _, _, _ = await _check_and_enforce_resume_deadline(
                    root_job_id=root_job_id,
                    wakeup_id=wakeup_id,
                    thread_id=thread_id,
                    owner_user_id=owner_user_id,
                    now=datetime.now(timezone.utc),
                )
            except Exception as exc:
                stop_state["db_error"] = exc
                return False
            if not ok_dispatch:
                stop_state["reason"] = "deadline_or_terminal"
                return False
        try:
            if await _has_dispatched_wakeup_resume_run(
                thread_id, request_key=req_key, failed_run_id=failed_run_id
            ):
                stop_state["reason"] = "already_dispatched"
                return False
        except Exception as exc:
            stop_state["db_error"] = exc
            return False
        return True

    outcome, new_run_id = await post_notification_turn(
        thread_id=thread_id,
        body=retry_body,
        user_id=owner_user_id,
        wait_cap=120.0,
        log_prefix="[WakeupQuotaResume]",
        subject=f"wakeup_resume:{root_job_id}",
        route="retry",
        still_due=_still_due,
        busy_fails_fast=True,
        refusal_fails_fast=True,
        on_plan_limited=_on_plan_limited,
    )
    if stop_state.get("db_error") is not None:
        raise RuntimeError(
            f"wakeup_quota_resume deadline/state check failed for root_job={root_job_id}"
        ) from stop_state["db_error"]
    if outcome == "dispatched" and new_run_id:
        logger.info(
            "[UNATTENDED_FALLBACK] Resumed wakeup root_job=%s from checkpoint=%s as run=%s",
            root_job_id,
            resume_checkpoint_id,
            new_run_id,
        )
        return
    if outcome == "plan_limited":
        if plan_hold.get("type") == DALTON_WAKEUP_QUOTA_EXHAUSTED_TYPE:
            logger.warning(
                "[UNATTENDED_FALLBACK] Wakeup resume root_job=%s exhausted deferral cap (%s); settling job=%s",
                root_job_id,
                plan_hold.get("reason"),
                job_id,
            )
            return
        if plan_hold.get("held_until"):
            # Pre-dispatch cooldown hit again on resume; deferral was already accounted for in PG.
            # Reschedule this outbox job fenced by ``attempts`` so lost leases never overwrite a new claim.
            next_at = _parse_iso_or_epoch(plan_hold.get("held_until"))
            if next_at is not None and job_id:
                async with get_db_connection() as conn:
                    async with conn.transaction():
                        async with conn.cursor() as cur:
                            await cur.execute(
                                """
                                UPDATE hook_outbox
                                SET status = 'pending',
                                    lease_expires_at = NULL,
                                    next_retry_at = %s + interval '1 minute'
                                WHERE hook_outbox_id = %s::uuid
                                  AND status = 'claimed'
                                  AND attempts = %s
                                """,
                                (next_at, job_id, attempts),
                            )
                return
    if outcome == "superseded":
        if stop_state.get("reason") in ("switch_disabled", "deadline_or_terminal", "already_dispatched"):
            return
        if root_job_id and not await _has_dispatched_wakeup_resume_run(
            thread_id, request_key=req_key, failed_run_id=failed_run_id
        ):
            await mark_wakeup_deferral_settled(root_job_id)
        return
    if outcome == "drop":
        if root_job_id and not await _has_dispatched_wakeup_resume_run(
            thread_id, request_key=req_key, failed_run_id=failed_run_id
        ):
            await mark_wakeup_deferral_settled(root_job_id)
        return
    raise RuntimeError(
        f"wakeup_quota_resume for root_job={root_job_id} not dispatched (outcome={outcome})"
    )


def build_wakeup_cooldown_http_exception(decision: WakeupDeferDecision) -> HTTPException:
    """Build the structured HTTP 400 payload recognized by ``notify_turn._plan_limited``."""
    if decision.action == "defer" and decision.effective_reset_at is not None:
        return HTTPException(
            status_code=400,
            detail={
                "type": DALTON_SUBSCRIPTION_COOLDOWN_TYPE,
                "reason": "plan_limited",
                "held_until": decision.effective_reset_at.isoformat(),
                "held_known": decision.held_known,
                "root_job_id": decision.root_job_id,
                "defer_count": decision.defer_count,
            },
        )
    return HTTPException(
        status_code=400,
        detail={
            "type": DALTON_WAKEUP_QUOTA_EXHAUSTED_TYPE,
            "reason": decision.reason,
            "held_until": None,
            "root_job_id": decision.root_job_id,
            "defer_count": decision.defer_count,
        },
    )


def annotate_wakeup_request_body(
    body: dict[str, Any],
    *,
    job_id: str,
    attempts: int,
    payload: Mapping[str, Any],
    created_at: Any = None,
) -> None:
    """Stamp ``root_job_id``, ``attempts``, and ``first_due_at`` onto wakeup POST body when enabled."""
    if not is_unattended_quota_fallback_enabled():
        return
    wakeup_block = body.get("wakeup")
    if not isinstance(wakeup_block, dict):
        return
    wakeup_block["root_job_id"] = str(payload.get("root_job_id") or job_id)
    wakeup_block["job_id"] = str(job_id)
    wakeup_block["attempts"] = int(attempts)
    first_due = payload.get("first_due_at") or created_at
    if isinstance(first_due, datetime):
        wakeup_block["first_due_at"] = first_due.isoformat()
    elif isinstance(first_due, str) and first_due.strip():
        wakeup_block["first_due_at"] = first_due.strip()
