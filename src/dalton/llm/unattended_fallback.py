"""Unattended-turn subscription quota fallback and pre-dispatch model selection.

Scoped to ``$0`` admin-whitelisted OAuth subscription pool fallback:
- Trusted server-side unattended source classification (never trusting client-supplied fields).
- Structured OAuth subscription availability & Redis quota cooldown probing (using the exact
  same structured terminal quota/auth rules as ``is_quota_or_auth_switch_trigger``; plain 429
  rate limits and transient ``OAuthRefreshUnavailable`` never trigger switching).
- Unified admin-whitelisted ``$0`` subscription pool (``internal_llm.allowed_models``) shared
  across pre-dispatch automation/wakeup selection, ``/internal/llm/complete``, and ``assist_json``.
- Live pre-dispatch admission re-verification: role policy (L21), whitelist & system-catalog
  isolation, latest org policy via ``policy_refuses`` + ``OWN_SUBSCRIPTIONS``, strict
  subject/payer unity, ``$0`` OAuth-only enforcement, and live subscription availability.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import HTTPException

from src.dalton.services.team_model_catalog.snapshot import (
    RouteFingerprint,
    resolve_live_route_fingerprint,
)
from src.llms.provider_errors import read_refusal
from src.server.contracts.turn_kinds import (
    UNWATCHED_RUN_META,
    classify_origin,
    posted_by_server,
    run_unwatched,
)

logger = logging.getLogger(__name__)

_HKT = timezone(timedelta(hours=8))

# Structured refusal reasons representing terminal subscription quota exhaustion or
# confirmed invalid credentials (plain 429 / ``rate_limited`` is explicitly excluded).
QUOTA_OR_AUTH_REASONS: frozenset[str] = frozenset(
    {
        "plan_limit",
        "quota_exceeded",
        "out_of_credit",
        "plan_excludes",
        "invalid_key",
    }
)

# Confirmed terminal OAuth HTTP refusal types (transient ``oauth_refresh_unavailable`` is excluded
# so temporary refresh network/lock failures remain retryable).
TERMINAL_AUTH_HTTP_TYPES: frozenset[str] = frozenset(
    {
        "oauth_reauth_required",
        "oauth_plan_no_api_access",
        "oauth_required",
    }
)


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
    """Outcome of live admission checks before selecting a ``$0`` subscription fallback model."""

    admitted: bool
    model_name: str
    reason: str | None = None
    billing: str = "oauth"
    route: RouteFingerprint | None = None


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
    non-service requests at ingress in ``messaging.py``).
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
    """Build the Redis cooldown coordination key matching ``model_resilience.quota_redis_key``."""
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
    """Probe OAuth authorization health and structured Redis quota cooldown for ``(user_id, model_name)``.

    Non-subscription models always return ``available=True``.
    Plain 429 / ``rate_limited`` / unstructured Redis entries return ``available=True``
    (matching ``is_quota_or_auth_switch_trigger``).
    """
    now_dt = now or datetime.now(timezone.utc)
    now_ts = now_dt.timestamp()
    mk = str(model_name or "").strip()
    if not mk:
        return SubscriptionAvailability(available=True, model_name="")

    sub_provider, catalog_provider, model_id, is_custom = _resolve_model_subscription_info(
        mk, prefs=prefs, ignore_custom_models=ignore_custom_models
    )
    if not sub_provider:
        return SubscriptionAvailability(
            available=True,
            provider=catalog_provider or None,
            model_name=mk,
        )
    provider = sub_provider

    # 1. Check OAuth subscription auth status via oauth_tokens / oauth_health.
    resolved_credential_id = ""
    resolved_account_id = ""
    if user_id:
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

    # 2. Compute Redis cooldown key matching model_resilience.quota_redis_key.
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

    # 3. Read structured cooldown record from Redis.
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
            reset_at = None

    # Strictly require a structured terminal quota/auth refusal reason (same judgment as
    # is_quota_or_auth_switch_trigger): plain 429 / rate_limited / unstructured records do not switch.
    if refusal_code not in QUOTA_OR_AUTH_REASONS:
        return SubscriptionAvailability(
            available=True,
            provider=provider,
            cooldown_key=cooldown_key,
            model_name=mk,
        )

    return SubscriptionAvailability(
        available=False,
        kind="quota_cooldown",
        refusal_code=refusal_code,
        provider=provider,
        reset_at=reset_at,
        cooldown_key=cooldown_key,
        message=format_unattended_quota_notice(
            "subscription_quota_exhausted", model_name=mk, reset_at=reset_at
        ),
        model_name=mk,
    )


# ---------------------------------------------------------------------------
# 3. Single admin-whitelisted $0 subscription pool resolution
# ---------------------------------------------------------------------------


def _classify_candidate_billing(
    model_name: str,
    *,
    prefs: Mapping[str, Any] | None = None,
    ignore_custom_models: bool = True,
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


def get_admin_subscription_whitelist() -> list[str]:
    """Return the ordered admin-whitelisted ``$0`` subscription pool (``internal_llm.allowed_models``)."""
    from src.dalton.settings import get_dalton_config

    raw = get_dalton_config().internal_llm.allowed_models
    seen: set[str] = set()
    ordered: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        m = item.strip()
        if m and m not in seen:
            seen.add(m)
            ordered.append(m)
    return ordered


def is_verified_system_subscription_model(model_key: str) -> bool:
    """Return True only when ``model_key`` is a built-in system-catalog ``$0`` OAuth subscription model."""
    mk = str(model_key or "").strip()
    if not mk:
        return False
    try:
        from src.llms.llm import LLM as LLMFactory

        if LLMFactory.get_model_config().get_model_config(mk) is None:
            return False
    except Exception:
        return False
    billing, _ = _classify_candidate_billing(mk, ignore_custom_models=True)
    return billing == "oauth"


def resolve_unattended_fallback_chain(
    *,
    primary_model: str | None = None,
    allowed_pool: Sequence[str] | None = None,
    skip_cooldown_keys: Iterable[str] | None = None,
    user_id: str | None = None,
    ignore_custom_models: bool = True,
) -> list[str]:
    """Resolve ordered ``$0`` OAuth subscription fallback candidates strictly from the admin whitelist.

    Preserves the admin whitelist order, filtering out ``primary_model``, non-system / non-OAuth
    models, and candidates sharing a cooled-down scope in ``skip_cooldown_keys``.
    """
    _ = ignore_custom_models  # Always system-catalog-only ($0 subscription whitelist).
    raw_pool = list(allowed_pool) if allowed_pool is not None else get_admin_subscription_whitelist()
    skip_keys = {str(k).strip() for k in (skip_cooldown_keys or ()) if str(k).strip()}
    primary_norm = str(primary_model or "").strip()

    seen_models: set[str] = set()
    result: list[str] = []

    for raw in raw_pool:
        cand = str(raw or "").strip()
        if not cand or cand == primary_norm or cand in seen_models:
            continue
        if not is_verified_system_subscription_model(cand):
            continue

        if user_id and skip_keys:
            sub_id, prov, mid, is_custom = _resolve_model_subscription_info(
                cand, ignore_custom_models=True
            )
            ckey = _compute_cooldown_redis_key_without_client(
                user_id=user_id,
                model_name=cand,
                provider=sub_id or prov,
                model_id=mid,
                is_custom=is_custom,
            )
            if ckey and any(sk == ckey or sk.startswith(f"{ckey}:") for sk in skip_keys):
                continue

        seen_models.add(cand)
        result.append(cand)

    return result


# ---------------------------------------------------------------------------
# 4. Structured switch-trigger classification
# ---------------------------------------------------------------------------


def is_terminal_auth_http_error(exc: BaseException | None) -> bool:
    """Return True only for confirmed terminal OAuth HTTP refusals (never ``oauth_refresh_unavailable``)."""
    return (
        isinstance(exc, HTTPException)
        and isinstance(exc.detail, Mapping)
        and exc.detail.get("type") in TERMINAL_AUTH_HTTP_TYPES
    )


def is_quota_or_auth_switch_trigger(exc: BaseException | None) -> bool:
    """Return True ONLY for confirmed terminal quota refusals or terminal OAuth auth failures.

    Transient network errors, timeouts, generic 429 rate limits, 5xx errors, and transient
    ``OAuthRefreshUnavailable`` / ``oauth_refresh_unavailable`` return ``False``.
    """
    if exc is None:
        return False

    from src.ptc_agent.agent.middleware.model_resilience import _RemoteQuotaExhaustedError
    from src.server.services.oauth_health import OAuthReauthRequired, OAuthRefreshUnavailable
    from src.server.services.oauth_protocol import OAuthPlanNoApiAccess

    # Transient OAuth token refresh errors must remain retryable, never terminal auth failure.
    if isinstance(exc, OAuthRefreshUnavailable):
        return False
    if isinstance(exc, HTTPException) and isinstance(exc.detail, Mapping):
        dtype = exc.detail.get("type")
        if dtype == "oauth_refresh_unavailable":
            return False
        if dtype in TERMINAL_AUTH_HTTP_TYPES:
            return True

    if isinstance(exc, _RemoteQuotaExhaustedError):
        refusal = read_refusal(exc)
        return refusal is not None and refusal.reason in QUOTA_OR_AUTH_REASONS

    if isinstance(exc, (OAuthReauthRequired, OAuthPlanNoApiAccess)):
        return True

    refusal = read_refusal(exc)
    if refusal is not None and refusal.reason in QUOTA_OR_AUTH_REASONS:
        return True

    # Explicit 401/402 HTTP status after auth failed
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in (401, 402):
        return True

    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, BaseException) and cause is not exc:
        if is_quota_or_auth_switch_trigger(cause):
            return True
    return False


def is_complete_result_quota_or_auth_trigger(result: Any) -> bool:
    """Return True ONLY when a ``DaltonCompleteResult`` carries structured terminal quota/auth metadata."""
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


# ---------------------------------------------------------------------------
# 5. Pre-dispatch live admission re-verification
# ---------------------------------------------------------------------------


def _policy_allows_model(
    policy: Any,
    model_name: str,
    billing: str,
    *,
    model_pref: Mapping[str, Any] | None = None,
    ignore_custom_models: bool = True,
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

    allows_model_fn = getattr(policy, "allows_model", None)
    if callable(allows_model_fn) and not allows_model_fn(model_name):
        return False, "policy_denied:models.allowed"
    if billing == "oauth":
        allows_fn = getattr(policy, "allows", None)
        if callable(allows_fn) and not allows_fn(OWN_SUBSCRIPTIONS):
            return False, f"policy_denied:{OWN_SUBSCRIPTIONS}"
    return True, None


def route_fingerprint_to_dict(route: RouteFingerprint) -> dict[str, Any]:
    """Serialize a 6-tuple :class:`RouteFingerprint` for run metadata / snapshot recording."""
    return {
        "model_key": route.model_key,
        "provider": route.provider,
        "model_id": route.model_id,
        "bound_base_url": route.bound_base_url,
        "key_fingerprint": route.key_fingerprint,
        "billing": route.billing,
    }


async def acquire_fallback_candidate_live(
    *,
    subject_user_id: str,
    candidate_model: str,
    payer_user_id: str | None = None,
    allowed_pool: Sequence[str] | None = None,
    ignore_custom_models: bool = True,
    policy: Any = None,
    model_pref: Mapping[str, Any] | None = None,
    conn: Any = None,
    require_policy_fn: Any = None,
    availability_fn: Any = None,
    resolve_route_fn: Any = None,
) -> FallbackAdmissionResult:
    """Re-verify a ``$0`` admin-whitelisted subscription candidate before pre-dispatch selection.

    Gates:
    1. Role policy forbidden variants (L21), admin whitelist membership, and system-catalog ``$0``
       OAuth subscription verification.
    2. Fresh org policy via canonical ``policy_refuses`` + ``OWN_SUBSCRIPTIONS``.
    3. Strict subject/payer identity unity and ``$0`` OAuth-only enforcement.
    4. Live subscription availability (OAuth auth health + structured Redis cooldown) and live
       ``RouteFingerprint`` resolution for run snapshot recording.
    """
    _ = ignore_custom_models  # Always enforce system-catalog-only ($0 subscription whitelist).
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

    # Gate 1b: Admin whitelist membership & verified system subscription model
    pool = list(allowed_pool) if allowed_pool is not None else get_admin_subscription_whitelist()
    if mk not in pool:
        return FallbackAdmissionResult(
            admitted=False, model_name=mk, reason="not_in_subscription_whitelist"
        )
    if not is_verified_system_subscription_model(mk):
        return FallbackAdmissionResult(
            admitted=False, model_name=mk, reason="non_subscription_fallback_disabled"
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

    prefs = (
        {
            k: v
            for k, v in model_pref.items()
            if k not in ("custom_models", "custom_providers")
        }
        if isinstance(model_pref, Mapping)
        else {}
    )

    live_route: RouteFingerprint | None = None
    try:
        _resolve_route = resolve_route_fn or resolve_live_route_fingerprint
        live_route = await _resolve_route(
            mk,
            user_id=subj,
            model_pref=prefs or None,
            ignore_custom_models=True,
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

    billing, _ = _classify_candidate_billing(mk, prefs=prefs, ignore_custom_models=True)
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
        ignore_custom_models=True,
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
            reason="non_subscription_fallback_disabled",
            billing=billing,
            route=live_route,
        )

    # Gate 4: Live OAuth subscription availability probe
    _avail_fn = availability_fn or inspect_subscription_availability
    avail = await _avail_fn(
        subj,
        mk,
        prefs=prefs or None,
        ignore_custom_models=True,
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


# ---------------------------------------------------------------------------
# 6. User-visible notice formatting
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
