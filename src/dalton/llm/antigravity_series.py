"""Antigravity model series grouping and centralized default reasoning effort.

Pure stdlib at import time so standalone tools/skills can import this module
without backend or third-party dependencies.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

ANTIGRAVITY_PROVIDER = "antigravity-oauth"

# Single source of truth for per-series default reasoning effort across backend & UI
# (served to frontend via GET /api/v1/models).
ANTIGRAVITY_SERIES_DEFAULT_EFFORTS: dict[str, str] = {
    "gemini-4-neon": "high",
    "gemini-4-hydrogen": "high",
    "gemini-4-argon": "high",
    "gemini-4-carbon": "medium",
}
FALLBACK_SERIES_DEFAULT_EFFORT = "medium"

# Canonical ladder matching src/llms/reasoning.py:REASONING_LEVELS.
REASONING_LEVELS: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

_OAUTH_SUFFIX = "-oauth"
_VERSION_PAIR_RE = re.compile(r"-(\d+)-(\d+)(?=-|$)")
_CAPACITY_TOKEN_RE = re.compile(r"^\d+[a-z]+$")


@dataclass(frozen=True)
class AntigravitySeriesEntry:
    """One collapsed Antigravity model series derived from the active catalog."""

    series_id: str
    series_prefix: str
    provider: str
    display_name: str
    qualified_display_name: str
    default_effort: str
    default_model: str
    efforts: list[str]
    variants: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "series_id": self.series_id,
            "series_prefix": self.series_prefix,
            "provider": self.provider,
            "display_name": self.display_name,
            "qualified_display_name": self.qualified_display_name,
            "default_effort": self.default_effort,
            "default_model": self.default_model,
            "efforts": list(self.efforts),
            "variants": dict(self.variants),
        }


def parse_antigravity_series(key: str) -> tuple[str, str | None] | None:
    """Split an Antigravity ``*-oauth`` model key into ``(series_prefix, effort)``.

    Returns ``None`` if ``key`` does not end with ``-oauth`` or has an empty stem.
    When the stem does not end with ``-<effort>`` from ``REASONING_LEVELS`` (e.g.
    ``gemini-2.5-pro-oauth``), returns ``(stem, None)``.
    """
    if not isinstance(key, str) or not key.endswith(_OAUTH_SUFFIX):
        return None
    stem = key[: -len(_OAUTH_SUFFIX)]
    if not stem:
        return None
    for effort in REASONING_LEVELS:
        suffix = f"-{effort}"
        if stem.endswith(suffix) and len(stem) > len(suffix):
            return stem[: -len(suffix)], effort
    return stem, None


def format_series_display_name(series_prefix: str) -> str:
    """Format a series prefix into its human-readable series name."""
    cleaned = _VERSION_PAIR_RE.sub(r" \1.\2", series_prefix).replace("-", " ")
    tokens = cleaned.split()
    formatted: list[str] = []
    for i, token in enumerate(tokens):
        low = token.lower()
        if low == "gpt" and i + 1 < len(tokens) and tokens[i + 1].lower() == "oss":
            formatted.append("GPT-OSS")
            continue
        if low == "oss" and i > 0 and tokens[i - 1].lower() == "gpt":
            continue
        if low == "gpt":
            formatted.append("GPT")
        elif low == "oss":
            formatted.append("OSS")
        elif _CAPACITY_TOKEN_RE.match(low):
            formatted.append(low.upper())
        else:
            formatted.append(token[0].upper() + token[1:])
    return " ".join(formatted)


def load_dalton_series_default_efforts(
    dalton_yaml_path: Path | None = None,
) -> dict[str, str]:
    """Return per-series default efforts, overlaid with ``llm.antigravity_series_default_efforts`` from ``dalton.yaml``."""
    merged = dict(ANTIGRAVITY_SERIES_DEFAULT_EFFORTS)
    path = dalton_yaml_path or (Path(__file__).resolve().parents[1] / "dalton.yaml")
    if not path.exists():
        return merged
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return merged
    if not isinstance(raw, Mapping):
        return merged
    llm_block = raw.get("llm")
    if not isinstance(llm_block, Mapping):
        return merged
    overrides = llm_block.get("antigravity_series_default_efforts")
    if isinstance(overrides, Mapping):
        for prefix, effort in overrides.items():
            if isinstance(prefix, str) and isinstance(effort, str) and effort in REASONING_LEVELS:
                merged[prefix] = effort
    return merged


def resolve_series_default_effort(
    series_prefix: str,
    efforts: Sequence[str],
    *,
    default_efforts: Mapping[str, str] | None = None,
    dalton_yaml_path: Path | None = None,
) -> str:
    """Resolve the default effort for ``series_prefix`` within available ``efforts``.

    Uses the centralized series default (with optional ``dalton.yaml`` override)
    and falls back using the same step-down clamping rule as
    ``clamp_reasoning_effort`` when the configured default is absent from
    ``efforts``.
    """
    efforts_map = (
        default_efforts
        if default_efforts is not None
        else load_dalton_series_default_efforts(dalton_yaml_path=dalton_yaml_path)
    )
    target = efforts_map.get(series_prefix, FALLBACK_SERIES_DEFAULT_EFFORT)
    if target not in REASONING_LEVELS:
        target = FALLBACK_SERIES_DEFAULT_EFFORT

    ordered = [lv for lv in REASONING_LEVELS if lv in efforts]
    if not ordered:
        return target
    if target in ordered:
        return target

    ceiling = REASONING_LEVELS.index(target)
    at_or_below = [lv for lv in ordered if REASONING_LEVELS.index(lv) <= ceiling]
    return at_or_below[-1] if at_or_below else ordered[0]


def _extract_llm_config(model_config: Any) -> Mapping[str, Any] | None:
    if model_config is None:
        return None
    if isinstance(model_config, Mapping):
        return model_config
    llm_cfg = getattr(model_config, "llm_config", None)
    return llm_cfg if isinstance(llm_cfg, Mapping) else None


def build_antigravity_series_catalog(
    model_config: Any,
    *,
    default_efforts: Mapping[str, str] | None = None,
    dalton_yaml_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Aggregate visible built-in ``antigravity-oauth`` entries by series prefix."""
    llm_config = _extract_llm_config(model_config)
    if not llm_config:
        return []

    efforts_map = (
        default_efforts
        if default_efforts is not None
        else load_dalton_series_default_efforts(dalton_yaml_path=dalton_yaml_path)
    )

    grouped: dict[str, dict[str, str]] = {}
    for key, entry in llm_config.items():
        if not isinstance(key, str) or not isinstance(entry, Mapping):
            continue
        if entry.get("provider") != ANTIGRAVITY_PROVIDER:
            continue
        if entry.get("visible") is False or entry.get("retired"):
            continue
        parsed = parse_antigravity_series(key)
        if parsed is None:
            continue
        series_prefix, effort = parsed
        if effort is None:
            continue
        grouped.setdefault(series_prefix, {})[effort] = key

    result: list[dict[str, Any]] = []
    for series_prefix, raw_variants in grouped.items():
        efforts = [lv for lv in REASONING_LEVELS if lv in raw_variants]
        if not efforts:
            continue
        variants = {lv: raw_variants[lv] for lv in efforts}
        default_effort = resolve_series_default_effort(
            series_prefix,
            efforts,
            default_efforts=efforts_map,
        )
        display_name = format_series_display_name(series_prefix)
        entry = AntigravitySeriesEntry(
            series_id=f"{ANTIGRAVITY_PROVIDER}:{series_prefix}",
            series_prefix=series_prefix,
            provider=ANTIGRAVITY_PROVIDER,
            display_name=display_name,
            qualified_display_name=f"{display_name} (Antigravity)",
            default_effort=default_effort,
            default_model=variants[default_effort],
            efforts=efforts,
            variants=variants,
        )
        result.append(entry.to_dict())
    return result


def quota_cooldown_target(
    provider: str,
    model_name: str,
    *,
    is_custom: bool = False,
    model_config: Any = None,
) -> tuple[Literal["series", "model"], str]:
    """Return ``("series", series_prefix)`` for built-in Antigravity models, else ``("model", model_name)``.

    Built-in ``antigravity-oauth`` models share one upstream quota pool across
    reasoning-effort variants of the same series. All other providers and
    user-defined custom models (including custom entries that shadow a built-in
    model name) remain scoped to the individual model.
    """
    if provider != ANTIGRAVITY_PROVIDER or is_custom or not model_name:
        return ("model", model_name)

    parsed = parse_antigravity_series(model_name)
    if parsed is None:
        return ("model", model_name)

    llm_cfg = _extract_llm_config(model_config)
    if llm_cfg is None:
        try:
            from src.llms.llm import LLM

            llm_cfg = _extract_llm_config(LLM.get_model_config())
        except Exception:
            llm_cfg = None
    if llm_cfg is None:
        return ("model", model_name)

    entry = llm_cfg.get(model_name)
    if not isinstance(entry, Mapping) or entry.get("provider") != ANTIGRAVITY_PROVIDER:
        return ("model", model_name)

    series_prefix, _effort = parsed
    return ("series", series_prefix)
