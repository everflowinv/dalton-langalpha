"""Model family resolution, text-only detection, availability, and role policy checks (P0.6 / P0N.5)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from src.dalton.llm.openclaw_catalog import FAMILY_PREFIX_TABLE, infer_family_from_model_id
from src.dalton.llm.overlay import load_dalton_meta, load_overlay_data
from src.dalton.settings import get_disabled_providers, is_broker_models_enabled

REPO_ROOT = Path(__file__).resolve().parents[3]
LATEST_NATIVE_SMOKE_PATH = REPO_ROOT / "var" / "dalton" / "smoke" / "latest_native.json"

_OAUTH_PROVIDER_TOKEN_KEYS: dict[str, str] = {
    "claude-oauth": "claude-oauth",
    "codex-oauth": "codex-oauth",
    "antigravity-oauth": "antigravity-oauth",
    "muse-oauth": "muse-oauth",
    "xai-oauth": "xai-oauth",
}


def _overlay_provider_index() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return `(by_key, by_ref)` of provider model entries from `openclaw_overlay.json`."""
    overlay = load_overlay_data() or {}
    models_map = overlay.get("models") or {}
    prov_models = ((overlay.get("providers") or {}).get("models") or {}).get(
        "openclaw-broker", []
    )
    by_ref: dict[str, dict[str, Any]] = {}
    for pm in prov_models:
        if isinstance(pm, dict) and pm.get("id"):
            by_ref[str(pm["id"])] = pm

    by_key: dict[str, dict[str, Any]] = {}
    for key, mcfg in models_map.items():
        if isinstance(mcfg, dict):
            ref = str(mcfg.get("model_id") or "")
            if ref in by_ref:
                by_key[str(key)] = by_ref[ref]
    return by_key, by_ref


def _merged_model_entry(model_key: str) -> dict[str, Any] | None:
    """Return the merged `ModelConfig.get_model_config(model_key)` entry if present."""
    try:
        from src.llms.llm import ModelConfig

        entry = ModelConfig.get_model_config(model_key)
        if isinstance(entry, dict):
            return entry
    except Exception:
        pass
    return None


def family_of(model_key: str | None) -> str:
    """Return canonical underlying vendor family for `model_key` (P0N.5 L20).

    Lookup order:
    1. `load_dalton_meta()[key]["family"]` (includes both native_overlay and openclaw_overlay)
    2. Merged `ModelConfig.get_model_config(key)["model_id"]` via `infer_family_from_model_id`
    3. `key` itself via `infer_family_from_model_id` (including `ocb-` prefix handling)
    4. `'unknown'`
    """
    if not model_key:
        return "unknown"
    key = str(model_key).strip()
    if not key:
        return "unknown"

    # 1. Explicit dalton_meta family
    meta_map = load_dalton_meta()
    if key in meta_map and isinstance(meta_map[key], dict):
        fam = meta_map[key].get("family")
        if fam and str(fam) != "unknown":
            return str(fam)

    by_key, by_ref = _overlay_provider_index()
    if key in by_key:
        fam = (by_key[key].get("dalton") or {}).get("family")
        if fam and str(fam) != "unknown":
            return str(fam)
    if key in by_ref:
        fam = (by_ref[key].get("dalton") or {}).get("family")
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

    # 3. Key itself (including ocb-<provider>-<model>)
    if key.startswith("ocb-"):
        rest = key[4:].lower()
        for prefix, _fam in FAMILY_PREFIX_TABLE:
            idx = rest.find(prefix)
            if idx != -1:
                return infer_family_from_model_id(rest[idx:])

    return infer_family_from_model_id(key)


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

    meta_map = load_dalton_meta()
    if key in meta_map and isinstance(meta_map[key], dict):
        if "tool_calling" in meta_map[key] and not bool(meta_map[key]["tool_calling"]):
            return True

    by_key, by_ref = _overlay_provider_index()
    entry = by_key.get(key) or by_ref.get(key)
    if entry is not None:
        dalton_meta = entry.get("dalton") or {}
        if "tool_calling" in dalton_meta:
            return not bool(dalton_meta["tool_calling"])

    # Check latest_native.json smoke results (N3 == "fail" -> text-only)
    native_smoke = _load_latest_native_smoke()
    smoke_entry = native_smoke.get(key)
    if isinstance(smoke_entry, dict):
        checks = smoke_entry.get("checks") or {}
        if isinstance(checks, dict) and checks.get("N3") == "fail":
            return True

    lower = key.lower()
    for text_only_gw in ("claude-cli-gateway", "antigravity-cli-gateway", "muse-cli-gateway"):
        if text_only_gw in lower:
            return True
    return False


def is_forbidden_for_roles(model_key: str | None) -> bool:
    """Return True if `model_key` is marked `forbidden_for_roles` (e.g. contributor OAuth models, L21)."""
    if not model_key:
        return False
    key = str(model_key).strip()
    meta_map = load_dalton_meta()
    meta = meta_map.get(key)
    if isinstance(meta, dict) and bool(meta.get("forbidden_for_roles")):
        return True
    if "contributor" in key.lower():
        return True
    return False


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
    broker_enabled = is_broker_models_enabled()
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
        if provider == "openclaw-broker" or key.startswith("ocb-"):
            if not broker_enabled:
                reasons.append("broker_disabled")
        else:
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
    by_key, _ = _overlay_provider_index()

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
        if cand.startswith("ocb-"):
            if not is_broker_models_enabled():
                continue
            if by_key and cand not in by_key:
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


class DaltonStartupConfigError(RuntimeError):
    """Fatal Dalton startup configuration or schema migration error (P0J.J3)."""


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
        # In Dalton local dev or CLI invocations where APP_ENV may be unset while Dalton is enabled,
        # we still overlay agent_config.dalton.yaml so startup role-policy enforcement always checks
        # the effective Dalton role assignments rather than the upstream base defaults.
        if not os.getenv("APP_ENV", "").strip() and cfg_path.name == AGENT_CONFIG_FILE:
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
    except DaltonStartupConfigError:
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

    if lead_model:
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
    # contributor ban as every role, and it must be configured at all.
    pub_cfg = get_dalton_settings().publish
    audit_chain = [m for m in [pub_cfg.audit_model, *pub_cfg.audit_fallback_models] if m and m.strip()]
    if not (pub_cfg.audit_model or "").strip():
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

