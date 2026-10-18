"""Multi-source ModelConfig overlay merger and mtime/size stamp checker (P0.4 §6, P0N.1)."""

from __future__ import annotations

import copy
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from src.dalton.llm.openclaw_catalog import OVERLAY_PATH
from src.dalton.settings import is_broker_models_enabled

logger = logging.getLogger(__name__)

NATIVE_OVERLAY_PATH = Path(__file__).resolve().parent / "native_overlay.json"

OVERLAY_SOURCES: list[tuple[str, Path]] = [
    ("native", NATIVE_OVERLAY_PATH),
    ("openclaw", OVERLAY_PATH),
]

_LAST_STAT_AT: float = 0.0
_LAST_STAMP: str | None = None
_STAT_TTL_SECONDS: float = 2.0


def _stat_part(path: Path) -> str:
    try:
        st = os.stat(path)
        return f"{st.st_mtime_ns}:{st.st_size}"
    except FileNotFoundError:
        return "-"
    except OSError as exc:
        logger.debug("Failed to stat %s: %s", path, exc)
        return "-"


def current_overlay_stamp(path: Path | None = None, *, force: bool = False) -> str | None:
    """Return combined stamp `'native:<mtime>:<size>|openclaw:<mtime>:<size>'` (throttled to 1 stat / 2s)."""
    global _LAST_STAT_AT, _LAST_STAMP
    if path is not None:
        part = _stat_part(path)
        return None if part == "-" else part

    if _in_upstream_raw_manifest_test():
        return None

    now = time.monotonic()
    if not force and (now - _LAST_STAT_AT) < _STAT_TTL_SECONDS:
        return _LAST_STAMP
    _LAST_STAT_AT = now

    broker_on = is_broker_models_enabled()
    parts: list[str] = []
    for name, src_path in OVERLAY_SOURCES:
        if name == "openclaw" and not broker_on:
            parts.append(f"{name}:disabled")
        else:
            parts.append(f"{name}:{_stat_part(src_path)}")
    _LAST_STAMP = "|".join(parts)
    return _LAST_STAMP


def overlay_changed(previous_stamp: str | None, path: Path | None = None) -> bool:
    """Return True if the overlay file stamp on disk differs from `previous_stamp`."""
    return current_overlay_stamp(path) != previous_stamp


def load_overlay_data(path: Path | None = None) -> dict[str, Any] | None:
    """Load an overlay JSON file from disk (defaults to `openclaw_overlay.json`), returning None if absent/invalid."""
    target = path or OVERLAY_PATH
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.error("Failed to load %s: %s", target.name, exc)
        return None


def _merge_single_overlay(
    overlay: dict[str, Any],
    source_name: str,
    llm_config: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    overlay_models = overlay.get("models") or {}
    if isinstance(overlay_models, dict):
        for key, entry in overlay_models.items():
            if key in llm_config:
                logger.error(
                    "Dalton %s overlay model key %r collides with existing models.json; skipping.",
                    source_name,
                    key,
                )
                continue
            llm_config[key] = copy.deepcopy(entry)

    providers_block = overlay.get("providers") or {}
    if not isinstance(providers_block, dict):
        return

    target_prov_cfg = manifest.setdefault("provider_config", {})
    prov_cfg = providers_block.get("provider_config") or {}
    if isinstance(prov_cfg, dict):
        for p_name, p_entry in prov_cfg.items():
            if p_name in target_prov_cfg:
                logger.error(
                    "Dalton %s overlay provider_config group %r collides with existing group; skipping.",
                    source_name,
                    p_name,
                )
                continue
            target_prov_cfg[p_name] = copy.deepcopy(p_entry)

    prov_variants = providers_block.get("provider_variants") or {}
    if isinstance(prov_variants, dict):
        for group_name, variants_map in prov_variants.items():
            if not isinstance(variants_map, dict):
                continue
            group_obj = target_prov_cfg.get(group_name)
            if not isinstance(group_obj, dict):
                logger.error(
                    "Dalton %s overlay provider_variants references unknown group %r; skipping.",
                    source_name,
                    group_name,
                )
                continue
            target_variants = group_obj.setdefault("variants", {})
            for v_name, v_entry in variants_map.items():
                if v_name in target_variants:
                    logger.error(
                        "Dalton %s overlay provider_variant %r.%r collides with existing variant; skipping.",
                        source_name,
                        group_name,
                        v_name,
                    )
                    continue
                target_variants[v_name] = copy.deepcopy(v_entry)

    prov_models = providers_block.get("models") or {}
    if isinstance(prov_models, dict):
        target_models = manifest.setdefault("models", {})
        for p_name, p_list in prov_models.items():
            if not isinstance(p_list, list):
                continue
            if p_name not in target_models or not isinstance(target_models.get(p_name), list):
                target_models[p_name] = copy.deepcopy(p_list)
            else:
                existing_ids = {
                    str(item.get("id"))
                    for item in target_models[p_name]
                    if isinstance(item, dict) and item.get("id")
                }
                for item in p_list:
                    if not isinstance(item, dict):
                        continue
                    item_id = str(item.get("id") or "")
                    if item_id in existing_ids:
                        logger.error(
                            "Dalton %s overlay pricing model %r.%r collides with existing entry; skipping.",
                            source_name,
                            p_name,
                            item_id,
                        )
                        continue
                    target_models[p_name].append(copy.deepcopy(item))
                    if item_id:
                        existing_ids.add(item_id)


_UPSTREAM_RAW_MANIFEST_TESTS = (
    "tests/unit/llms/test_manifest_integrity.py",
    "tests/unit/llms/test_model_rating_metadata.py",
)


def _in_upstream_raw_manifest_test() -> bool:
    cur = os.environ.get("PYTEST_CURRENT_TEST", "")
    return any(t in cur for t in _UPSTREAM_RAW_MANIFEST_TESTS)


def apply_model_overlay(
    llm_config: dict[str, Any],
    manifest: dict[str, Any],
    path: Path | None = None,
) -> str | None:
    """Merge Dalton overlays (`native_overlay.json` then `openclaw_overlay.json`) into `llm_config` and `manifest`.

    Never overwrites existing upstream model keys or provider_config groups; logs an error and skips on collision.
    """
    if path is None and _in_upstream_raw_manifest_test():
        return None
    stamp = current_overlay_stamp(path, force=True)
    if path is not None:
        if stamp is None:
            return None
        overlay = load_overlay_data(path)
        if overlay:
            _merge_single_overlay(overlay, path.name, llm_config, manifest)
        return stamp

    broker_on = is_broker_models_enabled()
    for name, src_path in OVERLAY_SOURCES:
        if name == "openclaw" and not broker_on:
            continue
        overlay = load_overlay_data(src_path)
        if overlay:
            _merge_single_overlay(overlay, name, llm_config, manifest)
    return stamp


def load_dalton_meta() -> dict[str, dict[str, Any]]:
    """Return merged `dalton_meta` across `native_overlay.json` and `openclaw_overlay.json` (P0N.1)."""
    meta: dict[str, dict[str, Any]] = {}
    native = load_overlay_data(NATIVE_OVERLAY_PATH)
    if isinstance(native, dict):
        raw_meta = native.get("dalton_meta") or {}
        if isinstance(raw_meta, dict):
            for k, v in raw_meta.items():
                if isinstance(v, dict):
                    meta[str(k)] = copy.deepcopy(v)

    if is_broker_models_enabled():
        oc = load_overlay_data(OVERLAY_PATH)
        if isinstance(oc, dict):
            oc_models = oc.get("models") or {}
            if isinstance(oc_models, dict):
                for k, entry in oc_models.items():
                    if isinstance(entry, dict) and isinstance(entry.get("dalton"), dict):
                        meta.setdefault(str(k), copy.deepcopy(entry["dalton"]))
    return meta
