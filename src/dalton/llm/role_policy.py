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
    user_id: str = "local-dev-user",
    *,
    require_tool_calling: bool = False,
) -> dict[str, dict[str, Any]]:
    """Return availability status and reason for every model key in the merged catalog."""
    from src.llms.llm import ModelConfig

    mc = ModelConfig()
    llm_cfg = mc.llm_config or {}
    flat_providers = mc.flat_providers or {}
    disabled = get_disabled_providers()
    broker_enabled = is_broker_models_enabled()
    meta_map = load_dalton_meta()

    # Query OAuth statuses for the 4 subscription providers
    oauth_connected: dict[str, bool] = {}
    try:
        from src.server.database.oauth_tokens import get_oauth_status

        for prov_key in _OAUTH_PROVIDER_TOKEN_KEYS:
            try:
                st = await get_oauth_status(user_id, prov_key)
                oauth_connected[prov_key] = bool(st and st.get("connected"))
            except Exception:
                oauth_connected[prov_key] = False
    except Exception:
        pass

    # Query user BYOK keys if available
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
    user_id: str = "local-dev-user",
    *,
    require_tool_calling: bool = False,
) -> set[str]:
    """Return the set of model keys currently available for `user_id` (P0N.5 #4)."""
    report = await explain_model_availability(
        user_id, require_tool_calling=require_tool_calling
    )
    return {k for k, v in report.items() if v.get("available")}


def select_independent_reviewer(
    lead_model: str,
    candidates: list[str] | None = None,
    *,
    require_tool_calling: bool = False,
    available_models: set[str] | None = None,
) -> str | None:
    """Pick the first candidate whose `family_of()` differs from `lead_model` and is available."""
    if candidates is None:
        from src.dalton.settings import get_dalton_settings

        candidates = list(get_dalton_settings().publish.reviewer_models)

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


def check_roles(
    config: dict[str, Any] | None = None,
    *,
    reviewer_models: list[str] | None = None,
    available_models: set[str] | None = None,
) -> list[dict[str, str]]:
    """Validate role model assignments against family independence, forbidden models, and text-only rules.

    Returns a list of warning dicts `{"code", "role", "model", "message", "link"}`.
    """
    warnings: list[dict[str, str]] = []
    cfg = config or {}
    llm_cfg = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else cfg
    lead_model = str(
        llm_cfg.get("name")
        or llm_cfg.get("preferred_model")
        or "claude-opus-5-5-oauth"
    )

    if is_forbidden_for_roles(lead_model):
        warnings.append(
            {
                "code": "role_forbidden_model",
                "role": "lead",
                "model": lead_model,
                "message": f"主模型 {lead_model} 为 contributor 训练贡献变体（L21），严禁用于任何基金研究角色。",
                "link": "/settings?tab=model",
            }
        )

    if is_text_only(lead_model):
        warnings.append(
            {
                "code": "lead_text_only",
                "role": "lead",
                "model": lead_model,
                "message": f"主分析师模型 {lead_model} 为仅文本模型（不支持工具调用），将无法执行代码或调用数据源。",
                "link": "/settings?tab=model",
            }
        )

    if available_models is not None and lead_model not in available_models:
        warnings.append(
            {
                "code": "role_model_unavailable",
                "role": "lead",
                "model": lead_model,
                "message": f"主分析师模型 {lead_model} 当前不可用（凭据未连接或供应商已关闭）。",
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

    chosen_reviewer = select_independent_reviewer(
        lead_model,
        reviewer_models,
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

    return warnings
