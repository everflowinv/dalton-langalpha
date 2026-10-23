"""OpenClaw model catalog normalization, overlay builder, and diffing (P0.4 & P0.7).

Never reads or emits any credential fields (apiKey, headers, auth, token,
secret, password, gateway.*, credentials).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
OVERLAY_PATH = Path(__file__).resolve().parent / "openclaw_overlay.json"
OVERRIDES_PATH = Path(__file__).resolve().parent / "openclaw_overrides.yaml"
UPSTREAM_MODELS_PATH = REPO_ROOT / "src" / "llms" / "manifest" / "models.json"
UPSTREAM_PROVIDERS_PATH = REPO_ROOT / "src" / "llms" / "manifest" / "providers.json"

FAMILY_PREFIX_TABLE: tuple[tuple[str, str], ...] = (
    ("claude-", "anthropic-claude"),
    ("gemini-", "google-gemini"),
    ("gpt-oss-", "openai-gpt"),
    ("gpt-", "openai-gpt"),
    ("grok-", "xai-grok"),
    ("deepseek-", "deepseek"),
    ("glm-", "zai-glm"),
    ("muse-", "meta-muse"),
    ("kimi-", "moonshot-kimi"),
    ("qwen", "alibaba-qwen"),
    ("minimax-", "minimax"),
)

_FORBIDDEN_CREDENTIAL_KEYS = frozenset(
    {
        "apiKey",
        "api_key",
        "headers",
        "auth",
        "token",
        "secret",
        "password",
        "credentials",
        "bearer",
    }
)

_KEY_SANITIZE_RE = re.compile(r"[^a-z0-9.-]+")


def make_model_key(ref: str) -> str:
    """Convert `provider/model` into `ocb-<provider>-<model>` using `[a-z0-9.-]`."""
    raw = f"ocb-{ref.replace('/', '-').lower()}"
    cleaned = _KEY_SANITIZE_RE.sub("-", raw).strip("-")
    return re.sub(r"-{2,}", "-", cleaned)


def infer_family_from_model_id(bare_model_id: str, override_family: str | None = None) -> str:
    """Derive canonical vendor family from bare model id or explicit override."""
    if override_family:
        return str(override_family)
    lower = bare_model_id.lower().strip()
    if "/" in lower:
        lower = lower.split("/", 1)[1]
    for suffix in ("-oauth", "-byok"):
        if lower.endswith(suffix):
            lower = lower[: -len(suffix)]
    for prefix, family in FAMILY_PREFIX_TABLE:
        if lower.startswith(prefix):
            return family
    return "unknown"


def load_overrides(path: Path | None = None) -> dict[str, dict[str, Any]]:
    target = path or OVERRIDES_PATH
    if not target.exists():
        return {}
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): dict(v or {}) for k, v in raw.items() if isinstance(v, dict)}


def extract_v1_rate_cards_from_openclaw_config(config_path: Path) -> dict[str, dict[str, Any]]:
    """Read ONLY whitelisted rateCard/maxTokens fields from v1 broker profiles in openclaw.json."""
    if not config_path.exists():
        return {}
    data = json.loads(config_path.read_text(encoding="utf-8"))
    entries = ((data.get("plugins") or {}).get("entries") or {})
    v1_cfg = (entries.get("dalton-openclaw-model-broker") or {}).get("config") or {}
    profiles = v1_cfg.get("profiles") or []
    result: dict[str, dict[str, Any]] = {}
    for prof in profiles:
        if not isinstance(prof, dict):
            continue
        model_ref = prof.get("model")
        if not isinstance(model_ref, str) or not model_ref:
            continue
        controls = prof.get("providerControls") or {}
        rc = controls.get("rateCard") if isinstance(controls, dict) else None
        entry: dict[str, Any] = {
            "id": prof.get("id"),
            "model": model_ref,
            "maxTokens": prof.get("maxTokens"),
            "thinkingLevel": prof.get("thinkingLevel"),
        }
        if isinstance(rc, dict):
            parsed_rc: dict[str, float] = {}
            for src_k, dst_k in (
                ("inputUsdPerMillion", "input"),
                ("outputUsdPerMillion", "output"),
                ("cachedInputUsdPerMillion", "cached_input"),
                ("cacheWriteUsdPerMillion", "cache_5m"),
            ):
                val = rc.get(src_k)
                if val is not None:
                    try:
                        parsed_rc[dst_k] = float(val)
                    except (TypeError, ValueError):
                        pass
            if parsed_rc:
                entry["rateCard"] = parsed_rc
        result[model_ref] = entry
    return result


def load_upstream_bare_pricing() -> dict[str, dict[str, Any]]:
    """Load fallback pricing by bare model id from upstream providers.json."""
    if not UPSTREAM_PROVIDERS_PATH.exists():
        return {}
    data = json.loads(UPSTREAM_PROVIDERS_PATH.read_text(encoding="utf-8"))
    models_by_provider = data.get("models") or {}
    out: dict[str, dict[str, Any]] = {}
    for _prov, items in models_by_provider.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("id") and isinstance(item.get("pricing"), dict):
                out.setdefault(str(item["id"]), copy.deepcopy(item["pricing"]))
    return out


# Upstream reasoning-level spellings → catalog effort names (shared with the
# native model sync, which maps per-provider levels onto the same scale).
_REASONING_LEVEL_MAP: dict[str, str] = {
    "off": "none",
    "none": "none",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}


def _normalize_reasoning_efforts(levels: list[str] | None) -> list[str]:
    if not levels:
        return ["low", "medium", "high"]
    seen: list[str] = []
    for lv in levels:
        mapped = _REASONING_LEVEL_MAP.get(str(lv).lower())
        if mapped and mapped not in seen:
            seen.append(mapped)
    return seen or ["low", "medium", "high"]


def _build_tiered_pricing(
    tiers: list[dict[str, Any]],
    override_max: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Convert OpenClaw `tieredPricing` into LangAlpha `input_tiers`/`output_tiers`."""
    input_tiers: list[dict[str, Any]] = []
    output_tiers: list[dict[str, Any]] = []
    top_cache_5m = 0.0

    for idx, tier in enumerate(tiers):
        if idx + 1 < len(tiers):
            next_range = tiers[idx + 1].get("range") or []
            next_start = int(next_range[0]) if next_range else 0
            max_tok: int | None = (next_start // 1000) * 1000
        else:
            max_tok = None

        in_rate = float(tier.get("input", 0.0) or 0.0)
        out_rate = float(tier.get("output", 0.0) or 0.0)
        cache_read = float(tier.get("cacheRead", 0.0) or 0.0)
        cache_write = float(tier.get("cacheWrite", 0.0) or 0.0)

        if override_max and idx == 0:
            in_rate = max(in_rate, override_max.get("input", 0.0))
            out_rate = max(out_rate, override_max.get("output", 0.0))
            cache_read = max(cache_read, override_max.get("cached_input", 0.0))
            cache_write = max(cache_write, override_max.get("cache_5m", 0.0))

        in_tier: dict[str, Any] = {
            "max_tokens": max_tok,
            "rate": in_rate,
            "cached_input": cache_read,
        }
        if cache_write > 0:
            in_tier["cache_5m"] = cache_write
            if idx == 0:
                top_cache_5m = cache_write
        input_tiers.append(in_tier)
        output_tiers.append({"max_tokens": max_tok, "rate": out_rate})

    card: dict[str, Any] = {
        "input_tiers": input_tiers,
        "output_tiers": output_tiers,
        "input_pricing_mode": "input_dependent",
        "output_pricing_mode": "input_dependent",
        "unit": "per_1m_tokens",
    }
    if top_cache_5m > 0:
        card["cache_5m"] = top_cache_5m
    return card


def _normalize_model_pricing(
    ref: str,
    bare_id: str,
    broker_cost: dict[str, Any] | None,
    v1_rate_card: dict[str, float] | None,
    dalton_card: dict[str, float] | None,
    upstream_fallback: dict[str, Any] | None,
    preserved_card: dict[str, Any] | None,
    discrepancies: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Combine pricing sources conservatively (taking maximum on disagreements)."""
    sources_used: list[str] = []
    component_candidates: dict[str, dict[str, float]] = {
        "input": {},
        "output": {},
        "cached_input": {},
        "cache_5m": {},
    }

    if isinstance(broker_cost, dict) and (
        float(broker_cost.get("input") or 0.0) > 0
        or float(broker_cost.get("output") or 0.0) > 0
    ):
        sources_used.append("broker.cost")
        if broker_cost.get("input") is not None:
            component_candidates["input"]["broker.cost"] = float(broker_cost["input"])
        if broker_cost.get("output") is not None:
            component_candidates["output"]["broker.cost"] = float(broker_cost["output"])
        if broker_cost.get("cacheRead") is not None:
            component_candidates["cached_input"]["broker.cost"] = float(broker_cost["cacheRead"])
        if broker_cost.get("cacheWrite") is not None and float(broker_cost["cacheWrite"]) > 0:
            component_candidates["cache_5m"]["broker.cost"] = float(broker_cost["cacheWrite"])

    if isinstance(v1_rate_card, dict) and v1_rate_card:
        sources_used.append("openclaw.rateCard")
        for field in ("input", "output", "cached_input", "cache_5m"):
            if field in v1_rate_card and v1_rate_card[field] is not None:
                val = float(v1_rate_card[field])
                if field != "cache_5m" or val > 0:
                    component_candidates[field]["openclaw.rateCard"] = val

    if isinstance(dalton_card, dict) and dalton_card:
        sources_used.append("dalton.priceCard")
        if dalton_card.get("input_per_million_usd") is not None:
            component_candidates["input"]["dalton.priceCard"] = float(
                dalton_card["input_per_million_usd"]
            )
        if dalton_card.get("output_per_million_usd") is not None:
            component_candidates["output"]["dalton.priceCard"] = float(
                dalton_card["output_per_million_usd"]
            )

    if isinstance(preserved_card, dict) and preserved_card:
        for field in ("input", "output", "cached_input", "cache_5m"):
            if preserved_card.get(field) is not None:
                val = float(preserved_card[field])
                if field != "cache_5m" or val > 0:
                    component_candidates[field]["preserved.rateCard"] = val
        if "openclaw.rateCard" not in sources_used:
            sources_used.append("openclaw.rateCard")

    chosen_components: dict[str, float] = {}
    for field, cand in component_candidates.items():
        if not cand:
            continue
        max_val = max(cand.values())
        chosen_components[field] = max_val
        distinct_vals = {round(v, 6) for v in cand.values()}
        if len(distinct_vals) > 1:
            discrepancies.append(
                {
                    "ref": ref,
                    "field": field,
                    "values": cand,
                    "chosen": max_val,
                }
            )

    tiered = broker_cost.get("tieredPricing") if isinstance(broker_cost, dict) else None
    if isinstance(tiered, list) and len(tiered) > 0:
        return _build_tiered_pricing(tiered, override_max=chosen_components), sources_used

    if "input" in chosen_components or "output" in chosen_components:
        card: dict[str, Any] = {
            "input": chosen_components.get("input", 0.0),
            "output": chosen_components.get("output", 0.0),
            "cached_input": chosen_components.get("cached_input", 0.0),
            "unit": "per_1m_tokens",
        }
        if chosen_components.get("cache_5m", 0.0) > 0:
            card["cache_5m"] = chosen_components["cache_5m"]
        return card, sources_used

    if isinstance(upstream_fallback, dict) and upstream_fallback:
        return copy.deepcopy(upstream_fallback), ["upstream.providers"]

    return None, []


def assert_no_credentials(obj: Any, path: str = "root") -> None:
    """Raise ValueError if any forbidden credential key appears anywhere in obj."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k) in _FORBIDDEN_CREDENTIAL_KEYS:
                raise ValueError(f"Forbidden credential field {k!r} at {path}")
            assert_no_credentials(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            assert_no_credentials(item, f"{path}[{i}]")


def build_overlay(
    *,
    catalog: dict[str, Any],
    overrides: dict[str, dict[str, Any]] | None = None,
    v1_rate_cards: dict[str, dict[str, Any]] | None = None,
    price_cards: dict[str, dict[str, Any]] | None = None,
    current_overlay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build complete `openclaw_overlay.json` structure from broker `/models?detail=1`."""
    if not isinstance(catalog, dict) or not isinstance(catalog.get("data"), list):
        raise ValueError("catalog_invalid: missing data list")
    items = catalog["data"]
    if len(items) == 0:
        raise ValueError("catalog_invalid: model catalog is empty")

    overrides_map = overrides if overrides is not None else load_overrides()
    v1_map = v1_rate_cards or {}
    dalton_cards = price_cards or {}
    upstream_fallback_map = load_upstream_bare_pricing()

    # If syncing inside container without openclaw.json, preserve previous rateCard prices
    preserved_by_ref: dict[str, dict[str, Any]] = {}
    preserved_source = False
    if not v1_map and isinstance(current_overlay, dict):
        prev_meta = current_overlay.get("meta") or {}
        if prev_meta.get("source") == "broker+openclaw_config":
            prev_models = ((current_overlay.get("providers") or {}).get("models") or {}).get(
                "openclaw-broker"
            ) or []
            for pm in prev_models:
                if not isinstance(pm, dict):
                    continue
                dalton_meta = pm.get("dalton") or {}
                ref = dalton_meta.get("openclaw_ref") or pm.get("id")
                sources = dalton_meta.get("price_sources") or []
                if ref and "openclaw.rateCard" in sources:
                    card = pm.get("notional_pricing") or pm.get("pricing")
                    if isinstance(card, dict) and "input" in card:
                        preserved_by_ref[str(ref)] = card
                        preserved_source = True

    models_out: dict[str, dict[str, Any]] = {}
    provider_models_out: list[dict[str, Any]] = []
    discrepancies: list[dict[str, Any]] = []
    warnings: list[str] = []
    hidden_list: list[dict[str, str]] = []

    if preserved_source:
        warnings.append(
            "Preserved higher rateCard pricing from previous host sync (broker+openclaw_config)."
        )

    visible_count = 0
    hidden_count = 0
    text_only_count = 0

    for raw in items:
        if not isinstance(raw, dict):
            continue
        ref = str(raw.get("ref") or raw.get("id") or "").strip()
        if not ref or "/" not in ref:
            continue
        provider, bare_id = ref.split("/", 1)
        key = make_model_key(ref)
        ovr = overrides_map.get(ref, {})
        caps = (
            raw.get("dalton_capabilities")
            if isinstance(raw.get("dalton_capabilities"), dict)
            else {}
        )

        name = str(raw.get("name") or caps.get("name") or bare_id)
        is_cli_text_only = provider in (
            "claude-cli-gateway",
            "antigravity-cli-gateway",
            "muse-cli-gateway",
        )
        tool_calling = bool(
            ovr.get(
                "tool_calling",
                False
                if is_cli_text_only
                else raw.get("toolCalling", caps.get("supports_tools", True)),
            )
        )
        if not tool_calling:
            text_only_count += 1

        display_name = str(ovr.get("display_name") or f"{name} · OpenClaw")
        if not tool_calling and "仅文本" not in display_name:
            display_name = f"{display_name}（仅文本）"

        family = infer_family_from_model_id(bare_id, ovr.get("family"))
        if family == "unknown":
            warnings.append(f"Model {ref} ({key}) has unknown vendor family.")

        billing = str(
            ovr.get("billing")
            or raw.get("billing")
            or ("subscription" if is_cli_text_only else "cash")
        )
        v1_prof = v1_map.get(ref) or {}
        v1_rc = v1_prof.get("rateCard") if isinstance(v1_prof, dict) else None
        ovr_pricing = ovr.get("pricing") if isinstance(ovr.get("pricing"), dict) else None
        if ovr_pricing:
            v1_rc = {**(v1_rc or {}), **ovr_pricing}
        raw_cost = (
            raw.get("cost")
            if isinstance(raw.get("cost"), dict)
            else (
                caps.get("cost_per_million_usd")
                if isinstance(caps.get("cost_per_million_usd"), dict)
                else None
            )
        )

        norm_pricing, price_sources = _normalize_model_pricing(
            ref=ref,
            bare_id=bare_id,
            broker_cost=raw_cost,
            v1_rate_card=v1_rc,
            dalton_card=dalton_cards.get(ref),
            upstream_fallback=upstream_fallback_map.get(bare_id),
            preserved_card=preserved_by_ref.get(ref),
            discrepancies=discrepancies,
        )

        visible = bool(ovr.get("visible", True))
        hide_reason: str | None = None
        if "visible" in ovr and not ovr["visible"]:
            hide_reason = "hidden_by_override"

        if billing == "subscription":
            pricing_obj: dict[str, Any] = {
                "pricing_type": "subscription",
                "unit": "per_1m_tokens",
            }
            notional_pricing = norm_pricing
        else:
            notional_pricing = None
            if norm_pricing is None:
                visible = False
                hide_reason = hide_reason or "unpriced"
                warnings.append(
                    f"Cash model {ref} ({key}) has no pricing card; forced visible=false."
                )
                pricing_obj = {
                    "input": 0.0,
                    "output": 0.0,
                    "cached_input": 0.0,
                    "unit": "per_1m_tokens",
                }
            else:
                pricing_obj = norm_pricing
                cache_5m_val = float(pricing_obj.get("cache_5m", 0.0) or 0.0)
                if cache_5m_val > 0:
                    warnings.append(
                        f"Cash model {ref} ({key}) has cache_5m={cache_5m_val} > 0; "
                        "verify cache_write_tokens metering."
                    )

        if visible:
            visible_count += 1
        else:
            hidden_count += 1
            hidden_list.append(
                {"key": key, "openclaw_ref": ref, "reason": hide_reason or "hidden"}
            )

        context_window = int(
            raw.get("contextWindow")
            or caps.get("context_window")
            or ovr.get("context")
            or 128000
        )
        max_tokens = int(
            raw.get("maxTokens")
            or caps.get("max_tokens")
            or v1_prof.get("maxTokens")
            or ovr.get("max_tokens")
            or 32000
        )
        raw_inputs = raw.get("input") or caps.get("input_modalities") or ["text"]
        input_modalities = [str(m) for m in raw_inputs if str(m) in ("text", "image", "pdf")]
        if not input_modalities:
            input_modalities = ["text"]

        is_reasoning = bool(raw.get("reasoning", caps.get("reasoning", False)))
        model_entry: dict[str, Any] = {
            "model_id": ref,
            "provider": "openclaw-broker",
            "display_name": display_name,
            "visible": visible,
            "speed": int(ovr.get("speed", 3)),
            "intelligence": int(ovr.get("intelligence", 4)),
            "context": context_window,
            "input_modalities": input_modalities,
            "prompt_guidance": str(ovr.get("prompt_guidance", "lean")),
            "parameters": {"max_tokens": max_tokens},
        }
        if is_reasoning:
            efforts = _normalize_reasoning_efforts(raw.get("reasoningLevels"))
            default_effort = "high" if "high" in efforts else efforts[-1]
            model_entry["reasoning"] = {
                "efforts": efforts,
                "default": default_effort,
                "write": "parameters.reasoning_effort",
            }
        if not tool_calling:
            model_entry["profile"] = {"tool_calling": False}

        models_out[key] = model_entry

        provider_models_out.append(
            {
                "id": ref,
                "name": name,
                "is_reasoning": is_reasoning,
                "description": f"{name} via Dalton broker v2",
                "context_window": context_window,
                "max_output": max_tokens,
                "pricing": pricing_obj,
                "notional_pricing": notional_pricing,
                "dalton": {
                    "openclaw_ref": ref,
                    "upstream_provider": provider,
                    "family": family,
                    "billing": billing,
                    "tool_calling": tool_calling,
                    "price_sources": price_sources,
                },
            }
        )

    broker_meta = catalog.get("meta") if isinstance(catalog.get("meta"), dict) else {}
    source_label = (
        "broker+openclaw_config" if (v1_map or preserved_source) else "broker"
    )
    overlay: dict[str, Any] = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": source_label,
            "broker_version": str(broker_meta.get("brokerVersion") or "2.0.0"),
            "openclaw_version": str(broker_meta.get("openclawVersion") or "unknown"),
            "counts": {
                "catalog": len(models_out),
                "visible": visible_count,
                "hidden": hidden_count,
                "text_only": text_only_count,
            },
            "hidden": hidden_list,
            "price_discrepancies": discrepancies,
            "warnings": warnings,
        },
        "models": models_out,
        "providers": {
            "provider_config": {
                "openclaw-broker": {
                    "sdk": "openai",
                    "base_url": "http://{HOST_IP}:18789/dalton-broker/v1",
                    "env_key": "DALTON_BROKER_TOKEN",
                    "access_type": "local",
                    "use_response_api": False,
                    "dynamic_models": False,
                    "byok_eligible": False,
                    "prompt_cache_key": False,
                    "display_name": "OpenClaw (broker)",
                    "dalton_broker": True,
                }
            },
            "models": {
                "openclaw-broker": provider_models_out,
            },
        },
    }
    assert_no_credentials(overlay)
    return overlay


def diff_overlay(
    current: dict[str, Any] | None,
    new: dict[str, Any],
) -> dict[str, Any]:
    """Compute added, removed, changed, and unchanged model entries between overlays."""
    cur_models = (current or {}).get("models") or {}
    new_models = new.get("models") or {}

    cur_prov_by_ref = {
        m.get("id"): m
        for m in (((current or {}).get("providers") or {}).get("models") or {}).get(
            "openclaw-broker", []
        )
        if isinstance(m, dict)
    }
    new_prov_by_ref = {
        m.get("id"): m
        for m in ((new.get("providers") or {}).get("models") or {}).get(
            "openclaw-broker", []
        )
        if isinstance(m, dict)
    }
    hidden_reasons = {
        h.get("key"): h.get("reason", "hidden")
        for h in ((new.get("meta") or {}).get("hidden") or [])
        if isinstance(h, dict)
    }

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    unchanged = 0

    all_keys = sorted(set(cur_models.keys()) | set(new_models.keys()))
    for key in all_keys:
        in_cur = key in cur_models
        in_new = key in new_models
        if not in_cur and in_new:
            nm = new_models[key]
            if nm.get("visible", True):
                added.append(
                    {
                        "key": key,
                        "openclaw_ref": nm.get("model_id"),
                        "display_name": nm.get("display_name"),
                    }
                )
            else:
                unchanged += 1
            continue

        if in_cur and not in_new:
            cm = cur_models[key]
            removed.append(
                {
                    "key": key,
                    "openclaw_ref": cm.get("model_id"),
                    "reason": "gone_from_openclaw",
                }
            )
            continue

        cm = cur_models[key]
        nm = new_models[key]
        ref = nm.get("model_id")
        c_prov = cur_prov_by_ref.get(ref) or {}
        n_prov = new_prov_by_ref.get(ref) or {}

        # If transitioning from visible=True to visible=False, also report in removed
        if cm.get("visible", True) and not nm.get("visible", True):
            removed.append(
                {
                    "key": key,
                    "openclaw_ref": ref,
                    "reason": hidden_reasons.get(key, "hidden_by_override"),
                }
            )

        field_diffs: dict[str, list[Any]] = {}
        for fld in ("visible", "context", "display_name", "speed", "intelligence"):
            if cm.get(fld) != nm.get(fld):
                field_diffs[fld] = [cm.get(fld), nm.get(fld)]
        c_max = (cm.get("parameters") or {}).get("max_tokens")
        n_max = (nm.get("parameters") or {}).get("max_tokens")
        if c_max != n_max:
            field_diffs["max_output"] = [c_max, n_max]

        c_price = c_prov.get("pricing") or {}
        n_price = n_prov.get("pricing") or {}
        if c_price != n_price:
            for pfld in ("input", "output", "cached_input", "pricing_type"):
                if c_price.get(pfld) != n_price.get(pfld):
                    field_diffs[f"pricing.{pfld}"] = [
                        c_price.get(pfld),
                        n_price.get(pfld),
                    ]
            if not any(k.startswith("pricing.") for k in field_diffs):
                field_diffs["pricing"] = [c_price, n_price]

        if field_diffs:
            changed.append({"key": key, "fields": field_diffs})
        else:
            unchanged += 1

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
    }


def write_overlay_atomic(overlay: dict[str, Any], path: Path | None = None) -> str:
    """Atomically write `openclaw_overlay.json` and return its new overlay_stamp."""
    assert_no_credentials(overlay)
    target = path or OVERLAY_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(".json.tmp")
    serialized = json.dumps(overlay, indent=2, ensure_ascii=False) + "\n"
    tmp_path.write_text(serialized, encoding="utf-8")
    os.replace(tmp_path, target)
    st = target.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def public_extract_sha256(overlay: dict[str, Any]) -> str:
    """Deterministic SHA-256 of the models + providers payload (excluding generated_at)."""
    payload = {
        "models": overlay.get("models") or {},
        "providers": overlay.get("providers") or {},
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
