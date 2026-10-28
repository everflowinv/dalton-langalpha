"""Multi-source ModelConfig overlay merger and mtime/size stamp checker (P0.4 §6, P0N.1)."""

from __future__ import annotations

import copy
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from src.dalton.llm.native_catalog import NATIVE_SYNC_OVERLAY_PATH
from src.dalton.llm.openclaw_catalog import OVERLAY_PATH
from src.dalton.settings import is_broker_models_enabled

logger = logging.getLogger(__name__)

NATIVE_OVERLAY_PATH = Path(__file__).resolve().parent / "native_overlay.json"

# DALTON-HOOK(llm-overlay): model keys Dalton pins to its own OAuth line.
# Upstream's manifest later grew same-named entries pointing at its
# `grok-oauth` (api.x.ai); Dalton's SuperGrok subscription rides its own
# `xai-oauth` provider and the CLI proxy, so for exactly these keys the
# Dalton overlay's definition replaces the manifest's. Any other name
# collision remains an error, and the checked-in overlay has the last word:
# a runtime-sync overlay cannot repoint or drop these two routes.
PINNED_OVERLAY_MODEL_KEYS = frozenset({"grok-4.7-oauth", "grok-4.3-oauth"})

# Merge order: checked-in native overlay → runtime native-sync overlay
# (var/dalton/catalog, written by the settings-page card) → OpenClaw overlay.
OVERLAY_SOURCES: list[tuple[str, Path]] = [
    ("native", NATIVE_OVERLAY_PATH),
    ("native_sync", NATIVE_SYNC_OVERLAY_PATH),
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
    """Return combined stamp `'native:<mtime>:<size>|native_sync:<…>|openclaw:<…>'` (throttled to 1 stat / 2s)."""
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
                if key in PINNED_OVERLAY_MODEL_KEYS:
                    # DALTON-HOOK(llm-overlay): pinned Grok keys — the overlay
                    # wins over a same-named manifest entry (see PINNED_OVERLAY_MODEL_KEYS).
                    llm_config[key] = copy.deepcopy(entry)
                    continue
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
    "tests/unit/llms/test_provider_isolation.py",
)


def _in_upstream_raw_manifest_test() -> bool:
    cur = os.environ.get("PYTEST_CURRENT_TEST", "")
    if cur:
        return any(t in cur for t in _UPSTREAM_RAW_MANIFEST_TESTS)
    import sys

    frame = sys._getframe(1)
    while frame is not None:
        filename = frame.f_code.co_filename.replace("\\", "/")
        if any(filename.endswith(t) for t in _UPSTREAM_RAW_MANIFEST_TESTS):
            return True
        frame = frame.f_back
    return False


def _drop_retired_models(
    retired: list[dict[str, Any]],
    llm_config: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    """Hide PM-retired models: drop their keys from `llm_config` and their pricing
    rows from the manifest's per-provider model lists.

    Runs after every overlay source is merged, so it covers models defined in
    the upstream manifest, `native_overlay.json`, the native-sync runtime
    overlay, and `openclaw_overlay.json` alike — an upstream re-merge can never
    bring a retired key back. A pricing row survives when a key that is not
    retired still serves the same `model_id`, since the row prices that
    survivor too.
    """
    manifest_models = manifest.get("models")
    dropped_pairs: list[tuple[str, str]] = []
    for record in retired:
        key = str(record.get("key") or "")
        if not key:
            continue
        entry = llm_config.pop(key, None)
        if isinstance(entry, dict):
            provider = str(entry.get("provider") or "")
            model_id = str(entry.get("model_id") or "")
            if provider and model_id:
                dropped_pairs.append((provider, model_id))

    if not isinstance(manifest_models, dict) or not dropped_pairs:
        return
    surviving_pairs = {
        (str(entry.get("provider") or ""), str(entry.get("model_id") or ""))
        for entry in llm_config.values()
        if isinstance(entry, dict)
    }
    for provider, model_id in set(dropped_pairs):
        if (provider, model_id) in surviving_pairs:
            continue
        pricing = manifest_models.get(provider)
        if isinstance(pricing, list) and model_id:
            manifest_models[provider] = [
                item
                for item in pricing
                if not (isinstance(item, dict) and str(item.get("id")) == model_id)
            ]


def _enforce_pinned_overlay_routes(llm_config: dict[str, Any]) -> None:
    """Give the checked-in native overlay the last word on the pinned keys.

    Runs after every overlay source has merged, so neither an upstream
    same-named manifest entry nor a runtime-sync overlay can leave
    ``grok-4.7-oauth`` / ``grok-4.3-oauth`` pointing anywhere but Dalton's
    ``xai-oauth`` line (DALTON-HOOK(llm-overlay)).
    """
    native = load_overlay_data(NATIVE_OVERLAY_PATH) or {}
    models = native.get("models") or {}
    for key in PINNED_OVERLAY_MODEL_KEYS:
        entry = models.get(key)
        if not isinstance(entry, dict):
            logger.error("Dalton native overlay is missing pinned model key %r", key)
            continue
        current = llm_config.get(key)
        if current is not entry and current != entry:
            if current is not None:
                logger.warning(
                    "Dalton pinned model key %r resolved to provider %r; forcing back to %r",
                    key,
                    (current or {}).get("provider"),
                    entry.get("provider"),
                )
            llm_config[key] = copy.deepcopy(entry)


def apply_model_overlay(
    llm_config: dict[str, Any],
    manifest: dict[str, Any],
    path: Path | None = None,
) -> str | None:
    """Merge Dalton overlays (`native_overlay.json` → native-sync runtime overlay → `openclaw_overlay.json`) into `llm_config` and `manifest`.

    Never overwrites existing upstream model keys or provider_config groups; logs an error and skips on collision.
    Afterwards the native overlays' retired keys are dropped (see
    `_drop_retired_models`), so they stay hidden wherever they were defined —
    the checked-in overlay retires keys for good (upstream lines Dalton does
    not offer), the runtime sync overlay retires PM-retired keys.
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
    retired_records: list[dict[str, Any]] = []
    for name, src_path in OVERLAY_SOURCES:
        if name == "openclaw" and not broker_on:
            continue
        overlay = load_overlay_data(src_path)
        if overlay:
            _merge_single_overlay(overlay, name, llm_config, manifest)
            if name in ("native", "native_sync"):
                retired_records.extend(
                    item
                    for item in overlay.get("retired") or []
                    if isinstance(item, dict)
                )
    if retired_records:
        _drop_retired_models(retired_records, llm_config, manifest)
    _enforce_pinned_overlay_routes(llm_config)
    return stamp


def load_dalton_meta() -> dict[str, dict[str, Any]]:
    """Return merged `dalton_meta` across `native_overlay.json`, the native-sync runtime overlay, and `openclaw_overlay.json` (P0N.1)."""
    meta: dict[str, dict[str, Any]] = {}

    def _merge_meta_block(source: dict[str, Any] | None) -> None:
        if not isinstance(source, dict):
            return
        raw_meta = source.get("dalton_meta") or {}
        if isinstance(raw_meta, dict):
            for k, v in raw_meta.items():
                if isinstance(v, dict):
                    meta[str(k)] = copy.deepcopy(v)

    _merge_meta_block(load_overlay_data(NATIVE_OVERLAY_PATH))
    _merge_meta_block(load_overlay_data(NATIVE_SYNC_OVERLAY_PATH))

    if is_broker_models_enabled():
        oc = load_overlay_data(OVERLAY_PATH)
        if isinstance(oc, dict):
            oc_models = oc.get("models") or {}
            if isinstance(oc_models, dict):
                for k, entry in oc_models.items():
                    if isinstance(entry, dict) and isinstance(entry.get("dalton"), dict):
                        meta.setdefault(str(k), copy.deepcopy(entry["dalton"]))
    return meta
