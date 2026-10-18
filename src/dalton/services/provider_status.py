"""Model provider status, live probe, and available models service (P0N.8)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
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
    family_of,
)
from src.dalton.settings import get_disabled_providers, is_broker_models_enabled

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_CONFIG_PATH = REPO_ROOT / "agent_config.dalton.yaml"

_HIGH_TOS_RISK_PROVIDERS = frozenset(
    {"claude-oauth", "codex-oauth", "antigravity-oauth", "muse-oauth"}
)

_DEFAULT_PROBE_MODELS: dict[str, str] = {
    "claude-oauth": "claude-sonnet-5-5-oauth",
    "codex-oauth": "gpt-6.1-sol-oauth",
    "antigravity-oauth": "gemini-3.8-flash-low-oauth",
    "muse-oauth": "muse-spark-1.3-oauth",
    "xai": "grok-4.3",
    "deepseek": "deepseek-flash",
    "z-ai": "glm-5.3-flash",
    "openai": "gpt-6-luna",
    "gemini": "gemini-3-flash",
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

    llm_cfg = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else {}
    lead = str(llm_cfg.get("name") or "claude-opus-5-5-oauth")
    roles.append({"role": "lead", "model": lead})

    bg = str(llm_cfg.get("background") or "deepseek-flash")
    if bg:
        roles.append({"role": "background", "model": bg})

    assist = str(llm_cfg.get("assist") or "gemini-3.8-flash-low-oauth")
    if assist:
        roles.append({"role": "assist", "model": assist})

    subagents = ((cfg.get("subagents") or {}).get("definitions")) or {}
    if isinstance(subagents, dict):
        for name, spec in subagents.items():
            m = spec.get("model") if isinstance(spec, dict) else str(spec or "")
            if m:
                roles.append({"role": f"subagent:{name}", "model": str(m)})

    from src.dalton.settings import get_dalton_settings

    for rev_m in get_dalton_settings().publish.reviewer_models:
        roles.append({"role": "reviewer_candidate", "model": str(rev_m)})

    return roles


async def _fetch_usage_stats_24h(
    model_to_provider: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Query last 24h `conversation_usages` and `dalton_llm_journal` per provider."""
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
                    WHERE created_at >= %s
                    ORDER BY created_at DESC
                    LIMIT 500
                    """,
                    (since,),
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
                        WHERE state = 'done' AND created_at >= %s
                        ORDER BY created_at DESC
                        LIMIT 500
                        """,
                        (since,),
                    )
                    jrows = await cur.fetchall()
                    for jr in jrows:
                        prov = str(jr.get("provider") or model_to_provider.get(str(jr.get("model_key") or ""), ""))
                        ts = jr.get("completed_at") or jr.get("created_at")
                        at_iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts or "")
                        _bump(prov, at_iso, str(jr.get("model_key") or ""), "journal")
                except Exception:
                    pass
    except Exception as e:
        logger.debug("[provider_status] usage stats query skipped: %s", type(e).__name__)

    return stats


async def get_providers_status(user_id: str = "local-dev-user") -> dict[str, Any]:
    """Build the `GET /api/v1/dalton/providers/status` payload (P0N.8 §3)."""
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

    # Check user BYOK keys
    byok_providers: set[str] = set()
    try:
        from src.server.database.api_keys import get_user_api_keys

        rows = await get_user_api_keys(user_id)
        if isinstance(rows, dict):
            byok_providers = {k for k, v in rows.items() if v}
        elif isinstance(rows, list):
            for r in rows:
                if isinstance(r, dict) and r.get("provider"):
                    byok_providers.add(str(r["provider"]))
    except Exception:
        pass

    # Candidate providers: core native providers + any provider referenced by roles or configured
    candidate_providers: list[str] = [
        "claude-oauth",
        "codex-oauth",
        "antigravity-oauth",
        "muse-oauth",
        "xai",
        "deepseek",
    ]
    for p in list(roles_by_provider.keys()) + list(byok_providers):
        if p and p != "openclaw-broker" and p not in candidate_providers and p in flat_providers:
            candidate_providers.append(p)

    usage_stats = await _fetch_usage_stats_24h(model_to_provider)

    providers_out: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for prov in candidate_providers:
        pinfo = flat_providers.get(prov) or {}
        parent = str(pinfo.get("parent_provider") or "")
        access_type = str(pinfo.get("access_type") or ("oauth" if prov.endswith("-oauth") else "api_key"))
        is_oauth = access_type == "oauth" or prov.endswith("-oauth")
        billing = "subscription" if is_oauth else "cash"
        enabled = prov not in disabled and (not parent or parent not in disabled)

        configured = False
        credential_info: dict[str, Any] = {}
        warnings: list[dict[str, str]] = []

        if is_oauth:
            try:
                from src.server.database.oauth_tokens import get_oauth_status

                st = await get_oauth_status(user_id, prov) or {}
                configured = bool(st.get("connected"))
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
            except Exception:
                credential_info = {"source": "oauth", "connected": False}
        else:
            env_key = str(pinfo.get("env_key") or f"{prov.upper().replace('-', '_')}_API_KEY")
            has_env = bool(env_key and os.environ.get(env_key, "").strip())
            has_byok = prov in byok_providers or (parent and parent in byok_providers)
            configured = has_env or has_byok
            credential_info = {
                "source": "env" if has_env else ("byok" if has_byok else "none"),
                "env_key": env_key,
                "has_env": has_env,
                "has_byok": has_byok,
            }
            if has_byok:
                warnings.append(
                    {
                        "code": "byok_cash_key",
                        "message": f"现金供应商 {prov} 配置了设置页 BYOK key（L19 建议放入 .env {env_key}，以便运行中实时强制执行日额度上限）。",
                    }
                )

        recent_events = await list_recent_provider_events(prov, limit=10)
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
            if str(ev.get("kind") or "").endswith("fail")
        ]
        errors_24h = await count_provider_errors_24h(prov)
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

        providers_out.append(
            {
                "provider": prov,
                "display_name": str(pinfo.get("display_name") or prov),
                "access_type": access_type,
                "billing": billing,
                "enabled": enabled,
                "configured": configured,
                "credential": credential_info,
                "last_success": last_success,
                "recent_errors": recent_errors,
                "errors_24h": errors_24h,
                "calls_24h": ust.get("calls_24h", 0),
                "used_by_roles": roles_by_provider.get(prov, []),
                "warnings": warnings,
                "tos_risk": "high" if prov in _HIGH_TOS_RISK_PROVIDERS else "none",
                "default_probe_model": _pick_probe_model(prov, provider_models.get(prov, []), meta_map),
            }
        )

    broker_enabled = is_broker_models_enabled()
    broker_health: dict[str, Any] | None = None
    if broker_enabled:
        try:
            from src.dalton.services.openclaw_admin import probe_health

            broker_health = await probe_health()
        except Exception as e:
            broker_health = {"status": "error", "error": sanitize_error_text(str(e))}

    avail_map = await explain_model_availability(user_id)
    avail_set = {k for k, v in avail_map.items() if v.get("available")}
    role_warnings = check_roles(available_models=avail_set)

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
    preferred = _DEFAULT_PROBE_MODELS.get(provider)
    if preferred and preferred in models:
        return preferred
    for m in models:
        meta = meta_map.get(m) or {}
        if not meta.get("forbidden_for_roles"):
            return m
    return models[0] if models else preferred


async def probe_provider(
    provider: str,
    *,
    user_id: str = "local-dev-user",
    model: str | None = None,
    model_key: str | None = None,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Run a user-initiated live PONG probe against `provider` (P0N.8 §4)."""
    from langchain_core.messages import HumanMessage, SystemMessage
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
        from src.server.services.llm.clients import resolve_llm_client

        client = await resolve_llm_client(selected_model, user_id=user_id)
        messages = [
            SystemMessage(content="Reply with exactly: PONG"),
            HumanMessage(content="ping"),
        ]
        resp = await asyncio.wait_for(client.ainvoke(messages), timeout=timeout_s)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        text = str(getattr(resp, "content", "") or "").strip()
        usage = getattr(resp, "usage_metadata", None) or {}
        ok = "PONG" in text.upper() or bool(text)
        await record_provider_event(
            provider=provider,
            kind="probe_ok" if ok else "probe_fail",
            user_id=user_id,
            model_key=selected_model,
            detail={
                "latency_ms": latency_ms,
                "reply_preview": sanitize_error_text(text[:40]),
            },
        )
        return {
            "ok": ok,
            "provider": provider,
            "model": selected_model,
            "latency_ms": latency_ms,
            "reply": sanitize_error_text(text[:40]),
            "usage": dict(usage) if isinstance(usage, dict) else {},
            "error_kind": None if ok else "unexpected_reply",
            "message": f"PONG ({latency_ms} ms)" if ok else f"收到非预期回复: {sanitize_error_text(text[:40])}",
        }
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        status_code = getattr(exc, "status_code", None)
        msg = sanitize_error_text(f"{type(exc).__name__}: {exc}")
        if status_code in (401, 403) or "401" in msg or "403" in msg or "unauthorized" in msg.lower():
            error_kind = f"http_{status_code or 401}"
        elif status_code == 429 or "429" in msg:
            error_kind = "http_429"
        elif isinstance(exc, asyncio.TimeoutError):
            error_kind = "timeout"
        else:
            error_kind = type(exc).__name__

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


async def get_available_models_report(user_id: str = "local-dev-user") -> dict[str, Any]:
    """Return `GET /api/v1/dalton/providers/available-models` payload (P0N.8 §5)."""
    report = await explain_model_availability(user_id)
    available = sorted(k for k, v in report.items() if v.get("available"))
    return {
        "user_id": user_id,
        "available_keys": available,
        "models": report,
    }
