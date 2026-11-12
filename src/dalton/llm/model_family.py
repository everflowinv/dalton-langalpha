"""Canonical vendor family for a bare model id.

Shared by role policy and native catalog sync. The OpenClaw broker catalog
that used to own this table was removed with the ``ocb-*`` models (OC-5).
"""

from __future__ import annotations

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
