"""Model family resolution, text-only detection, and role policy checks (P0.6)."""

from __future__ import annotations

from typing import Any

from src.dalton.llm.openclaw_catalog import infer_family_from_model_id
from src.dalton.llm.overlay import load_overlay_data


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


def family_of(model_key: str | None) -> str:
    """Return canonical vendor family for `model_key` (e.g., `anthropic-claude`, `xai-grok`).

    For `ocb-*` keys, reads `dalton.family` from `openclaw_overlay.json` first.
    For native keys (e.g. `claude-opus-5-5-oauth`), strips `-oauth`/`-byok` and uses
    the prefix table. Returns `'unknown'` if unrecognized.
    """
    if not model_key:
        return "unknown"
    key = str(model_key).strip()
    if not key:
        return "unknown"

    by_key, by_ref = _overlay_provider_index()
    if key in by_key:
        fam = (by_key[key].get("dalton") or {}).get("family")
        if fam:
            return str(fam)
    if key in by_ref:
        fam = (by_ref[key].get("dalton") or {}).get("family")
        if fam:
            return str(fam)

    # Also handle ocb-<provider>-<model> when not yet in overlay
    if key.startswith("ocb-"):
        parts = key[4:].split("-", 1)
        if len(parts) == 2:
            # Try matching known prefixes anywhere after provider prefix
            rest = key[4:]
            for prefix in (
                "claude-",
                "gemini-",
                "gpt-oss-",
                "gpt-",
                "grok-",
                "deepseek-",
                "glm-",
                "muse-",
            ):
                idx = rest.find(prefix)
                if idx != -1:
                    return infer_family_from_model_id(rest[idx:])

    return infer_family_from_model_id(key)


def is_text_only(model_key: str | None) -> bool:
    """Return True if `model_key` does not support tool calling."""
    if not model_key:
        return False
    key = str(model_key).strip()
    by_key, by_ref = _overlay_provider_index()
    entry = by_key.get(key) or by_ref.get(key)
    if entry is not None:
        dalton_meta = entry.get("dalton") or {}
        if "tool_calling" in dalton_meta:
            return not bool(dalton_meta["tool_calling"])

    lower = key.lower()
    for text_only_gw in ("claude-cli-gateway", "antigravity-cli-gateway", "muse-cli-gateway"):
        if text_only_gw in lower:
            return True
    return False


def select_independent_reviewer(
    lead_model: str,
    candidates: list[str] | None = None,
    *,
    require_tool_calling: bool = False,
) -> str | None:
    """Pick the first candidate whose `family_of()` differs from `lead_model` and is not `'unknown'`."""
    if candidates is None:
        from src.dalton.settings import get_dalton_settings

        candidates = list(get_dalton_settings().publish.reviewer_models)

    lead_family = family_of(lead_model)
    by_key, _ = _overlay_provider_index()

    for cand in candidates:
        cand_family = family_of(cand)
        if cand_family == "unknown" or lead_family == "unknown":
            if cand_family == "unknown":
                continue
        if cand_family == lead_family:
            continue
        if require_tool_calling and is_text_only(cand):
            continue
        # If it is an ocb-* model and overlay is populated, check it exists or is configured
        if cand.startswith("ocb-") and by_key and cand not in by_key:
            continue
        return cand
    return None


def check_roles(
    config: dict[str, Any] | None = None,
    *,
    reviewer_models: list[str] | None = None,
) -> list[dict[str, str]]:
    """Validate role model assignments against family independence and text-only rules.

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
            if sub_model and is_text_only(sub_model):
                warnings.append(
                    {
                        "code": "subagent_text_only",
                        "role": f"subagent:{sub_name}",
                        "model": sub_model,
                        "message": f"子智能体 {sub_name} 配置了仅文本模型 {sub_model}，无法调用工具。",
                        "link": "/plugins?tab=subagents",
                    }
                )

    chosen_reviewer = select_independent_reviewer(
        lead_model, reviewer_models, require_tool_calling=False
    )
    if chosen_reviewer is None:
        warnings.append(
            {
                "code": "reviewer_same_family",
                "role": "reviewer",
                "model": lead_model,
                "message": f"审稿候选列表中没有与主模型 {lead_model}（家族 {family_of(lead_model)}）不同家族的模型。",
                "link": "/settings?tab=model",
            }
        )

    return warnings
