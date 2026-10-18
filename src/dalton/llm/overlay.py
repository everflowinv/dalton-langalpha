"""ModelConfig overlay merger and mtime/size stamp checker (P0.4 §6)."""

from __future__ import annotations

import copy
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from src.dalton.llm.openclaw_catalog import OVERLAY_PATH

logger = logging.getLogger(__name__)

_LAST_STAT_AT: float = 0.0
_LAST_STAMP: str | None = None
_STAT_TTL_SECONDS: float = 2.0


def current_overlay_stamp(path: Path | None = None, *, force: bool = False) -> str | None:
    """Return `f'{st_mtime_ns}:{st_size}'` for `openclaw_overlay.json`, throttled to 1 stat / 2s."""
    global _LAST_STAT_AT, _LAST_STAMP
    target = path or OVERLAY_PATH
    now = time.monotonic()
    if not force and (now - _LAST_STAT_AT) < _STAT_TTL_SECONDS:
        return _LAST_STAMP
    _LAST_STAT_AT = now
    try:
        st = os.stat(target)
        _LAST_STAMP = f"{st.st_mtime_ns}:{st.st_size}"
    except FileNotFoundError:
        _LAST_STAMP = None
    except OSError as exc:
        logger.debug("Failed to stat openclaw_overlay.json: %s", exc)
    return _LAST_STAMP


def overlay_changed(previous_stamp: str | None, path: Path | None = None) -> bool:
    """Return True if the overlay file stamp on disk differs from `previous_stamp`."""
    return current_overlay_stamp(path) != previous_stamp


def load_overlay_data(path: Path | None = None) -> dict[str, Any] | None:
    """Load `openclaw_overlay.json` from disk, returning None if absent or invalid."""
    target = path or OVERLAY_PATH
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.error("Failed to load openclaw_overlay.json: %s", exc)
        return None


def apply_model_overlay(
    llm_config: dict[str, Any],
    manifest: dict[str, Any],
    path: Path | None = None,
) -> str | None:
    """Merge `openclaw_overlay.json` into `llm_config` (models.json) and `manifest` (providers.json).

    Never overwrites existing upstream model keys; logs an error and skips on collision.
    Returns the overlay stamp `f'{st_mtime_ns}:{st_size}'` or None if file does not exist.
    """
    target = path or OVERLAY_PATH
    stamp = current_overlay_stamp(target, force=True)
    if stamp is None:
        return None
    overlay = load_overlay_data(target)
    if not overlay:
        return stamp

    overlay_models = overlay.get("models") or {}
    if isinstance(overlay_models, dict):
        for key, entry in overlay_models.items():
            if key in llm_config:
                logger.error(
                    "Dalton overlay model key %r collides with upstream models.json; skipping.",
                    key,
                )
                continue
            llm_config[key] = copy.deepcopy(entry)

    providers_block = overlay.get("providers") or {}
    if isinstance(providers_block, dict):
        prov_cfg = providers_block.get("provider_config") or {}
        if isinstance(prov_cfg, dict):
            target_prov_cfg = manifest.setdefault("provider_config", {})
            for p_name, p_entry in prov_cfg.items():
                target_prov_cfg[p_name] = copy.deepcopy(p_entry)

        prov_models = providers_block.get("models") or {}
        if isinstance(prov_models, dict):
            target_models = manifest.setdefault("models", {})
            for p_name, p_list in prov_models.items():
                if isinstance(p_list, list):
                    target_models[p_name] = copy.deepcopy(p_list)

    return stamp
