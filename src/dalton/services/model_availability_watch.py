"""Daily subscription model availability watch and Codex version probe service.

Checks each connected subscription provider via
``src.server.services.llm.provider_models.service.available_models(user_id, provider, refresh=True)``
under the team credential holder (``DALTON_PM_USER_IDS[0]`` via ``credential_user_id``),
flags any visible configured model (manifest + overlay) that appears in ``missing``,
and optionally probes Codex with the latest ``@openai/codex`` npm version when newer
than ``get_codex_client_version()``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from src.dalton.credentials import credential_user_id, get_configured_pm_user_ids
from src.dalton.database.provider_events import sanitize_error_text
from src.dalton.llm import native_catalog
from src.dalton.llm.native_catalog import NATIVE_LISTING_PROVIDERS
from src.dalton.llm.overlay import load_dalton_meta
from src.dalton.settings import get_codex_client_version
from src.llms.llm import LLM
from src.server.services.llm import egress
from src.server.services.llm.provider_models import matching as pm_matching
from src.server.services.llm.provider_models import service as pm_service
from src.server.services.llm.provider_models import wire as pm_wire

logger = logging.getLogger(__name__)

CORE_SUBSCRIPTION_PROVIDERS: tuple[str, ...] = (
    "codex-oauth",
    "claude-oauth",
    "antigravity-oauth",
    "xai-oauth",
)

# Providers whose upstream listing protocol is supported by availability watch.
# Codex and Claude use ``provider_models.service.available_models`` (``provider_models.wire``);
# Antigravity uses ``native_catalog.fetch_antigravity_models`` (``v1internal:fetchAvailableModels``).
# xAI (CLI proxy headers) and UNSUPPORTED_PROVIDERS stay excluded until supported.
SUPPORTED_AVAILABILITY_PROVIDERS: frozenset[str] = frozenset(
    {
        "codex-oauth",
        "claude-oauth",
        "antigravity-oauth",
    }
)

NPM_CODEX_LATEST_URL = "https://registry.npmjs.org/@openai/codex/latest"
NPM_FETCH_TIMEOUT_S = 10.0

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

_NON_BUILDABLE_OAUTH_REASONS = frozenset(
    {
        pm_service.NEEDS_REAUTH,
        pm_service.REFRESH_UNAVAILABLE,
        pm_service.NO_API_ACCESS,
        pm_wire.UNAUTHORIZED,
        pm_wire.ENDPOINT_REFUSED,
    }
)


def iter_subscription_providers() -> tuple[str, ...]:
    """Return deduplicated subscription providers in canonical check order."""
    ordered = list(CORE_SUBSCRIPTION_PROVIDERS)
    for provider in NATIVE_LISTING_PROVIDERS:
        if provider not in ordered:
            ordered.append(provider)
    return tuple(ordered)


def resolve_credential_holder_user_id() -> str:
    """Resolve the team credential holder user_id (``dalton_credential_holder`` in ``local`` mode, ``DALTON_PM_USER_IDS[0]`` otherwise)."""
    from src.dalton.access_tiers import get_primary_admin_user_id_sync
    from src.dalton.settings import is_dalton_local_auth_enabled

    if is_dalton_local_auth_enabled():
        primary = get_primary_admin_user_id_sync()
        if not primary:
            raise RuntimeError("No local administrator or credential holder is configured; cannot resolve team credential holder")
        holder = credential_user_id(primary)
    else:
        pm_ids = get_configured_pm_user_ids()
        if not pm_ids:
            raise RuntimeError("DALTON_PM_USER_IDS is not configured; cannot resolve team credential holder")
        holder = credential_user_id(pm_ids[0])
    if not holder:
        raise RuntimeError("Failed to resolve team credential holder user_id")
    return holder


def parse_semver(raw: str | None) -> tuple[int, int, int] | None:
    """Parse strict ``X.Y.Z`` (optional ``v`` prefix) version string into an integer tuple."""
    if not isinstance(raw, str):
        return None
    match = _SEMVER_RE.match(raw.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _is_visible_provider_model(mc: Any, model_key: str, provider: str) -> bool:
    entry = (getattr(mc, "llm_config", None) or {}).get(model_key)
    return bool(
        isinstance(entry, dict)
        and entry.get("visible")
        and entry.get("provider") == provider
    )


def _is_awaiting_release(dalton_meta: dict[str, dict[str, Any]], model_key: str) -> bool:
    return bool((dalton_meta.get(model_key) or {}).get("awaiting_release"))


def _awaiting_release_prefix(model_id: str) -> str:
    """Strip the trailing ``-<tier>`` segment from an ``awaiting_release`` model_id."""
    raw = str(model_id or "").strip()
    if not raw or "-" not in raw:
        return raw
    base, suffix = raw.rsplit("-", 1)
    if base and suffix and not suffix[:1].isdigit():
        return base
    return raw


def _provider_awaiting_prefixes(
    mc: Any,
    dalton_meta: dict[str, dict[str, Any]],
    provider: str,
) -> list[str]:
    prefixes: set[str] = set()
    for key, entry in (getattr(mc, "llm_config", None) or {}).items():
        if not isinstance(entry, dict) or entry.get("provider") != provider:
            continue
        # Awaiting-release models stay hidden (visible: false) until the subscription
        # serves them, so the prefix watch covers them regardless of visibility.
        if not _is_awaiting_release(dalton_meta, str(key)):
            continue
        prefix = _awaiting_release_prefix(str(entry.get("model_id") or key))
        if prefix:
            prefixes.add(prefix)
    return sorted(prefixes)


def _provider_configured_model_ids(mc: Any, provider: str) -> set[str]:
    ids: set[str] = set()
    for key, entry in (getattr(mc, "llm_config", None) or {}).items():
        if not isinstance(entry, dict) or entry.get("provider") != provider:
            continue
        mid = str(entry.get("model_id") or key).strip()
        if mid:
            ids.add(mid)
    return ids


async def _fetch_antigravity_available_models(
    user_id: str,
    mc: Any,
) -> pm_matching.ModelsResult:
    """Fetch ``antigravity-oauth`` models via ``native_catalog.fetch_antigravity_models``."""
    info = mc.get_provider_info("antigravity-oauth") or {}
    try:
        cred = await pm_service._resolve_credential(user_id, "antigravity-oauth", mc)
    except pm_service._OAuthUnavailable as exc:
        return pm_matching.build_result(
            "antigravity-oauth",
            "antigravity",
            None,
            mc,
            reason=exc.reason,
        )

    base_url = cred.base_url or str(info.get("base_url") or "https://daily-cloudcode-pa.googleapis.com")
    try:
        upstream = await native_catalog.fetch_antigravity_models(
            str(cred.api_key or ""),
            base_url=base_url,
        )
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        reason = pm_wire.UNAUTHORIZED if code in (401, 403) else pm_wire.BAD_RESPONSE
        return pm_matching.build_result(
            "antigravity-oauth",
            "antigravity",
            None,
            mc,
            holder=cred.holder,
            base_url_saved=bool(cred.base_url),
            reason=reason,
        )
    except (TimeoutError, httpx.TimeoutException, httpx.TransportError):
        return pm_matching.build_result(
            "antigravity-oauth",
            "antigravity",
            None,
            mc,
            holder=cred.holder,
            base_url_saved=bool(cred.base_url),
            reason=pm_wire.UNREACHABLE,
        )

    served = pm_matching.served_models(
        "antigravity-oauth",
        mc,
        holder=cred.holder,
        base_url_saved=bool(cred.base_url),
    )
    configured_ids = _provider_configured_model_ids(mc, "antigravity-oauth")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for um in upstream:
        if not um.id or um.id in seen:
            continue
        # Keep exact matches for every configured model_id (hidden awaiting-release
        # models are not "served"); only unconfigured internal/non-chat ids are dropped.
        if (
            um.id not in served
            and um.id not in configured_ids
            and (um.internal or not pm_wire.is_chat_model(um.id))
        ):
            continue
        seen.add(um.id)
        item: dict[str, Any] = {
            "id": um.id,
            "display_name": um.display_name or um.id,
        }
        if um.context_window:
            item["context_window"] = um.context_window
        normalized.append(item)

    return pm_matching.build_result(
        "antigravity-oauth",
        "antigravity",
        normalized,
        mc,
        holder=cred.holder,
        base_url_saved=bool(cred.base_url),
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


async def fetch_npm_codex_latest_version(
    *,
    url: str = NPM_CODEX_LATEST_URL,
    timeout_s: float = NPM_FETCH_TIMEOUT_S,
    transport: Any = None,
) -> str | None:
    """Fetch ``@openai/codex`` latest semver string from npm; return ``None`` on any failure."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout_s,
            transport=transport,
            follow_redirects=True,
        ) as client:
            resp = await client.get(url, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            logger.info("[model_availability_watch] npm @openai/codex returned HTTP %s; skipping probe", resp.status_code)
            return None
        payload = resp.json()
    except Exception as exc:
        logger.info(
            "[model_availability_watch] npm @openai/codex lookup failed (%s); skipping probe",
            type(exc).__name__,
        )
        return None
    if not isinstance(payload, dict):
        return None
    raw_version = payload.get("version")
    parsed = parse_semver(raw_version if isinstance(raw_version, str) else None)
    if parsed is None:
        return None
    return f"{parsed[0]}.{parsed[1]}.{parsed[2]}"


def _replace_query_param(url: str, key: str, value: str) -> str:
    parts = urlsplit(url)
    query_items = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != key]
    query_items.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment))


async def probe_codex_with_client_version(
    user_id: str,
    client_version: str,
    mc: Any,
) -> pm_matching.ModelsResult:
    """Request the Codex model list using an explicit ``client_version`` override."""
    info = mc.get_provider_info("codex-oauth")
    if not info:
        raise pm_service.UnknownProvider("codex-oauth")
    cred = await pm_service._resolve_credential(user_id, "codex-oauth", mc)
    req = pm_wire.build_list_request(
        info,
        api_key=cred.api_key,
        base_url=cred.base_url,
        token_headers=cred.token_headers,
    )
    probed_url = _replace_query_param(req.url, "client_version", client_version)
    probed_headers = {**req.headers, "User-Agent": f"codex_cli_rs/{client_version}"}
    probed_req = pm_wire.ListRequest(shape=req.shape, url=probed_url, headers=probed_headers)
    try:
        transport = await egress.user_endpoint_transport(cred.base_url)
    except egress.EndpointRefused as exc:
        raise pm_wire.ListFetchError(pm_wire.ENDPOINT_REFUSED) from exc
    models = pm_wire.normalize(probed_req.shape, await pm_wire.fetch_list(probed_req, transport=transport))
    return pm_matching.build_result(
        "codex-oauth",
        probed_req.shape,
        models,
        mc,
        holder=cred.holder,
        base_url_saved=bool(cred.base_url),
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


async def run_model_availability_check(
    *,
    user_id: str | None = None,
    probe_codex_upgrade: bool = True,
    npm_version_fetcher: Callable[[], Awaitable[str | None]] | None = None,
    codex_version_prober: Callable[[str, str, Any], Awaitable[pm_matching.ModelsResult]] | None = None,
) -> dict[str, Any]:
    """Check all connected subscription providers and run the Codex upgrade probe."""
    resolved_user_id = credential_user_id(user_id) if user_id else resolve_credential_holder_user_id()
    mc = LLM.get_model_config()
    dalton_meta = load_dalton_meta() or {}
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    providers_report: dict[str, dict[str, Any]] = {}
    missing_by_provider: dict[str, list[str]] = {}
    released_by_provider: dict[str, list[str]] = {}
    error_by_provider: dict[str, str] = {}

    for provider in iter_subscription_providers():
        if provider not in SUPPORTED_AVAILABILITY_PROVIDERS:
            providers_report[provider] = {
                "connected": False,
                "supported": False,
                "status": "unsupported",
                "reason": "provider_listing_protocol_unsupported",
                "ready": [],
                "missing": [],
                "missing_visible": [],
                "missing_awaiting_release": [],
                "released_configured": [],
                "released_unconfigured": [],
            }
            continue

        try:
            if provider == "antigravity-oauth":
                result = await _fetch_antigravity_available_models(resolved_user_id, mc)
            else:
                result = await pm_service.available_models(resolved_user_id, provider, refresh=True)
        except pm_service.NotConnected:
            providers_report[provider] = {
                "connected": False,
                "supported": True,
                "status": "not_connected",
                "ready": [],
                "missing": [],
                "missing_visible": [],
                "missing_awaiting_release": [],
                "released_configured": [],
                "released_unconfigured": [],
            }
            continue
        except (pm_service.UnsupportedProvider, pm_service.UnknownProvider) as exc:
            err_msg = type(exc).__name__
            providers_report[provider] = {
                "connected": True,
                "supported": True,
                "status": "error",
                "reason": err_msg,
                "error": err_msg,
                "ready": [],
                "missing": [],
                "missing_visible": [],
                "missing_awaiting_release": [],
                "released_configured": [],
                "released_unconfigured": [],
            }
            error_by_provider[provider] = err_msg
            continue
        except Exception as exc:
            err_msg = sanitize_error_text(exc)
            providers_report[provider] = {
                "connected": True,
                "supported": True,
                "status": "error",
                "error": err_msg,
                "ready": [],
                "missing": [],
                "missing_visible": [],
                "missing_awaiting_release": [],
                "released_configured": [],
                "released_unconfigured": [],
            }
            error_by_provider[provider] = err_msg
            continue

        raw_status = str(result.get("status") or "")
        raw_reason = result.get("reason")
        ready = list(result.get("ready") or [])
        missing_raw = list(result.get("missing") or [])
        if raw_status != "ok":
            err_msg = f"{raw_status or 'unknown'}:{raw_reason or 'unavailable'}"
            providers_report[provider] = {
                "connected": True,
                "supported": True,
                "status": "error",
                "upstream_status": raw_status,
                "reason": raw_reason,
                "error": err_msg,
                "fetched_at": result.get("fetched_at"),
                "cached": bool(result.get("cached", False)),
                "ready": ready,
                "missing": missing_raw,
                "missing_visible": [],
                "missing_awaiting_release": [],
                "released_configured": [],
                "released_unconfigured": [],
            }
            error_by_provider[provider] = err_msg
            continue

        missing_visible = sorted(
            k
            for k in missing_raw
            if _is_visible_provider_model(mc, k, provider)
            and not _is_awaiting_release(dalton_meta, k)
        )
        missing_awaiting_release = sorted(
            k
            for k in missing_raw
            if _is_visible_provider_model(mc, k, provider)
            and _is_awaiting_release(dalton_meta, k)
        )
        released_configured = sorted(
            k
            for k in ready
            if _is_visible_provider_model(mc, k, provider)
            and _is_awaiting_release(dalton_meta, k)
        )

        awaiting_prefixes = _provider_awaiting_prefixes(mc, dalton_meta, provider)
        configured_ids = _provider_configured_model_ids(mc, provider)
        raw_other_ids = {
            str(item.get("id") or "").strip()
            for item in (result.get("others") or [])
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        }
        # Hidden awaiting-release models are not "served", so the upstream lists them
        # under `others` with the exact model_id we configured.
        llm_cfg = getattr(mc, "llm_config", None) or {}
        ready_ids = {
            str((llm_cfg.get(k) or {}).get("model_id") or k) for k in ready
        }
        upstream_ids = raw_other_ids | ready_ids
        hidden_missing: set[str] = set()
        for key, entry in llm_cfg.items():
            if (
                not isinstance(entry, dict)
                or entry.get("provider") != provider
                or not _is_awaiting_release(dalton_meta, str(key))
            ):
                continue
            if str(entry.get("model_id") or key) in upstream_ids:
                released_configured = sorted(set(released_configured) | {str(key)})
            elif not entry.get("visible"):
                hidden_missing.add(str(key))
        missing_awaiting_release = sorted(set(missing_awaiting_release) | hidden_missing)
        others_ids = sorted(raw_other_ids - configured_ids)
        released_unconfigured = (
            sorted(
                mid
                for mid in others_ids
                if any(mid.startswith(prefix) for prefix in awaiting_prefixes)
            )
            if awaiting_prefixes
            else []
        )
        released_items = sorted(set(released_configured) | set(released_unconfigured))

        providers_report[provider] = {
            "connected": True,
            "supported": True,
            "status": "ok",
            "reason": raw_reason,
            "fetched_at": result.get("fetched_at"),
            "cached": bool(result.get("cached", False)),
            "ready": ready,
            "missing": missing_raw,
            "missing_visible": missing_visible,
            "missing_awaiting_release": missing_awaiting_release,
            "released_configured": released_configured,
            "released_unconfigured": released_unconfigured,
        }
        if missing_visible:
            missing_by_provider[provider] = missing_visible
        if released_items:
            released_by_provider[provider] = released_items

    codex_report = providers_report.get("codex-oauth") or {}
    codex_upstream_status = codex_report.get("upstream_status") or codex_report.get("status")
    current_codex_version = get_codex_client_version()
    codex_probe: dict[str, Any] = {
        "status": "skipped",
        "reason": "not_requested",
        "current_version": current_codex_version,
        "npm_latest_version": None,
        "unlocked_models": [],
        "upgrade_hint": None,
    }

    if not probe_codex_upgrade:
        codex_probe["reason"] = "disabled"
    elif not codex_report.get("connected"):
        codex_probe["reason"] = "codex_not_connected"
    elif "error" in codex_report and "upstream_status" not in codex_report:
        codex_probe["reason"] = f"codex_unavailable:{codex_report.get('reason') or codex_report.get('status')}"
    elif codex_upstream_status == "fallback" and codex_report.get("reason") in _NON_BUILDABLE_OAUTH_REASONS:
        codex_probe["reason"] = f"codex_unavailable:{codex_report.get('reason') or codex_upstream_status}"
    else:
        fetcher = npm_version_fetcher or fetch_npm_codex_latest_version
        latest_version = await fetcher()
        latest_tuple = parse_semver(latest_version)
        current_tuple = parse_semver(current_codex_version)
        if latest_version is None or latest_tuple is None:
            codex_probe["reason"] = "npm_version_unavailable"
        elif current_tuple is not None and latest_tuple <= current_tuple:
            codex_probe["npm_latest_version"] = latest_version
            codex_probe["reason"] = "already_up_to_date"
        else:
            codex_probe["npm_latest_version"] = latest_version
            prober = codex_version_prober or probe_codex_with_client_version
            try:
                probed_res = await prober(resolved_user_id, latest_version, mc)
            except Exception as exc:
                codex_probe["status"] = "error"
                codex_probe["reason"] = "probe_failed"
                codex_probe["error"] = sanitize_error_text(exc)
            else:
                probed_status = str(probed_res.get("status") or "")
                probed_ready = list(probed_res.get("ready") or [])
                main_was_fallback = codex_upstream_status == "fallback"
                if codex_upstream_status == "ok":
                    baseline_ready: set[str] | None = set(codex_report.get("ready") or [])
                elif main_was_fallback and codex_report.get("reason") == pm_matching.NO_MATCH:
                    baseline_ready = set()
                else:
                    baseline_ready = None

                if probed_status == "ok" and baseline_ready is not None:
                    unlocked = sorted(
                        k
                        for k in probed_ready
                        if k not in baseline_ready and _is_visible_provider_model(mc, k, "codex-oauth")
                    )
                    codex_probe["status"] = "ok"
                    if main_was_fallback:
                        codex_probe["reason"] = (
                            "main_check_fallback_probe_unlocked"
                            if unlocked
                            else "main_check_fallback_probe_no_additional_models"
                        )
                    else:
                        codex_probe["reason"] = "unlocked_models" if unlocked else "no_additional_models"
                    codex_probe["unlocked_models"] = unlocked
                    if unlocked:
                        codex_probe["upgrade_hint"] = (
                            f"需要提高 DALTON_CODEX_CLIENT_VERSION 到 {latest_version}"
                        )
                elif probed_status == "ok":
                    # Main check fell back without a usable baseline (e.g. bad_response/unreachable);
                    # probe succeeded, though baseline gating cannot be inferred.
                    codex_probe["status"] = "ok"
                    codex_probe["reason"] = (
                        f"main_check_fallback_probe_ok:{codex_report.get('reason') or codex_upstream_status or 'unknown'}"
                    )
                    codex_probe["unlocked_models"] = []
                else:
                    codex_probe["status"] = "skipped"
                    codex_probe["reason"] = (
                        f"probe_non_ok:{probed_res.get('reason') or probed_status or 'unknown'}"
                    )

    return {
        "ok": not error_by_provider,
        "checked_at": checked_at,
        "user_id": resolved_user_id,
        "supported_providers": sorted(SUPPORTED_AVAILABILITY_PROVIDERS),
        "providers": providers_report,
        "missing_by_provider": missing_by_provider,
        "released_by_provider": released_by_provider,
        "error_by_provider": error_by_provider,
        "codex_version_probe": codex_probe,
    }
