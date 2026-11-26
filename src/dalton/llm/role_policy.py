"""Model family resolution, text-only detection, availability, and role policy checks (P0.6 / P0N.5)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from src.dalton.llm.model_family import infer_family_from_model_id
from src.dalton.llm.overlay import current_overlay_stamp, load_dalton_meta
from src.dalton.profile import (
    DaltonStartupConfigError,
    DeploymentProfile,
    ProfileConfigError,
    is_core_profile,
    resolve_profile,
)
from src.dalton.settings import get_disabled_providers

REPO_ROOT = Path(__file__).resolve().parents[3]
LATEST_NATIVE_SMOKE_PATH = REPO_ROOT / "var" / "dalton" / "smoke" / "latest_native.json"

# Single-vendor first-party provider groups whose upstream model family can be
# verified when no custom endpoint override is active. Multi-vendor gateways
# (e.g. OpenRouter, custom OpenAI-compatible endpoints, local servers) are
# intentionally omitted so they classify as "unknown".
_FIRST_PARTY_PROVIDER_FAMILIES: dict[str, str] = {
    "anthropic": "anthropic-claude",
    "openai": "openai-gpt",
    "gemini": "google-gemini",
    "xai": "xai-grok",
    "deepseek": "deepseek",
    "z-ai": "zai-glm",
    "dashscope": "alibaba-qwen",
    "moonshot": "moonshot-kimi",
    "minimax": "minimax",
}

_CAPABILITY_CACHE_KEY: tuple[str | None, str] | None = None
_CAPABILITY_CACHE_VALUE: tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, bool]] | None = None
_SMOKE_CACHE_KEY: str | None = None
_SMOKE_CACHE_VALUE: dict[str, Any] | None = None

_OAUTH_PROVIDER_TOKEN_KEYS: dict[str, str] = {
    "claude-oauth": "claude-oauth",
    "codex-oauth": "codex-oauth",
    "antigravity-oauth": "antigravity-oauth",
    "muse-oauth": "muse-oauth",
    "xai-oauth": "xai-oauth",
}


def _file_stat_stamp(path: Path) -> str:
    try:
        st = os.stat(path)
        return f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return "-"


def _load_tool_capability_snapshot() -> tuple[
    dict[str, dict[str, Any]],
    dict[str, Any],
    dict[str, bool],
]:
    """Load and cache `(dalton_meta, latest_native_smoke, resolved_tool_calling)` by catalog version.

    Avoids re-reading overlay and smoke JSON files per model key on `/api/v1/models`
    requests (reads disk at most once per `(overlay_stamp, smoke_stat)` change).
    """
    global _CAPABILITY_CACHE_KEY, _CAPABILITY_CACHE_VALUE
    from collections.abc import Mapping

    from src.dalton.services.team_model_catalog.billing import _SCOPED_DALTON_META

    scoped_meta = _SCOPED_DALTON_META.get()
    if isinstance(scoped_meta, Mapping):
        # Active request scope (e.g. GET /api/v1/models) already fetched dalton_meta;
        # compute from bound map + cached smoke without calling load_dalton_meta() or
        # overwriting the process-level capability cache.
        meta_map = {str(k): dict(v) for k, v in scoped_meta.items() if isinstance(v, Mapping)}
        native_smoke = _load_latest_native_smoke_cached()
        resolved: dict[str, bool] = {}
        for key, meta in meta_map.items():
            if isinstance(meta.get("tool_calling"), bool):
                resolved[key] = meta["tool_calling"]
        for key, smoke_entry in native_smoke.items():
            if not isinstance(smoke_entry, dict):
                continue
            checks = smoke_entry.get("checks") or {}
            if not isinstance(checks, dict):
                continue
            n3 = checks.get("N3")
            if n3 == "fail":
                resolved[key] = False
            elif n3 == "pass" and key not in resolved:
                resolved[key] = True
        return (meta_map, native_smoke, resolved)

    cache_key = (current_overlay_stamp(), _file_stat_stamp(LATEST_NATIVE_SMOKE_PATH))
    if _CAPABILITY_CACHE_KEY == cache_key and _CAPABILITY_CACHE_VALUE is not None:
        return _CAPABILITY_CACHE_VALUE

    meta_map = load_dalton_meta()
    native_smoke = _load_latest_native_smoke()
    resolved = {}

    for key, meta in meta_map.items():
        if isinstance(meta, dict) and isinstance(meta.get("tool_calling"), bool):
            resolved[key] = meta["tool_calling"]

    for key, smoke_entry in native_smoke.items():
        if not isinstance(smoke_entry, dict):
            continue
        checks = smoke_entry.get("checks") or {}
        if not isinstance(checks, dict):
            continue
        n3 = checks.get("N3")
        if n3 == "fail":
            resolved[key] = False
        elif n3 == "pass" and key not in resolved:
            resolved[key] = True

    _CAPABILITY_CACHE_KEY = cache_key
    _CAPABILITY_CACHE_VALUE = (meta_map, native_smoke, resolved)
    return _CAPABILITY_CACHE_VALUE


def _load_latest_native_smoke_cached() -> dict[str, Any]:
    """Load and cache `latest_native.json` by file stat stamp without calling `load_dalton_meta()`."""
    global _SMOKE_CACHE_KEY, _SMOKE_CACHE_VALUE
    stamp = _file_stat_stamp(LATEST_NATIVE_SMOKE_PATH)
    if _SMOKE_CACHE_KEY == stamp and _SMOKE_CACHE_VALUE is not None:
        return _SMOKE_CACHE_VALUE
    data = _load_latest_native_smoke()
    _SMOKE_CACHE_KEY = stamp
    _SMOKE_CACHE_VALUE = data
    return data


def clear_tool_capability_cache() -> None:
    """Clear the cached overlay/smoke capability snapshot (test helper)."""
    global _CAPABILITY_CACHE_KEY, _CAPABILITY_CACHE_VALUE, _SMOKE_CACHE_KEY, _SMOKE_CACHE_VALUE
    _CAPABILITY_CACHE_KEY = None
    _CAPABILITY_CACHE_VALUE = None
    _SMOKE_CACHE_KEY = None
    _SMOKE_CACHE_VALUE = None


def _merged_model_entry(model_key: str) -> dict[str, Any] | None:
    """Return the merged `ModelConfig.get_model_config(model_key)` entry if present."""
    try:
        from src.llms.llm import LLM as LLMFactory

        entry = LLMFactory.get_model_config().get_model_config(model_key)
        if isinstance(entry, dict):
            return entry
    except Exception:
        pass
    return None


def _normalized_url(url: str | None) -> str:
    return (url or "").strip().rstrip("/").lower()


def _has_custom_endpoint_override(
    provider_name: str,
    provider_info: dict[str, Any] | None,
    base_urls: dict[str, Any] | None,
) -> bool:
    if not isinstance(base_urls, dict) or not provider_name:
        return False
    override = _normalized_url(
        base_urls.get(provider_name) if isinstance(base_urls.get(provider_name), str) else None
    )
    if not override:
        return False
    default_url = _normalized_url(
        (provider_info or {}).get("base_url")
        if isinstance((provider_info or {}).get("base_url"), str)
        else None
    )
    return override != default_url


def family_of(model_key: str | None) -> str:
    """Return canonical underlying vendor family for `model_key` (P0N.5 L20).

    Lookup order:
    1. `load_dalton_meta()[key]["family"]` (native overlay and native-sync overlay)
    2. Merged `ModelConfig.get_model_config(key)["model_id"]` via `infer_family_from_model_id`
    3. `key` itself via `infer_family_from_model_id`
    4. `'unknown'`
    """
    if not model_key:
        return "unknown"
    key = str(model_key).strip()
    if not key:
        return "unknown"

    # 1. Explicit dalton_meta family
    meta_map, _, _ = _load_tool_capability_snapshot()
    if key in meta_map and isinstance(meta_map[key], dict):
        fam = meta_map[key].get("family")
        if fam and str(fam) != "unknown":
            return str(fam)

    # 2. Merged ModelConfig model_id
    mcfg = _merged_model_entry(key)
    if isinstance(mcfg, dict):
        model_id = str(mcfg.get("model_id") or "").strip()
        if model_id:
            inferred = infer_family_from_model_id(model_id)
            if inferred != "unknown":
                return inferred

    # 3. Key itself
    return infer_family_from_model_id(key)


def _find_custom_model_entry(
    model_pref: dict[str, Any] | None,
    model_key: str,
) -> dict[str, Any] | None:
    """Return the user-defined ``custom_models`` entry matching ``model_key``, or ``None``."""
    if not isinstance(model_pref, dict):
        return None
    custom_list = model_pref.get("custom_models")
    if not isinstance(custom_list, list):
        return None
    for entry in custom_list:
        if isinstance(entry, dict) and str(entry.get("name") or "").strip() == model_key:
            return entry
    return None


def family_of_for_config(
    model_key: str | None,
    *,
    model_pref: dict[str, Any] | None = None,
    base_urls: dict[str, Any] | None = None,
) -> str:
    """Resolve model family from actual inference configuration rather than alias names.

    - For user ``custom_models`` entries, inspects the actual provider and ``model_id``:
      custom providers / OpenAI-compatible gateways, custom ``base_url`` overrides, and
      multi-vendor aggregators cannot be independently verified and always classify as
      ``'unknown'`` (preventing alias-name spoofing).
    - For built-in catalog models, an active custom ``base_url`` override on the model's
      provider also classifies as ``'unknown'``; otherwise delegates to :func:`family_of`.
    """
    if not model_key or not str(model_key).strip():
        return "unknown"
    key = str(model_key).strip()

    from src.llms.llm import LLM as LLMFactory
    from src.server.services.llm import user_models

    mc = LLMFactory.get_model_config()

    cm = _find_custom_model_entry(model_pref, key)
    if isinstance(cm, dict):
        provider_name = str(cm.get("provider") or "").strip()
        model_id = str(cm.get("model_id") or "").strip()
        if not provider_name:
            return "unknown"
        # User-defined custom provider / gateway endpoint: upstream vendor cannot be verified.
        if user_models.custom_provider(model_pref, provider_name) is not None:
            return "unknown"
        prov_info = mc.get_provider_info(provider_name) or {}
        if not prov_info:
            return "unknown"
        if _has_custom_endpoint_override(provider_name, prov_info, base_urls):
            return "unknown"
        parent_group = str(prov_info.get("parent_provider") or provider_name).strip().lower()
        expected_family = _FIRST_PARTY_PROVIDER_FAMILIES.get(parent_group)
        if not expected_family:
            return "unknown"
        inferred = infer_family_from_model_id(model_id)
        return expected_family if inferred == expected_family else "unknown"

    mcfg = mc.get_model_config(key)
    if isinstance(mcfg, dict):
        provider_name = str(mcfg.get("provider") or "").strip()
        prov_info = mc.get_provider_info(provider_name) or {}
        if _has_custom_endpoint_override(provider_name, prov_info, base_urls):
            return "unknown"
        return family_of(key)

    return family_of(key)


async def _load_execution_inference_state(
    user_id: str | None,
) -> tuple[str, dict[str, Any] | None, dict[str, str], dict[str, str]]:
    """Load `(exec_user_id, model_pref, api_keys_map, base_urls_map)` for the calling/credential user."""
    from src.dalton.credentials import credential_user_id
    from src.dalton.llm.model_prefs import _read_user_model_preference_map

    uid = (user_id or "").strip()
    exec_uid = credential_user_id(uid) if uid else ""
    if not exec_uid:
        return "", None, {}, {}

    mp_raw = await _read_user_model_preference_map(exec_uid)
    model_pref = dict(mp_raw) if isinstance(mp_raw, dict) else None

    keys_map: dict[str, str] = {}
    base_urls: dict[str, str] = {}
    try:
        from src.server.database.api_keys import get_user_api_keys

        rows = await get_user_api_keys(exec_uid)
        if isinstance(rows, dict):
            raw_keys = rows.get("keys")
            if isinstance(raw_keys, dict):
                keys_map = {str(p): str(v) for p, v in raw_keys.items() if v}
            else:
                keys_map = {
                    str(k): str(v)
                    for k, v in rows.items()
                    if k not in ("byok_enabled", "keys", "base_urls") and v
                }
            raw_urls = rows.get("base_urls")
            if isinstance(raw_urls, dict):
                base_urls = {str(p): str(v) for p, v in raw_urls.items() if isinstance(v, str) and v.strip()}
    except Exception:
        pass

    return exec_uid, model_pref, keys_map, base_urls


async def resolve_model_family(
    model_key: str | None,
    *,
    user_id: str | None = None,
    model_pref: dict[str, Any] | None = None,
    base_urls: dict[str, Any] | None = None,
) -> str:
    """Async config-aware model family resolver (Standalone Core only).

    In Everflow profile, restores the original name-based :func:`family_of`
    behavior without consulting user/holder inference preferences.
    """
    if not model_key or not str(model_key).strip():
        return "unknown"
    if not is_core_profile():
        return family_of(model_key)
    eff_pref = model_pref
    eff_urls = base_urls
    if eff_pref is None and user_id:
        _, eff_pref, _, loaded_urls = await _load_execution_inference_state(user_id)
        if eff_urls is None:
            eff_urls = loaded_urls
    return family_of_for_config(model_key, model_pref=eff_pref, base_urls=eff_urls)


def _load_latest_native_smoke() -> dict[str, Any]:
    if not LATEST_NATIVE_SMOKE_PATH.exists():
        return {}
    try:
        data = json.loads(LATEST_NATIVE_SMOKE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def is_text_only(model_key: str | None) -> bool:
    """Return True if `model_key` does not support tool calling (P0N.5)."""
    if not model_key:
        return False
    key = str(model_key).strip()
    if not key:
        return False

    _, _, resolved_cap = _load_tool_capability_snapshot()
    return resolved_cap.get(key) is False


def is_forbidden_for_roles(model_key: str | None) -> bool:
    """Return True if `model_key` is marked `forbidden_for_roles` (e.g. contributor OAuth models, L21)."""
    if not model_key:
        return False
    key = str(model_key).strip()
    meta_map, _, _ = _load_tool_capability_snapshot()
    meta = meta_map.get(key)
    if isinstance(meta, dict) and bool(meta.get("forbidden_for_roles")):
        return True
    if "contributor" in key.lower():
        return True
    return False


def annotate_model_metadata_tool_calling(
    metadata: dict[str, Any],
    *,
    dalton_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach evidence-backed ``tool_calling`` booleans to ``/api/v1/models`` metadata.

    Reads the cached overlay/smoke capability snapshot at most once per catalog
    version (zero per-model disk reads). Models with explicit evidence in the
    manifest, ``dalton_meta``, or smoke checks receive ``tool_calling: True/False``
    (smoke ``N3 == "fail"`` always overrides to ``False``). Models with no
    recorded evidence omit ``tool_calling`` so callers treat them as unverified
    rather than assuming support.
    """
    if not isinstance(metadata, dict):
        return metadata
    _, native_smoke, resolved_cap = _load_tool_capability_snapshot()
    out: dict[str, Any] = {}
    changed = False
    for model_key, entry in metadata.items():
        if not isinstance(entry, dict):
            out[model_key] = entry
            continue
        smoke_entry = native_smoke.get(model_key)
        smoke_checks = smoke_entry.get("checks") if isinstance(smoke_entry, dict) else None
        smoke_failed = isinstance(smoke_checks, dict) and smoke_checks.get("N3") == "fail"

        if smoke_failed:
            if entry.get("tool_calling") is not False:
                updated = dict(entry)
                updated["tool_calling"] = False
                out[model_key] = updated
                changed = True
            else:
                out[model_key] = entry
            continue

        if isinstance(entry.get("tool_calling"), bool):
            out[model_key] = entry
            continue

        cap: Any = None
        if isinstance(dalton_meta, dict):
            dm_entry = dalton_meta.get(model_key)
            if isinstance(dm_entry, dict) and isinstance(dm_entry.get("tool_calling"), bool):
                cap = dm_entry["tool_calling"]
            elif isinstance(smoke_checks, dict) and smoke_checks.get("N3") == "pass":
                cap = True
        else:
            cap = resolved_cap.get(model_key)
        if isinstance(cap, bool):
            updated = dict(entry)
            updated["tool_calling"] = cap
            out[model_key] = updated
            changed = True
        else:
            out[model_key] = entry
    return out if changed else metadata


_ROLE_PREF_SPEC: dict[str, tuple[str, Literal["primary", "background"]]] = {
    "assist": ("dalton_assist_model", "background"),
    "extract": ("dalton_extract_model", "background"),
    "conversation_extract": ("dalton_extract_model", "background"),
    "audit": ("dalton_audit_model", "primary"),
    "language": ("dalton_language_model", "primary"),
    "reviewer": ("dalton_reviewer_models", "primary"),
}


def _validate_role_candidate(role: str, candidate: str) -> str:
    cand = candidate.strip()
    if is_forbidden_for_roles(cand):
        raise RolePolicyError(
            f"角色 {role} 配置了禁用变体 {cand}（contributor 变体可能用于训练，L21 严禁用于任何基金研究角色）。"
        )
    return cand


def _extract_candidate_strings(raw: Any) -> list[str]:
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if isinstance(item, str) and item.strip()]
    return []


def _custom_model_is_usable(
    cm: dict[str, Any],
    *,
    model_pref: dict[str, Any] | None,
    keys_map: dict[str, str],
    base_urls: dict[str, str],
    disabled_providers: set[str],
) -> bool:
    """Return True when a user ``custom_models`` entry can actually run under the execution account."""
    from src.llms.llm import LLM as LLMFactory
    from src.server.services.llm import user_models

    name = str(cm.get("name") or "").strip()
    provider_name = str(cm.get("provider") or "").strip()
    if not name or not provider_name or is_forbidden_for_roles(name):
        return False
    if provider_name in disabled_providers:
        return False

    mc = LLMFactory.get_model_config()
    custom_prov = user_models.custom_provider(model_pref, provider_name)
    if custom_prov is not None:
        parent = str(custom_prov.get("parent_provider") or "").strip()
        if parent in disabled_providers:
            return False
        parent_info = mc.get_provider_info(parent) or {}
        if not parent_info:
            return False
        # Custom provider works if an API key or custom base_url is configured (keyless local gateways allowed).
        return bool(keys_map.get(provider_name) or base_urls.get(provider_name))

    prov_info = mc.get_provider_info(provider_name) or {}
    if not prov_info:
        return False
    parent = str(prov_info.get("parent_provider") or "").strip()
    if parent and parent in disabled_providers:
        return False
    access_type = str(prov_info.get("access_type") or "")
    if access_type == "local":
        return bool(base_urls.get(provider_name) or prov_info.get("base_url"))
    env_key = str(prov_info.get("env_key") or "")
    if not env_key:
        return True
    has_env = bool(os.environ.get(env_key, "").strip())
    has_byok = bool(keys_map.get(provider_name) or (parent and keys_map.get(parent)))
    return has_env or has_byok


async def usable_custom_models_for_user(
    user_id: str | None,
    *,
    excluded: set[str] | None = None,
) -> list[str]:
    """Return usable ``custom_models`` names under ``user_id``'s execution/credential account."""
    exec_uid, exec_mp, keys_map, base_urls = await _load_execution_inference_state(user_id)
    if not exec_uid or not isinstance(exec_mp, dict):
        return []
    custom_list = exec_mp.get("custom_models")
    if not isinstance(custom_list, list):
        return []
    disabled = get_disabled_providers()
    out: list[str] = []
    seen: set[str] = set()
    for cm in custom_list:
        if not isinstance(cm, dict):
            continue
        name = str(cm.get("name") or "").strip()
        if not name or name in seen or (excluded and name in excluded):
            continue
        if _custom_model_is_usable(
            cm,
            model_pref=exec_mp,
            keys_map=keys_map,
            base_urls=base_urls,
            disabled_providers=disabled,
        ):
            seen.add(name)
            out.append(name)
    return out


async def _pick_first_usable_core_candidate(
    role: str,
    candidates: list[str],
    *,
    exec_uid: str,
    exec_mp: dict[str, Any] | None,
    keys_map: dict[str, str],
    base_urls: dict[str, str],
    excluded: set[str] | None = None,
) -> str:
    """Walk ``candidates`` in order, skipping forbidden/stale/unavailable entries.

    Reuses normal inference resolution (``model_resolves`` + ``explain_model_availability`` /
    custom provider credential check) under ``exec_uid``. Raises :class:`RolePolicyError`
    only when at least one candidate was forbidden and NO usable candidate remained.
    """
    from src.server.services.llm.availability import model_resolves

    disabled = get_disabled_providers()
    avail_map: dict[str, dict[str, Any]] | None = None
    saw_forbidden: str | None = None
    seen: set[str] = set()

    for raw in candidates:
        cand = str(raw or "").strip()
        if not cand or cand in seen or (excluded and cand in excluded):
            continue
        seen.add(cand)

        if is_forbidden_for_roles(cand):
            if saw_forbidden is None:
                saw_forbidden = cand
            continue

        # Check normal inference resolution under the execution account.
        if not model_resolves(exec_mp or {}, cand):
            continue

        # Custom model under the execution account takes precedence over catalog.
        cm = _find_custom_model_entry(exec_mp, cand)
        if isinstance(cm, dict):
            if _custom_model_is_usable(
                cm,
                model_pref=exec_mp,
                keys_map=keys_map,
                base_urls=base_urls,
                disabled_providers=disabled,
            ):
                return cand
            continue

        # System catalog model: verify provider/credential availability under exec_uid.
        if exec_uid:
            if avail_map is None:
                try:
                    avail_map = await explain_model_availability(user_id=exec_uid)
                except Exception:
                    avail_map = {}
            info = avail_map.get(cand)
            if isinstance(info, dict) and info.get("available"):
                return cand
            continue

        # No execution user_id (deployment-only path): verify catalog entry not disabled/hidden.
        mcfg = _merged_model_entry(cand)
        if isinstance(mcfg, dict):
            if mcfg.get("visible") is False:
                continue
            prov = str(mcfg.get("provider") or "")
            if prov and prov in disabled:
                continue
            return cand

    if saw_forbidden is not None:
        raise RolePolicyError(
            f"角色 {role} 配置了禁用变体 {saw_forbidden}（contributor 变体可能用于训练，L21 严禁用于任何基金研究角色），且无其他可用候选模型。"
        )
    return ""


async def resolve_customer_configured_model(
    user_id: str | None = None,
    *,
    prefer_slot: Literal["primary", "background"] = "primary",
    deployment_only: bool = False,
    excluded: set[str] | None = None,
) -> str:
    """Resolve the customer's configured primary, background, or custom model (Core profile).

    Never falls back to upstream ``agent_config`` defaults: when customer preferences
    are empty or ``deployment_only=True``, returns ``""``.
    """
    if deployment_only:
        return ""

    from src.dalton.llm.model_prefs import _read_user_model_preference_map

    uid = (user_id or "").strip()
    exec_uid, exec_mp, keys_map, base_urls = await _load_execution_inference_state(uid)
    caller_mp: dict[str, Any] | None = None
    if uid and uid != exec_uid:
        raw_caller = await _read_user_model_preference_map(uid)
        caller_mp = dict(raw_caller) if isinstance(raw_caller, dict) else None
    elif uid == exec_uid:
        caller_mp = exec_mp

    slot_keys = (
        ("preferred_background_model", "preferred_model")
        if prefer_slot == "background"
        else ("preferred_model", "preferred_background_model")
    )
    candidates: list[str] = []
    for mp in (caller_mp, exec_mp):
        if not isinstance(mp, dict):
            continue
        for sk in slot_keys:
            candidates.extend(_extract_candidate_strings(mp.get(sk)))

    # Custom models defined under the execution account (resolved where dalton_complete runs).
    if isinstance(exec_mp, dict):
        custom_list = exec_mp.get("custom_models")
        if isinstance(custom_list, list):
            for entry in custom_list:
                if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    candidates.append(entry["name"].strip())

    if not candidates:
        return ""

    return await _pick_first_usable_core_candidate(
        prefer_slot,
        candidates,
        exec_uid=exec_uid,
        exec_mp=exec_mp,
        keys_map=keys_map,
        base_urls=base_urls,
        excluded=excluded,
    )


async def resolve_effective_role_model(
    role: str = "assist",
    explicit_model: str | None = None,
    *,
    user_id: str | None = None,
    deployment_only: bool = False,
    excluded: set[str] | None = None,
) -> str:
    """Unified effective model resolution for assist, audit, language, extract, and reviewer roles.

    - **Everflow profile**: preserves existing ``explicit -> dalton_model_default/dalton_model_pref``
      precedence and returns ``""`` when unconfigured.
    - **Standalone Core profile** (``is_core_profile()``):
      - When ``deployment_only=True``, reads ONLY deployment config (``dalton.core.yaml``),
        skipping forbidden contributor candidates, and returns ``""`` when empty (never falls
        back to upstream ``agent_config`` defaults).
      - Otherwise walks ``role config -> user default (primary/background + custom models) ->
        deployment default`` sequentially, verifying each candidate via normal inference
        availability under the execution/credential account, skipping stale/unavailable or
        forbidden contributor candidates, and returning ``""`` when nothing is configured.
    """
    if explicit_model and explicit_model.strip():
        return _validate_role_candidate(role, explicit_model)

    from src.dalton.llm.model_prefs import (
        _read_user_model_preference_map,
        dalton_model_default,
        dalton_model_pref,
    )

    pref_key, prefer_slot = _ROLE_PREF_SPEC.get(
        role, ("dalton_assist_model", "background")
    )

    if not is_core_profile():
        val = (
            dalton_model_default(pref_key)
            if deployment_only
            else await dalton_model_pref(user_id, pref_key)
        )
        items = _extract_candidate_strings(val)
        if items:
            return _validate_role_candidate(role, items[0])
        return ""

    # Standalone Core profile: deployment-only callers read ONLY dalton.core.yaml
    # (never user prefs, never upstream agent_config Claude defaults).
    if deployment_only:
        deploy_candidates = _extract_candidate_strings(dalton_model_default(pref_key))
        if role == "language":
            deploy_candidates.extend(
                _extract_candidate_strings(dalton_model_default("dalton_audit_model"))
            )
        if not deploy_candidates:
            return ""
        return await _pick_first_usable_core_candidate(
            role,
            deploy_candidates,
            exec_uid="",
            exec_mp=None,
            keys_map={},
            base_urls={},
            excluded=excluded,
        )

    uid = (user_id or "").strip()
    exec_uid, exec_mp, keys_map, base_urls = await _load_execution_inference_state(uid)
    caller_mp: dict[str, Any] | None = None
    if uid and uid != exec_uid:
        raw_caller = await _read_user_model_preference_map(uid)
        caller_mp = dict(raw_caller) if isinstance(raw_caller, dict) else None
    elif uid == exec_uid:
        caller_mp = exec_mp

    candidates: list[str] = []

    # 1. Role config: caller pref -> execution/holder pref -> deployment dalton.core.yaml
    role_keys = (pref_key, "dalton_audit_model") if role == "language" else (pref_key,)
    for rk in role_keys:
        if isinstance(caller_mp, dict):
            candidates.extend(_extract_candidate_strings(caller_mp.get(rk)))
        if isinstance(exec_mp, dict) and exec_mp is not caller_mp:
            candidates.extend(_extract_candidate_strings(exec_mp.get(rk)))
        candidates.extend(_extract_candidate_strings(dalton_model_default(rk)))

    # 2. User default: preferred slot -> other slot -> custom models under execution account
    slot_keys = (
        ("preferred_background_model", "preferred_model")
        if prefer_slot == "background"
        else ("preferred_model", "preferred_background_model")
    )
    for mp in (caller_mp, exec_mp):
        if not isinstance(mp, dict):
            continue
        for sk in slot_keys:
            candidates.extend(_extract_candidate_strings(mp.get(sk)))

    if isinstance(exec_mp, dict):
        custom_list = exec_mp.get("custom_models")
        if isinstance(custom_list, list):
            for entry in custom_list:
                if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    candidates.append(entry["name"].strip())

    if not candidates:
        return ""

    return await _pick_first_usable_core_candidate(
        role,
        candidates,
        exec_uid=exec_uid,
        exec_mp=exec_mp,
        keys_map=keys_map,
        base_urls=base_urls,
        excluded=excluded,
    )


async def explain_model_availability(
    *,
    user_id: str,
    require_tool_calling: bool = False,
) -> dict[str, dict[str, Any]]:
    """Return availability status and reason for every model key in the merged catalog."""
    if not user_id or not str(user_id).strip():
        raise ValueError("user_id is required")
    user_id = str(user_id).strip()
    from src.llms.llm import ModelConfig

    mc = ModelConfig()
    llm_cfg = mc.llm_config or {}
    flat_providers = mc.flat_providers or {}
    disabled = get_disabled_providers()
    meta_map = load_dalton_meta()

    # Query OAuth connection states for the subscription providers. Availability
    # follows the unified projection (state, not row presence): a flagged or
    # unreadable connection is not connected.
    oauth_connected: dict[str, bool] = {}
    try:
        from src.server.services.oauth_health import connection_state
        from src.server.database.oauth_tokens import get_oauth_connection

        for prov_key in _OAUTH_PROVIDER_TOKEN_KEYS:
            try:
                row = await get_oauth_connection(user_id, prov_key)
                connected = connection_state(row) == "connected"
                if connected and prov_key == "muse-oauth":
                    from src.dalton.llm.providers.muse.oauth import (
                        is_invalidated_muse_token,
                        is_minted_muse_token,
                    )

                    if row and (
                        is_invalidated_muse_token(row)
                        or ("account_id" in row and not is_minted_muse_token(row))
                    ):
                        connected = False
                oauth_connected[prov_key] = connected
            except Exception:
                oauth_connected[prov_key] = False
    except Exception:
        pass

    # Query user BYOK keys if available (get_user_api_keys returns {"byok_enabled", "keys": {prov: key}, "base_urls"})
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
    except Exception:
        pass

    report: dict[str, dict[str, Any]] = {}
    for key, mcfg in llm_cfg.items():
        if not isinstance(mcfg, dict):
            continue
        provider = str(mcfg.get("provider") or "")
        prov_info = flat_providers.get(provider) or {}
        parent_provider = str(prov_info.get("parent_provider") or "")
        meta = meta_map.get(key) or {}

        reasons: list[str] = []
        if is_forbidden_for_roles(key) or bool(meta.get("forbidden_for_roles")):
            reasons.append("forbidden_for_roles")
        if mcfg.get("visible") is False:
            reasons.append("hidden")
        if provider in disabled or (parent_provider and parent_provider in disabled):
            reasons.append("provider_disabled")
        access_type = str(prov_info.get("access_type") or "")
        if access_type == "oauth" or provider in _OAUTH_PROVIDER_TOKEN_KEYS:
            if not oauth_connected.get(provider, False):
                reasons.append("oauth_not_connected")
        else:
            env_key = str(prov_info.get("env_key") or "")
            has_env = bool(env_key and os.environ.get(env_key, "").strip())
            if access_type == "coding_plan" or provider == "z-ai-cn-coding":
                has_byok = provider in byok_providers
            else:
                has_byok = provider in byok_providers or (
                    parent_provider and parent_provider in byok_providers
                )
            if env_key and not (has_env or has_byok):
                reasons.append("api_key_missing")

        if require_tool_calling and is_text_only(key):
            reasons.append("text_only")

        report[key] = {
            "available": len(reasons) == 0,
            "provider": provider,
            "family": family_of(key),
            "reasons": reasons,
        }
    return report


async def available_model_keys(
    *,
    user_id: str,
    require_tool_calling: bool = False,
) -> set[str]:
    """Return the set of model keys currently available for `user_id` (P0N.5 #4)."""
    if not user_id or not str(user_id).strip():
        raise ValueError("user_id is required")
    report = await explain_model_availability(
        user_id=user_id, require_tool_calling=require_tool_calling
    )
    return {k for k, v in report.items() if v.get("available")}


def _select_independent_reviewer_from_candidates(
    lead_model: str,
    candidates: list[str],
    *,
    require_tool_calling: bool = False,
    available_models: set[str] | None = None,
) -> str | None:
    """Pick the first candidate from an explicit list whose `family_of()` differs from `lead_model` and is available."""
    lead_family = family_of(lead_model)
    if lead_family == "unknown":
        return None

    disabled_providers = get_disabled_providers()

    for cand in candidates:
        if is_forbidden_for_roles(cand):
            continue
        cand_family = family_of(cand)
        if cand_family == "unknown" or cand_family == lead_family:
            continue
        if require_tool_calling and is_text_only(cand):
            continue
        mcfg = _merged_model_entry(cand)
        if isinstance(mcfg, dict):
            if mcfg.get("visible") is False:
                continue
            prov = str(mcfg.get("provider") or "")
            if prov in disabled_providers:
                continue
        if available_models is not None and cand not in available_models:
            continue
        return cand
    return None


async def select_independent_reviewer(
    lead_model: str,
    candidates: list[str] | None = None,
    *,
    require_tool_calling: bool = False,
    available_models: set[str] | None = None,
    user_id: str | None = None,
) -> str | None:
    """Pick the first candidate whose `family_of()` differs from `lead_model` and is available."""
    if candidates is None:
        from src.dalton.llm.model_prefs import dalton_model_pref

        resolved_cands = await dalton_model_pref(user_id, "dalton_reviewer_models")
        candidates = list(resolved_cands) if isinstance(resolved_cands, list) else []

    return _select_independent_reviewer_from_candidates(
        lead_model,
        candidates,
        require_tool_calling=require_tool_calling,
        available_models=available_models,
    )


class RolePolicyError(DaltonStartupConfigError):
    """Raised when a forbidden model (e.g. contributor OAuth variant, L21) is assigned to any role."""


def _load_default_agent_config() -> dict[str, Any]:
    from ptc_agent.config.file_utils import (
        AGENT_CONFIG_FILE,
        _deep_merge,
        _read_yaml,
        find_config_file,
        load_yaml_config,
    )

    profile = resolve_profile()

    explicit_env_path = os.getenv("PTC_CONFIG_FILE", "").strip()
    if explicit_env_path and not Path(explicit_env_path).is_file():
        raise DaltonStartupConfigError(
            f"PTC_CONFIG_FILE={explicit_env_path!r} does not exist or is not a file."
        )

    cfg_path = find_config_file(
        AGENT_CONFIG_FILE,
        search_paths=[REPO_ROOT],
        env_var="PTC_CONFIG_FILE",
    )
    if cfg_path is None:
        raise DaltonStartupConfigError(
            f"Agent configuration file '{AGENT_CONFIG_FILE}' not found."
        )
    try:
        loaded = load_yaml_config(str(cfg_path), use_cache=False)
        if not isinstance(loaded, dict):
            raise DaltonStartupConfigError(
                f"Agent configuration '{cfg_path}' must parse to a mapping, got {type(loaded).__name__}."
            )
        # Upstream load_yaml_config only overlays agent_config.<APP_ENV>.yaml when APP_ENV is set.
        # In Everflow local dev or CLI invocations where APP_ENV may be unset while Dalton is enabled,
        # we still overlay agent_config.dalton.yaml so startup role-policy enforcement always checks
        # the effective Everflow role assignments rather than the upstream base defaults.
        # In core profile, load_yaml_config already overlaid agent_config.core.yaml regardless of
        # whether APP_ENV=core or DALTON_PROFILE=core was used.
        if (
            profile is DeploymentProfile.EVERFLOW
            and not os.getenv("APP_ENV", "").strip()
            and cfg_path.name == AGENT_CONFIG_FILE
        ):
            dalton_overlay = cfg_path.with_name(f"{cfg_path.stem}.dalton{cfg_path.suffix}")
            if dalton_overlay.is_file():
                overlay_cfg = _read_yaml(dalton_overlay)
                if not isinstance(overlay_cfg, dict):
                    raise DaltonStartupConfigError(
                        f"Dalton overlay configuration '{dalton_overlay}' must parse to a mapping, "
                        f"got {type(overlay_cfg).__name__}."
                    )
                loaded = _deep_merge(loaded, overlay_cfg)
        return loaded
    except (DaltonStartupConfigError, ProfileConfigError):
        raise
    except Exception as exc:
        raise DaltonStartupConfigError(
            f"Failed to load or parse agent configuration '{cfg_path}': {exc}"
        ) from exc


def _resolve_assist_fallback_models_for_policy() -> list[str]:
    from src.dalton.settings import get_assist_fallback_models

    try:
        return get_assist_fallback_models()
    except Exception as exc:
        raise DaltonStartupConfigError(
            f"Failed to load assist_fallback_model from Dalton settings: {exc}"
        ) from exc


def check_roles(
    config: dict[str, Any] | None = None,
    *,
    assist_model: str | None = None,
    assist_fallback_models: list[str] | None = None,
    reviewer_models: list[str] | None = None,
    available_models: set[str] | None = None,
) -> list[dict[str, str]]:
    """Validate role model assignments against family independence, forbidden models, and text-only rules.

    Validates all roles (`lead`, `assist`, `background`, `compaction`, `fetch`, `fallback`,
    `subagent:*`, and `reviewer`). `assist` does not require tool calling (`require_tool_calling=False`),
    while `lead` and `subagent:*` require tool calling. Contributor OAuth variants (`L21`) are
    strictly forbidden in EVERY role.

    Returns a list of warning dicts `{"code", "role", "model", "message", "link"}`.
    """
    warnings: list[dict[str, str]] = []
    cfg = config if config is not None else _load_default_agent_config()
    llm_cfg = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else cfg
    lead_model = str(
        llm_cfg.get("name")
        or llm_cfg.get("preferred_model")
        or ""
    ).strip()

    if lead_model and is_forbidden_for_roles(lead_model):
        warnings.append(
            {
                "code": "role_forbidden_model",
                "role": "lead",
                "model": lead_model,
                "message": f"主模型 {lead_model} 为 contributor 训练贡献变体（L21），严禁用于任何基金研究角色。",
                "link": "/settings?tab=model",
            }
        )

    if lead_model and is_text_only(lead_model):
        warnings.append(
            {
                "code": "lead_text_only",
                "role": "lead",
                "model": lead_model,
                "message": f"主分析师模型 {lead_model} 为仅文本模型（不支持工具调用），将无法执行代码或调用数据源。",
                "link": "/settings?tab=model",
            }
        )

    if lead_model and available_models is not None and lead_model not in available_models:
        warnings.append(
            {
                "code": "role_model_unavailable",
                "role": "lead",
                "model": lead_model,
                "message": f"主分析师模型 {lead_model} 当前不可用（凭据未连接或供应商已关闭）。",
                "link": "/settings?tab=model",
            }
        )

    # Validate auxiliary LLM slots: assist, assist_fallback, background, compaction, fetch,
    # conversations.extractor_model (P0H §4 P3, P0J.J2).
    # Note: assist (and compaction/background/conversations.extractor_model) are non-tool
    # structured/background slots, so they do not require tool-calling, but they MUST NOT
    # use forbidden contributor models.
    from src.dalton.settings import get_dalton_settings

    assist_fb_models = (
        list(assist_fallback_models)
        if assist_fallback_models is not None
        else _resolve_assist_fallback_models_for_policy()
    )
    models_cfg = cfg.get("models") if isinstance(cfg.get("models"), dict) else {}
    eff_assist_model = (
        str(assist_model).strip()
        if assist_model is not None
        else str(
            llm_cfg.get("assist")
            or models_cfg.get("assist_model")
            or (get_dalton_settings().models.assist_model if config is None else "")
            or ""
        ).strip()
    )
    conv_cfg = cfg.get("conversations") if isinstance(cfg.get("conversations"), dict) else {}
    extractor_model = str(
        conv_cfg.get("extractor_model")
        or cfg.get("conversations.extractor_model")
        or llm_cfg.get("conversations.extractor_model")
        or llm_cfg.get("extractor_model")
        or (get_dalton_settings().conversations.extractor_model if config is None else "")
        or ""
    ).strip()

    aux_slots: list[tuple[str, str]] = [
        ("assist", eff_assist_model),
        *[("assist_fallback", fb_m) for fb_m in assist_fb_models],
        ("background", str(llm_cfg.get("background") or "").strip()),
        (
            "compaction",
            str(llm_cfg.get("compaction") or llm_cfg.get("summarization") or "").strip(),
        ),
        ("fetch", str(llm_cfg.get("fetch") or "").strip()),
        ("conversations.extractor_model", extractor_model),
    ]
    for role_name, role_model in aux_slots:
        if not role_model:
            continue
        if is_forbidden_for_roles(role_model):
            warnings.append(
                {
                    "code": "role_forbidden_model",
                    "role": role_name,
                    "model": role_model,
                    "message": f"角色 {role_name} 配置了禁用变体 {role_model}（contributor 变体可能用于训练，L21 严禁用于任何基金研究角色）。",
                    "link": "/settings?tab=model",
                }
            )
        if available_models is not None and role_model not in available_models:
            warnings.append(
                {
                    "code": "role_model_unavailable",
                    "role": role_name,
                    "model": role_model,
                    "message": f"角色 {role_name} 模型 {role_model} 当前不可用（凭据未配置或供应商已关闭）。",
                    "link": "/settings?tab=model",
                }
            )

    fallback_list = llm_cfg.get("fallback")
    if isinstance(fallback_list, list):
        for fb_item in fallback_list:
            fb_model = str(fb_item or "").strip()
            if not fb_model:
                continue
            if is_forbidden_for_roles(fb_model):
                warnings.append(
                    {
                        "code": "role_forbidden_model",
                        "role": "fallback",
                        "model": fb_model,
                        "message": f"后备模型列表包含禁用变体 {fb_model}（L21 严禁用于任何基金研究角色）。",
                        "link": "/settings?tab=model",
                    }
                )

    subagents_cfg = (
        (cfg.get("subagents") or {}).get("definitions")
        if isinstance(cfg.get("subagents"), dict)
        else (cfg.get("subagent_models") or {})
    )
    if isinstance(subagents_cfg, dict):
        for sub_name, sub_val in subagents_cfg.items():
            sub_model = (
                sub_val.get("model")
                if isinstance(sub_val, dict)
                else str(sub_val or "")
            )
            if not sub_model:
                continue
            if is_forbidden_for_roles(sub_model):
                warnings.append(
                    {
                        "code": "role_forbidden_model",
                        "role": f"subagent:{sub_name}",
                        "model": sub_model,
                        "message": f"子智能体 {sub_name} 配置了禁用变体 {sub_model}（contributor 变体可能用于训练，L21 严禁用于基金研究）。",
                        "link": "/plugins?tab=subagents",
                    }
                )
            if is_text_only(sub_model):
                warnings.append(
                    {
                        "code": "subagent_text_only",
                        "role": f"subagent:{sub_name}",
                        "model": sub_model,
                        "message": f"子智能体 {sub_name} 配置了仅文本模型 {sub_model}，无法调用工具。",
                        "link": "/plugins?tab=subagents",
                    }
                )
            if available_models is not None and sub_model not in available_models:
                warnings.append(
                    {
                        "code": "role_model_unavailable",
                        "role": f"subagent:{sub_name}",
                        "model": sub_model,
                        "message": f"子智能体 {sub_name} 模型 {sub_model} 当前不可用（凭据未配置或供应商已关闭）。",
                        "link": "/settings?tab=model",
                    }
                )

    from src.dalton.settings import get_dalton_settings

    eff_reviewer_models = (
        list(reviewer_models)
        if reviewer_models is not None
        else list(get_dalton_settings().publish.reviewer_models)
    )
    for rev_cand in eff_reviewer_models:
        if is_forbidden_for_roles(rev_cand):
            warnings.append(
                {
                    "code": "role_forbidden_model",
                    "role": "reviewer",
                    "model": rev_cand,
                    "message": f"审稿候选模型 {rev_cand} 为 contributor 禁用变体（L21 严禁用于任何基金研究角色）。",
                    "link": "/settings?tab=model",
                }
            )

    core_mode = is_core_profile()

    if lead_model and not core_mode:
        chosen_reviewer = _select_independent_reviewer_from_candidates(
            lead_model,
            eff_reviewer_models,
            require_tool_calling=False,
            available_models=available_models,
        )
        if chosen_reviewer is None:
            warnings.append(
                {
                    "code": "reviewer_same_family",
                    "role": "reviewer",
                    "model": lead_model,
                    "message": f"审稿候选列表中没有与主模型 {lead_model}（家族 {family_of(lead_model)}）不同家族且可用的模型。",
                    "link": "/settings?tab=model",
                }
            )

    # Number audit chain (publish.audit_model + audit_fallback_models): same
    # contributor ban as every role, and it must be configured at all in Everflow
    # (in Core profile, an unset audit_model follows the customer's configured model).
    pub_cfg = get_dalton_settings().publish
    audit_chain = [m for m in [pub_cfg.audit_model, *pub_cfg.audit_fallback_models] if m and m.strip()]
    if not (pub_cfg.audit_model or "").strip() and not core_mode:
        warnings.append(
            {
                "code": "audit_model_missing",
                "role": "audit",
                "model": "",
                "message": "数字核验未配置模型：请在 dalton.yaml 的 publish.audit_model 指定。",
                "link": "/settings?tab=model",
            }
        )
    for a_m in audit_chain:
        if is_forbidden_for_roles(a_m):
            warnings.append(
                {
                    "code": "role_forbidden_model",
                    "role": "audit",
                    "model": a_m,
                    "message": f"数字核验模型 {a_m} 为 contributor 禁用变体（L21 严禁用于任何基金研究角色）。",
                    "link": "/settings?tab=model",
                }
            )
        elif available_models is not None and a_m not in available_models:
            warnings.append(
                {
                    "code": "role_model_unavailable",
                    "role": "audit",
                    "model": a_m,
                    "message": f"数字核验模型 {a_m} 当前不可用（凭据未配置或供应商已关闭）。",
                    "link": "/settings?tab=model",
                }
            )

    if not core_mode:
        for fb_m in assist_fb_models:
            fb_fam = family_of(fb_m)
            if fb_fam == "unknown":
                continue
            for rev_cand in eff_reviewer_models:
                if family_of(rev_cand) == fb_fam:
                    warnings.append(
                        {
                            "code": "assist_fallback_same_family",
                            "role": "assist_fallback",
                            "model": fb_m,
                            "message": (
                                f"assist 备选模型 {fb_m} 与审稿候选模型 {rev_cand} "
                                f"同属家族 {fb_fam}（违反 L20 跨家族独立原则）。"
                            ),
                            "link": "/settings?tab=model",
                        }
                    )
                    break

    return warnings


def enforce_role_policy_on_startup(config: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Validate role assignments at startup; raise `RolePolicyError` on L20/L21 violation (P0H §4 P3, P0J.J2)."""
    warnings = check_roles(config)
    forbidden = [
        w
        for w in warnings
        if w.get("code") in ("role_forbidden_model", "assist_fallback_same_family")
    ]
    if forbidden:
        details = "; ".join(f"{w.get('role')}={w.get('model')}: {w.get('message')}" for w in forbidden)
        raise RolePolicyError(f"Role policy violation on startup (L20/L21): {details}")
    return warnings

