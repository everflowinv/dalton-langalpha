"""Model provider status, live probe, and available models service (P0N.8)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from src.dalton.database.provider_events import (
    count_provider_errors_24h,
    get_last_refresh_event,
    list_recent_provider_events,
    record_provider_event,
    sanitize_error_text,
)
from src.dalton.llm.overlay import load_dalton_meta
from src.dalton.llm.providers import ensure_provider_enabled
from src.dalton.llm.role_policy import (
    check_roles,
    explain_model_availability,
)
from src.dalton.settings import get_disabled_providers, is_broker_models_enabled

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_CONFIG_PATH = REPO_ROOT / "agent_config.dalton.yaml"

_HIGH_TOS_RISK_PROVIDERS = frozenset(
    {"claude-oauth", "codex-oauth", "antigravity-oauth", "muse-oauth", "xai-oauth"}
)

_PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "z-ai-cn-coding": "智谱 GLM（Coding Plan）",
}


def _load_role_model_assignments() -> list[dict[str, str]]:
    """Read role -> model assignments from `agent_config.dalton.yaml` and `dalton.yaml`."""
    roles: list[dict[str, str]] = []
    cfg: dict[str, Any] = {}
    if AGENT_CONFIG_PATH.exists():
        try:
            cfg = yaml.safe_load(AGENT_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            cfg = {}

    from src.dalton.settings import get_dalton_settings

    settings = get_dalton_settings()

    llm_cfg = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else {}
    lead = str(llm_cfg.get("name") or "").strip()
    if lead:
        roles.append({"role": "lead", "model": lead})

    bg = str(llm_cfg.get("background") or "").strip()
    if bg:
        roles.append({"role": "background", "model": bg})

    assist = str(llm_cfg.get("assist") or settings.models.assist_model or "").strip()
    if assist:
        roles.append({"role": "assist", "model": assist})

    compaction = str(llm_cfg.get("compaction") or llm_cfg.get("summarization") or "").strip()
    if compaction:
        roles.append({"role": "compaction", "model": compaction})

    fetch_m = str(llm_cfg.get("fetch") or "").strip()
    if fetch_m:
        roles.append({"role": "fetch", "model": fetch_m})

    subagents = ((cfg.get("subagents") or {}).get("definitions")) or {}
    if isinstance(subagents, dict):
        for name, spec in subagents.items():
            m = spec.get("model") if isinstance(spec, dict) else str(spec or "")
            if m:
                roles.append({"role": f"subagent:{name}", "model": str(m)})

    for rev_m in settings.publish.reviewer_models:
        roles.append({"role": "reviewer_candidate", "model": str(rev_m)})

    extractor_m = str(settings.conversations.extractor_model or "").strip()
    if extractor_m:
        roles.append({"role": "conversations.extractor_model", "model": extractor_m})

    return roles


async def _fetch_usage_stats_24h(
    model_to_provider: dict[str, str],
    *,
    user_id: str,
) -> dict[str, dict[str, Any]]:
    """Query last 24h `conversation_usages` and `dalton_llm_journal` per provider for `user_id`."""
    if not user_id or not str(user_id).strip():
        raise ValueError("user_id is required")
    user_id = str(user_id).strip()
    stats: dict[str, dict[str, Any]] = {}
    since = datetime.now(timezone.utc) - timedelta(hours=24)

    def _bump(prov: str, at_iso: str, model: str, source: str) -> None:
        if not prov:
            return
        entry = stats.setdefault(prov, {"calls_24h": 0, "last_success": None})
        entry["calls_24h"] += 1
        prev = entry["last_success"]
        if prev is None or str(at_iso) > str(prev.get("at") or ""):
            entry["last_success"] = {"at": at_iso, "model": model, "source": source}

    try:
        from psycopg.rows import dict_row
        from src.server.database.pool import get_db_connection

        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT created_at, token_usage
                    FROM conversation_usages
                    WHERE user_id = %s AND created_at >= %s
                    ORDER BY created_at DESC
                    LIMIT 500
                    """,
                    (user_id, since),
                )
                rows = await cur.fetchall()
                for row in rows:
                    tu = row.get("token_usage") or {}
                    if isinstance(tu, dict):
                        per_call = tu.get("per_call_costs")
                        if isinstance(per_call, list):
                            at_iso = (
                                row["created_at"].isoformat()
                                if hasattr(row.get("created_at"), "isoformat")
                                else str(row.get("created_at") or "")
                            )
                            for c in per_call:
                                if isinstance(c, dict):
                                    mname = str(c.get("model_name") or c.get("model") or "")
                                    prov = model_to_provider.get(mname, "")
                                    if prov:
                                        _bump(prov, at_iso, mname, "usage")

                try:
                    await cur.execute(
                        """
                        SELECT provider, model_key, completed_at, created_at
                        FROM dalton_llm_journal
                        WHERE user_id = %s AND state = 'done' AND created_at >= %s
                        ORDER BY created_at DESC
                        LIMIT 500
                        """,
                        (user_id, since),
                    )
                    jrows = await cur.fetchall()
                    for jr in jrows:
                        prov = str(jr.get("provider") or model_to_provider.get(str(jr.get("model_key") or ""), ""))
                        ts = jr.get("completed_at") or jr.get("created_at")
                        at_iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts or "")
                        _bump(prov, at_iso, str(jr.get("model_key") or ""), "journal")
                except Exception as jexc:
                    logger.debug(
                        "[provider_status] journal stats query skipped: %s",
                        type(jexc).__name__,
                    )
    except Exception as e:
        logger.debug("[provider_status] usage stats query skipped: %s", type(e).__name__)

    return stats


async def get_providers_status(*, user_id: str) -> dict[str, Any]:
    """Build the `GET /api/v1/dalton/providers/status` payload (P0N.8 §3)."""
    if not user_id or not str(user_id).strip():
        raise ValueError("user_id is required")
    user_id = str(user_id).strip()
    from src.llms.llm import ModelConfig

    mc = ModelConfig()
    llm_cfg = mc.llm_config or {}
    flat_providers = mc.flat_providers or {}
    disabled = get_disabled_providers()
    meta_map = load_dalton_meta()

    model_to_provider: dict[str, str] = {}
    provider_models: dict[str, list[str]] = {}
    for k, m in llm_cfg.items():
        if isinstance(m, dict):
            p = str(m.get("provider") or "")
            model_to_provider[k] = p
            provider_models.setdefault(p, []).append(k)

    role_assignments = _load_role_model_assignments()
    roles_by_provider: dict[str, list[dict[str, str]]] = {}
    for item in role_assignments:
        prov = model_to_provider.get(item["model"], "")
        if prov and prov != "openclaw-broker":
            roles_by_provider.setdefault(prov, []).append(item)

    # Check user BYOK keys (get_user_api_keys returns {"byok_enabled", "keys": {prov: key}, "base_urls"})
    byok_providers: set[str] = set()
    try:
        from src.server.database.api_keys import get_user_api_keys

        rows = await get_user_api_keys(user_id)
        if isinstance(rows, dict):
            keys_map = rows.get("keys")
            if isinstance(keys_map, dict):
                byok_providers = {str(p) for p, v in keys_map.items() if v}
            else:
                byok_providers = {
                    str(k)
                    for k, v in rows.items()
                    if k not in ("byok_enabled", "keys", "base_urls") and v
                }
        elif isinstance(rows, list):
            for r in rows:
                if isinstance(r, dict) and r.get("provider"):
                    byok_providers.add(str(r["provider"]))
    except Exception as exc:
        logger.debug(
            "[DaltonProviderStatus] get_user_api_keys failed for user_id=%s: %s",
            user_id,
            type(exc).__name__,
        )

    # Candidate providers: core native providers + any provider referenced by roles or configured
    candidate_providers: list[str] = [
        "claude-oauth",
        "codex-oauth",
        "antigravity-oauth",
        "muse-oauth",
        "xai-oauth",
        "z-ai-cn-coding",
        "xai",
        "deepseek",
    ]
    for p in list(roles_by_provider.keys()) + list(byok_providers):
        if p and p != "openclaw-broker" and p not in candidate_providers and p in flat_providers:
            candidate_providers.append(p)

    usage_stats = await _fetch_usage_stats_24h(model_to_provider, user_id=user_id)

    providers_out: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for prov in candidate_providers:
        pinfo = flat_providers.get(prov) or {}
        parent = str(pinfo.get("parent_provider") or "")
        access_type = str(pinfo.get("access_type") or ("oauth" if prov.endswith("-oauth") else "api_key"))
        is_oauth = access_type == "oauth" or prov.endswith("-oauth")
        is_subscription = is_oauth or access_type == "coding_plan" or prov == "z-ai-cn-coding"
        billing = "subscription" if is_subscription else "cash"
        enabled = prov not in disabled and (not parent or parent not in disabled)

        configured = False
        credential_info: dict[str, Any] = {}
        quota_info: dict[str, Any] | None = None
        warnings: list[dict[str, str]] = []
        muse_row_extras: dict[str, Any] = {}

        if is_oauth:
            try:
                # Unified projection: a connection is configured when its state
                # says connected, not merely because a token row exists.
                from src.server.services.oauth_health import connection_state
                from src.server.database.oauth_tokens import get_oauth_connection

                st = (await get_oauth_connection(user_id, prov)) or {}
                configured = connection_state(st) == "connected"
                expires_at = st.get("expires_at")
                seconds_left: int | None = None
                expires_iso: str | None = None
                if isinstance(expires_at, datetime):
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                    expires_iso = expires_at.isoformat()
                    seconds_left = int((expires_at - now).total_seconds())
                elif isinstance(expires_at, str) and expires_at:
                    expires_iso = expires_at
                last_refresh = await get_last_refresh_event(prov, user_id=user_id)
                credential_info = {
                    "source": "oauth",
                    "email": st.get("email"),
                    "plan_type": st.get("plan_type"),
                    "expires_at": expires_iso,
                    "seconds_left": seconds_left,
                    "last_refresh": last_refresh,
                }
                if prov == "xai-oauth" and configured:
                    try:
                        # The unified refresh protocol, not the removed
                        # per-provider refresher: refreshes under the shared
                        # lock and saves conditionally, so a status-page read
                        # cannot race a disconnect or reconnect (DALTON-HOOK(oauth-dispatch)).
                        from src.dalton.llm.providers.xai_grok.oauth import fetch_xai_billing
                        from src.server.services.oauth_health import (
                            OAuthReauthRequired,
                            OAuthRefreshUnavailable,
                            valid_token,
                        )

                        try:
                            valid_tok = await valid_token(user_id, prov)
                        except (OAuthReauthRequired, OAuthRefreshUnavailable):
                            valid_tok = None
                        access_tok = valid_tok.access_token if valid_tok else None
                        if access_tok:
                            quota_info = await fetch_xai_billing(str(access_tok))
                            credential_info["quota"] = quota_info
                            if quota_info.get("token_expired"):
                                credential_info["token_expired"] = True
                                warnings.append(
                                    {
                                        "code": "oauth_token_expired",
                                        "message": "xAI OAuth 令牌已过期（401/403），请重新连接。",
                                    }
                                )
                        else:
                            credential_info["token_expired"] = True
                            warnings.append(
                                {
                                    "code": "oauth_token_expired",
                                    "message": "xAI OAuth 令牌刷新失败或已过期，请重新连接。",
                                }
                            )
                    except Exception as qexc:
                        logger.debug(
                            "[provider_status] xai-oauth billing query skipped: %s",
                            type(qexc).__name__,
                        )
                if prov == "muse-oauth":
                    from src.dalton.credentials import credential_user_id
                    from src.dalton.llm.providers.muse.oauth import (
                        get_muse_cached_meta,
                        get_oauth_tokens as get_muse_oauth_tokens,
                        is_invalidated_muse_token,
                        is_minted_muse_token,
                    )

                    muse_meta = await get_muse_cached_meta(user_id)
                    row_check: dict[str, Any] = dict(st)
                    if "account_id" not in row_check:
                        try:
                            tok_row = await get_muse_oauth_tokens(credential_user_id(user_id), prov)
                            if isinstance(tok_row, dict):
                                row_check = tok_row
                                configured = True
                        except Exception:
                            pass

                    if configured and (
                        is_invalidated_muse_token(row_check)
                        or ("account_id" in row_check and not is_minted_muse_token(row_check))
                    ):
                        configured = False
                        credential_info["reconnect_required"] = True
                        credential_info["status_message"] = "请重新连接 Muse"
                        warnings.append(
                            {
                                "code": "muse_reconnect_required",
                                "message": (
                                    "Muse 凭据已失效（401 invalid_api_key），请重新连接 Muse。"
                                    if is_invalidated_muse_token(row_check)
                                    else "Muse 凭据已失效或为未换发 API key 的旧版 token，请重新连接 Muse。"
                                ),
                            }
                        )

                    is_subs_active = (
                        muse_meta.get("is_subs_active") if isinstance(muse_meta, dict) else None
                    )
                    subs_tier_name = (
                        (muse_meta.get("subs_tier_name") if isinstance(muse_meta, dict) else None)
                        or credential_info.get("plan_type")
                    )
                    raw_usage = (
                        muse_meta.get("subs_usage")
                        if isinstance(muse_meta, dict) and isinstance(muse_meta.get("subs_usage"), dict)
                        else {}
                    )
                    win_u = raw_usage.get("window") if isinstance(raw_usage.get("window"), dict) else {}
                    wk_u = raw_usage.get("weekly") if isinstance(raw_usage.get("weekly"), dict) else {}
                    subs_usage: dict[str, Any] = (
                        {
                            "window": {
                                "used_percent": win_u.get("used_percent"),
                                "window_duration_mins": win_u.get("window_duration_mins"),
                                "resets_at": win_u.get("resets_at"),
                            },
                            "weekly": {
                                "used_percent": wk_u.get("used_percent"),
                                "resets_at": wk_u.get("resets_at"),
                            },
                            "tier": raw_usage.get("tier"),
                        }
                        if raw_usage
                        else {}
                    )
                    credential_info["is_subs_active"] = is_subs_active
                    credential_info["subs_tier_name"] = subs_tier_name
                    credential_info["subs_usage"] = subs_usage
                    if isinstance(muse_meta, dict) and muse_meta.get("base_url"):
                        credential_info["base_url"] = muse_meta.get("base_url")
                    muse_row_extras = {
                        "is_subs_active": is_subs_active,
                        "subs_tier_name": subs_tier_name,
                        "subs_usage": subs_usage,
                    }
            except Exception:
                credential_info = {"source": "oauth", "connected": False}
        else:
            env_key = str(pinfo.get("env_key") or f"{prov.upper().replace('-', '_')}_API_KEY")
            has_env = bool(env_key and os.environ.get(env_key, "").strip())
            if access_type == "coding_plan" or prov == "z-ai-cn-coding":
                has_byok = prov in byok_providers
            else:
                has_byok = prov in byok_providers or (parent and parent in byok_providers)
            configured = has_env or has_byok
            credential_info = {
                "source": "env" if has_env else ("byok" if has_byok else "none"),
                "env_key": env_key,
                "has_env": has_env,
                "has_byok": has_byok,
            }
            if access_type == "coding_plan" or prov == "z-ai-cn-coding":
                credential_info["billing_mode"] = "subscription"
                if configured:
                    credential_info["status_text"] = (
                        f".env ({env_key}) 已配置（订阅制）"
                        if has_env
                        else "BYOK 已配置（订阅制）"
                    )
                    credential_info["hint"] = "已配置（订阅制，包月不占现金额度）"
                else:
                    credential_info["status_text"] = "未配置"
                    credential_info["hint"] = f"在 .env 配置 {env_key} 后重建后端"
            # With the .env key present the BYOK copy is redundant, not a risk: say nothing.
            if has_byok and not has_env and billing == "cash":
                warnings.append(
                    {
                        "code": "byok_cash_key",
                        "message": f"现金供应商 {prov} 的 key 只配置在 BYOK 中；建议放入 .env {env_key}，运行中才能实时执行日额度上限。",
                    }
                )

        recent_events = await list_recent_provider_events(prov, user_id=user_id, limit=10)
        recent_errors = [
            {
                "at": ev.get("created_at"),
                "kind": (ev.get("detail") or {}).get("error_kind") or ev.get("kind"),
                "model": ev.get("model_key"),
                "message": sanitize_error_text(
                    str((ev.get("detail") or {}).get("message") or (ev.get("detail") or {}).get("error") or "")
                ),
            }
            for ev in recent_events
            if str(ev.get("kind") or "").endswith("fail") or ev.get("kind") == "auth_expired"
        ]
        errors_24h = await count_provider_errors_24h(prov, user_id=user_id)
        ust = usage_stats.get(prov) or {"calls_24h": 0, "last_success": None}

        # Also check latest probe_ok event if no usage last_success
        last_success = ust.get("last_success")
        for ev in recent_events:
            if ev.get("kind") == "probe_ok":
                ev_at = str(ev.get("created_at") or "")
                if last_success is None or ev_at > str(last_success.get("at") or ""):
                    last_success = {
                        "at": ev_at,
                        "model": ev.get("model_key"),
                        "source": "probe",
                    }
                break

        row_payload: dict[str, Any] = {
            "provider": prov,
            "display_name": _PROVIDER_DISPLAY_NAMES.get(prov) or str(pinfo.get("display_name") or prov),
            "access_type": access_type,
            "billing": billing,
            "enabled": enabled,
            "configured": configured,
            "credential": credential_info,
            "quota": quota_info,
            "last_success": last_success,
            "recent_errors": recent_errors,
            "errors_24h": errors_24h,
            "calls_24h": ust.get("calls_24h", 0),
            "used_by_roles": roles_by_provider.get(prov, []),
            "warnings": warnings,
            "tos_risk": "high" if prov in _HIGH_TOS_RISK_PROVIDERS else "none",
            "default_probe_model": _pick_probe_model(prov, provider_models.get(prov, []), meta_map),
        }
        if muse_row_extras:
            row_payload.update(muse_row_extras)
        providers_out.append(row_payload)

    broker_enabled = is_broker_models_enabled()
    broker_health: dict[str, Any] | None = None
    if broker_enabled:
        try:
            from src.dalton.services.openclaw_admin import probe_health

            broker_health = await probe_health()
        except Exception as e:
            broker_health = {"status": "error", "error": sanitize_error_text(str(e))}

    avail_map = await explain_model_availability(user_id=user_id)
    avail_set = {k for k, v in avail_map.items() if v.get("available")}
    from src.dalton.llm.model_prefs import (
        resolve_dalton_assist_fallback_models,
        resolve_dalton_assist_model,
        resolve_dalton_reviewer_models,
    )

    assist_model = await resolve_dalton_assist_model(user_id)
    assist_fallback_models = await resolve_dalton_assist_fallback_models(user_id)
    reviewer_models = await resolve_dalton_reviewer_models(user_id)
    role_warnings = check_roles(
        assist_model=assist_model,
        assist_fallback_models=assist_fallback_models,
        reviewer_models=reviewer_models,
        available_models=avail_set,
    )

    return {
        "generated_at": now.isoformat(),
        "providers": providers_out,
        "role_warnings": role_warnings,
        "broker": {
            "enabled": broker_enabled,
            "health": broker_health,
        },
    }


def _pick_probe_model(
    provider: str,
    models: list[str],
    meta_map: dict[str, dict[str, Any]],
) -> str | None:
    from src.dalton.settings import get_dalton_settings

    probe_models = get_dalton_settings().models.probe_models or {}
    preferred = probe_models.get(provider)
    if preferred and preferred in models:
        return preferred
    for m in models:
        meta = meta_map.get(m) or {}
        if not meta.get("forbidden_for_roles"):
            return m
    return models[0] if models else preferred


class _ProbeLLMService:
    """Adapter allowing `dalton_complete` to execute a PONG probe via a resolved model client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def complete(
        self,
        *,
        user_id: str | None = None,
        user_prompt: str = "",
        system_prompt: str | None = None,
        response_schema: Any = None,
        slot: str = "background",
        request_model: str | None = None,
        is_byok: bool = True,
        reasoning_effort: str | None = None,
        disable_tracing: bool = True,
        return_token_usage: bool = False,
    ) -> Any:
        from langchain_core.messages import HumanMessage, SystemMessage
        from src.dalton.llm.complete import _unwrap_content_blocks

        messages = [
            SystemMessage(content=system_prompt or "Reply with exactly: PONG"),
            HumanMessage(content=user_prompt or "ping"),
        ]
        resp = await self._client.ainvoke(messages)
        text = _unwrap_content_blocks(getattr(resp, "content", "")).strip()
        raw_usage = getattr(resp, "usage_metadata", None) or {}
        usage_dict = dict(raw_usage) if isinstance(raw_usage, dict) else {}
        if return_token_usage:
            return text, usage_dict
        return text


async def probe_provider(
    provider: str,
    *,
    user_id: str,
    model: str | None = None,
    model_key: str | None = None,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Run a user-initiated live PONG probe against `provider` via `dalton_complete` (P0N.8 §4, P0H §4 P1/P2)."""
    if not user_id or not str(user_id).strip():
        raise ValueError("user_id is required")
    user_id = str(user_id).strip()
    from src.llms.llm import ModelConfig

    mc = ModelConfig()
    llm_cfg = mc.llm_config or {}
    meta_map = load_dalton_meta()
    prov_models = [
        k for k, v in llm_cfg.items() if isinstance(v, dict) and v.get("provider") == provider
    ]
    selected_model = model or model_key or _pick_probe_model(provider, prov_models, meta_map)
    if not selected_model:
        return {
            "ok": False,
            "provider": provider,
            "model": None,
            "latency_ms": 0,
            "error_kind": "model_not_found",
            "message": f"供应商 {provider} 下没有可探测的模型。",
        }

    t0 = time.perf_counter()
    try:
        ensure_provider_enabled(provider)
        from src.dalton.llm.complete import dalton_complete
        from src.server.services.llm.clients import resolve_model_client

        resolved = await resolve_model_client(
            user_id,
            selected_model,
            is_byok=True,
            allow_platform_fallback=True,
        )
        if resolved.client is None:
            raise RuntimeError(f"供应商 {provider} ({selected_model}) 凭据未配置或未连接。")

        probe_scope = f"probe:{provider}:{uuid.uuid4().hex[:12]}"
        comp = await dalton_complete(
            llm_service=_ProbeLLMService(resolved.client),
            user_id=user_id,
            system_prompt="Reply with exactly: PONG",
            user_prompt="ping",
            scope_ref=probe_scope,
            purpose="provider_probe",
            request_model=selected_model,
            slot="background",
            timeout_s=timeout_s,
            raise_on_error=True,
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        text = str(comp.text or "").strip()
        usage = dict(comp.usage) if isinstance(comp.usage, dict) else {}
        ok = "PONG" in text.upper()
        await record_provider_event(
            provider=provider,
            kind="probe_ok" if ok else "probe_fail",
            user_id=user_id,
            model_key=selected_model,
            detail={
                "latency_ms": latency_ms,
                "reply_preview": sanitize_error_text(text[:40]),
                "cash_usd": comp.cash_usd,
                "notional_usd": comp.notional_usd,
            },
        )
        return {
            "ok": ok,
            "provider": provider,
            "model": selected_model,
            "latency_ms": latency_ms,
            "reply": sanitize_error_text(text[:40]),
            "usage": usage,
            "cash_usd": comp.cash_usd,
            "notional_usd": comp.notional_usd,
            "error_kind": None if ok else "unexpected_reply",
            "message": f"PONG ({latency_ms} ms)" if ok else f"收到非预期回复: {sanitize_error_text(text[:40])}",
        }
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        status_code = getattr(exc, "status_code", None)
        exc_error_kind = getattr(exc, "error_kind", None)
        msg = sanitize_error_text(f"{type(exc).__name__}: {exc}")
        msg_lower = msg.lower()
        from src.dalton.llm.providers.muse.client import is_muse_auth_expired_error

        if provider == "muse-oauth" and is_muse_auth_expired_error(exc):
            error_kind = "auth_expired"
            if "请重新连接 Muse" not in msg:
                msg = f"{msg}（请重新连接 Muse）"
        elif status_code in (401, 403) or "401" in msg or "403" in msg or "unauthorized" in msg_lower:
            error_kind = f"http_{status_code or 401}"
        elif status_code == 429 or "429" in msg:
            error_kind = "http_429"
        elif isinstance(exc, asyncio.TimeoutError):
            error_kind = "timeout"
        else:
            error_kind = exc_error_kind or type(exc).__name__

        await record_provider_event(
            provider=provider,
            kind="probe_fail",
            user_id=user_id,
            model_key=selected_model,
            detail={
                "latency_ms": latency_ms,
                "error_kind": error_kind,
                "status_code": status_code,
                "message": msg,
            },
        )
        return {
            "ok": False,
            "provider": provider,
            "model": selected_model,
            "latency_ms": latency_ms,
            "usage": {},
            "error_kind": error_kind,
            "message": msg,
        }


async def get_available_models_report(*, user_id: str) -> dict[str, Any]:
    """Return `GET /api/v1/dalton/providers/available-models` payload (P0N.8 §5)."""
    if not user_id or not str(user_id).strip():
        raise ValueError("user_id is required")
    user_id = str(user_id).strip()
    report = await explain_model_availability(user_id=user_id)
    available = sorted(k for k, v in report.items() if v.get("available"))
    return {
        "user_id": user_id,
        "available_keys": available,
        "models": report,
    }
