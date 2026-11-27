"""Pure helpers for extracting effective model metadata from persisted run rows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def adopted_run_llm_model(existing: Mapping[str, Any] | None) -> str | None:
    """Return the non-empty ``metadata.llm_model`` recorded on an existing run row."""
    if not isinstance(existing, Mapping):
        return None
    meta = existing.get("metadata")
    if not isinstance(meta, dict):
        return None
    model = meta.get("llm_model")
    if isinstance(model, str) and model:
        return model
    return None
