"""Multi-source ModelConfig overlay merger and mtime/size stamp checker (P0.4 §6, P0N.1)."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from src.dalton.llm.native_catalog import NATIVE_SYNC_OVERLAY_PATH

logger = logging.getLogger(__name__)

NATIVE_OVERLAY_PATH = Path(__file__).resolve().parent / "native_overlay.json"

# DALTON-HOOK(llm-overlay): model keys Dalton pins to its own OAuth line.
# Upstream's manifest later grew same-named entries pointing at its
# `grok-oauth` (api.x.ai); Dalton's SuperGrok subscription rides its own
# `xai-oauth` provider and the CLI proxy, so for exactly these keys the
# Dalton overlay's definition replaces the manifest's. Any other name
# collision remains an error, and the checked-in overlay has the last word:
# a runtime-sync overlay cannot repoint or drop this route.
PINNED_OVERLAY_MODEL_KEYS = frozenset({"grok-4.7-oauth"})

# Merge order: checked-in native overlay → runtime native-sync overlay
# (var/dalton/catalog, written by the settings-page card).
# The OpenClaw broker overlay (`ocb-*`) was removed in OC-5.
OVERLAY_SOURCES: list[tuple[str, Path]] = [
    ("native", NATIVE_OVERLAY_PATH),
    ("native_sync", NATIVE_SYNC_OVERLAY_PATH),
]

_LAST_STAT_AT: float = 0.0
_LAST_STAMP: str | None = None
_LAST_STAMP_OAUTH_ON: bool | None = None
_STAT_TTL_SECONDS: float = 2.0


def _is_native_oauth_sync_enabled() -> bool:
    """Return True when the ``native_oauth_sync`` capability is active."""
    from src.dalton.capabilities import CapabilityId, has_capability

    return has_capability(CapabilityId.NATIVE_OAUTH_SYNC)


def _is_oauth_model_key(key: str) -> bool:
    return str(key or "").strip().endswith("-oauth")


def _filter_generic_overlay(overlay: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``overlay`` retaining only generic (non-private-OAuth) entries.

    Used when ``CapabilityId.NATIVE_OAUTH_SYNC`` is disabled (e.g. Standalone
    Core profile) so private Dalton OAuth providers/models (``antigravity-oauth``,
    ``muse-oauth``, ``xai-oauth``, and ``*-oauth`` keys) are never injected while
    generic BYOK additions/overrides (such as ``grok-build-0.1``, ``volcengine``,
    coding-plan models, and upstream model metadata patches) remain available.
    """
    providers_block = overlay.get("providers")
    oauth_provider_names: set[str] = set()
    generic_prov_cfg: dict[str, Any] = {}
    generic_prov_variants: dict[str, Any] = {}
    generic_prov_models: dict[str, Any] = {}
    generic_prov_model_overrides: dict[str, Any] = {}

    if isinstance(providers_block, dict):
        raw_prov_cfg = providers_block.get("provider_config") or {}
        if isinstance(raw_prov_cfg, dict):
            for group_name, group_entry in raw_prov_cfg.items():
                if not isinstance(group_entry, dict):
                    continue
                is_oauth_group = (
                    str(group_entry.get("access_type") or "").strip().lower() == "oauth"
                    or _is_oauth_model_key(group_name)
                )
                raw_variants = group_entry.get("variants")
                if is_oauth_group:
                    oauth_provider_names.add(group_name)
                    if isinstance(raw_variants, dict):
                        oauth_provider_names.update(str(v) for v in raw_variants)
                    continue
                kept_entry = copy.deepcopy(group_entry)
                if isinstance(raw_variants, dict):
                    kept_variants: dict[str, Any] = {}
                    for v_name, v_entry in raw_variants.items():
                        v_is_oauth = _is_oauth_model_key(v_name) or (
                            isinstance(v_entry, dict)
                            and str(v_entry.get("access_type") or "").strip().lower() == "oauth"
                        )
                        if v_is_oauth:
                            oauth_provider_names.add(str(v_name))
                        else:
                            kept_variants[v_name] = copy.deepcopy(v_entry)
                    kept_entry["variants"] = kept_variants
                generic_prov_cfg[group_name] = kept_entry

        raw_prov_variants = providers_block.get("provider_variants") or {}
        if isinstance(raw_prov_variants, dict):
            for group_name, variants_map in raw_prov_variants.items():
                if group_name in oauth_provider_names or _is_oauth_model_key(group_name):
                    continue
                if not isinstance(variants_map, dict):
                    continue
                kept = {
                    v_name: copy.deepcopy(v_entry)
                    for v_name, v_entry in variants_map.items()
                    if not _is_oauth_model_key(v_name)
                    and not (
                        isinstance(v_entry, dict)
                        and str(v_entry.get("access_type") or "").strip().lower() == "oauth"
                    )
                }
                if kept:
                    generic_prov_variants[group_name] = kept

        raw_prov_models = providers_block.get("models") or {}
        if isinstance(raw_prov_models, dict):
            for p_name, p_list in raw_prov_models.items():
                if p_name in oauth_provider_names or _is_oauth_model_key(p_name):
                    continue
                generic_prov_models[p_name] = copy.deepcopy(p_list)

        raw_prov_overrides = providers_block.get("model_overrides") or {}
        if isinstance(raw_prov_overrides, dict):
            for p_name, patches_by_id in raw_prov_overrides.items():
                if p_name in oauth_provider_names or _is_oauth_model_key(p_name):
                    continue
                generic_prov_model_overrides[p_name] = copy.deepcopy(patches_by_id)

    generic_models: dict[str, Any] = {}
    raw_models = overlay.get("models") or {}
    if isinstance(raw_models, dict):
        for key, entry in raw_models.items():
            if _is_oauth_model_key(key):
                continue
            provider = str(entry.get("provider") or "") if isinstance(entry, dict) else ""
            if provider in oauth_provider_names or _is_oauth_model_key(provider):
                continue
            generic_models[key] = copy.deepcopy(entry)

    generic_model_overrides: dict[str, Any] = {}
    raw_model_overrides = overlay.get("model_overrides") or {}
    if isinstance(raw_model_overrides, dict):
        for key, patch in raw_model_overrides.items():
            if _is_oauth_model_key(key):
                continue
            generic_model_overrides[key] = copy.deepcopy(patch)

    return {
        "schema": overlay.get("schema"),
        "models": generic_models,
        "model_overrides": generic_model_overrides,
        "providers": {
            "provider_config": generic_prov_cfg,
            "provider_variants": generic_prov_variants,
            "models": generic_prov_models,
            "model_overrides": generic_prov_model_overrides,
        },
    }


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
    """Return combined stamp `'native:<mtime>:<size>|native_sync:<…>'` (throttled to 1 stat / 2s)."""
    global _LAST_STAT_AT, _LAST_STAMP, _LAST_STAMP_OAUTH_ON
    if path is not None:
        part = _stat_part(path)
        return None if part == "-" else part

    if _in_upstream_raw_manifest_test():
        return None

    oauth_on = _is_native_oauth_sync_enabled()
    now = time.monotonic()
    if (
        not force
        and _LAST_STAMP_OAUTH_ON == oauth_on
        and (now - _LAST_STAT_AT) < _STAT_TTL_SECONDS
    ):
        return _LAST_STAMP
    _LAST_STAT_AT = now
    _LAST_STAMP_OAUTH_ON = oauth_on

    if not oauth_on:
        _LAST_STAMP = f"native_generic:{_stat_part(NATIVE_OVERLAY_PATH)}"
        return _LAST_STAMP

    parts = [f"{name}:{_stat_part(src_path)}" for name, src_path in OVERLAY_SOURCES]
    _LAST_STAMP = "|".join(parts)
    return _LAST_STAMP


def overlay_changed(previous_stamp: str | None, path: Path | None = None) -> bool:
    """Return True if the overlay file stamp on disk differs from `previous_stamp`."""
    return current_overlay_stamp(path) != previous_stamp


def load_overlay_data(path: Path | None = None) -> dict[str, Any] | None:
    """Load an overlay JSON file from disk (defaults to `native_overlay.json`), returning None if absent/invalid."""
    target = path or NATIVE_OVERLAY_PATH
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.error("Failed to load %s: %s", target.name, exc)
        return None


def _deep_merge_patch(target: dict[str, Any], patch: dict[str, Any]) -> None:
    """Recursively overwrite only the keys listed in `patch` onto `target`."""
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(target.get(k), dict):
            _deep_merge_patch(target[k], v)
        else:
            target[k] = copy.deepcopy(v)


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

    # DALTON-HOOK(llm-overlay): allow patching listed fields onto existing upstream models
    # without duplicating full model entries in native_overlay.json.
    model_overrides = overlay.get("model_overrides") or {}
    if isinstance(model_overrides, dict):
        for key, patch in model_overrides.items():
            if not isinstance(patch, dict):
                continue
            existing = llm_config.get(key)
            if not isinstance(existing, dict):
                logger.error(
                    "Dalton %s overlay model_overrides references unknown model %r; skipping.",
                    source_name,
                    key,
                )
                continue
            _deep_merge_patch(existing, patch)

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

    # DALTON-HOOK(llm-overlay): allow patching listed fields onto existing upstream
    # pricing cards without duplicating cards in native_overlay.json.
    prov_model_overrides = providers_block.get("model_overrides") or {}
    if isinstance(prov_model_overrides, dict):
        target_models = manifest.setdefault("models", {})
        for p_name, patches_by_id in prov_model_overrides.items():
            if not isinstance(patches_by_id, dict):
                continue
            cards = target_models.get(p_name)
            if not isinstance(cards, list):
                logger.error(
                    "Dalton %s overlay providers.model_overrides references unknown provider %r; skipping.",
                    source_name,
                    p_name,
                )
                continue
            by_id = {
                str(item.get("id")): item
                for item in cards
                if isinstance(item, dict) and item.get("id")
            }
            for card_id, patch in patches_by_id.items():
                if not isinstance(patch, dict):
                    continue
                card = by_id.get(str(card_id))
                if card is None:
                    logger.error(
                        "Dalton %s overlay providers.model_overrides references unknown card %r.%r; skipping.",
                        source_name,
                        p_name,
                        card_id,
                    )
                    continue
                _deep_merge_patch(card, patch)


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
    the upstream manifest, `native_overlay.json`, and the native-sync runtime
    overlay alike — an upstream re-merge can never bring a retired key back.
    A pricing row survives when a key that is not retired still serves the
    same `model_id`, since the row prices that survivor too.
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
    ``grok-4.7-oauth`` pointing anywhere but Dalton's
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


_ANTIGRAVITY_EFFORT_RE = re.compile(r"-(?:minimal|low|medium|high|xhigh|max)$")


def antigravity_gemini_direct_base(entry: dict[str, Any]) -> str | None:
    """Return the direct Gemini manifest key (e.g. ``gemini-4-neon``) for an
    ``antigravity-oauth`` Gemini entry, or ``None`` for any other entry."""
    if entry.get("provider") != "antigravity-oauth":
        return None
    model_id = str(entry.get("model_id") or "")
    if not model_id:
        return None
    # The caller keeps only a base whose catalog entry is a direct ``gemini`` row,
    # so non-Gemini Antigravity models (Claude, GPT-OSS) inherit nothing.
    return _ANTIGRAVITY_EFFORT_RE.sub("", model_id)


def inherit_antigravity_gemini_ratings(
    entries: dict[str, Any],
    source_catalog: dict[str, Any] | None = None,
) -> int:
    """Fill missing ``intelligence`` / ``speed`` / ``plain_language`` ratings on
    ``antigravity-oauth`` Gemini entries from their matching direct ``gemini``
    API manifest entry (e.g. ``gemini-4-neon`` -> ``gemini-4-neon-*-oauth``).

    Existing explicit ratings and non-Antigravity (or non-Gemini) entries are
    left untouched. Returns the number of entries that gained at least one rating.
    """
    catalog = source_catalog if source_catalog is not None else entries
    updated = 0
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        base_key = antigravity_gemini_direct_base(entry)
        if not base_key:
            continue
        direct = catalog.get(base_key)
        if not isinstance(direct, dict) or direct.get("provider") != "gemini":
            continue
        changed = False
        for field in ("intelligence", "speed", "plain_language"):
            val = direct.get(field)
            if entry.get(field) is None and isinstance(val, int) and not isinstance(val, bool):
                entry[field] = val
                changed = True
        if changed:
            updated += 1
    return updated


def apply_model_overlay(
    llm_config: dict[str, Any],
    manifest: dict[str, Any],
    path: Path | None = None,
) -> str | None:
    """Merge Dalton overlays (`native_overlay.json` → native-sync runtime overlay) into `llm_config` and `manifest`.

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
        inherit_antigravity_gemini_ratings(llm_config)
        return stamp

    if not _is_native_oauth_sync_enabled():
        native = load_overlay_data(NATIVE_OVERLAY_PATH)
        retired_records: list[dict[str, Any]] = []
        if native:
            _merge_single_overlay(_filter_generic_overlay(native), "native", llm_config, manifest)
            retired_records.extend(
                item
                for item in native.get("retired") or []
                if isinstance(item, dict)
            )
        # Drop retired entries and every subscription/OAuth model (including
        # upstream *-oauth keys such as grok-*-oauth / codex-oauth / claude-oauth)
        # so unreachable OAuth models never appear in the Core catalog.
        oauth_retired = [
            {"key": key}
            for key, entry in list(llm_config.items())
            if _is_oauth_model_key(key)
            or (
                isinstance(entry, dict)
                and _is_oauth_model_key(str(entry.get("provider") or ""))
            )
        ]
        if retired_records or oauth_retired:
            _drop_retired_models([*retired_records, *oauth_retired], llm_config, manifest)
        return stamp

    retired_records: list[dict[str, Any]] = []
    for name, src_path in OVERLAY_SOURCES:
        overlay = load_overlay_data(src_path)
        if overlay:
            _merge_single_overlay(overlay, name, llm_config, manifest)
            retired_records.extend(
                item
                for item in overlay.get("retired") or []
                if isinstance(item, dict)
            )
    if retired_records:
        _drop_retired_models(retired_records, llm_config, manifest)
    _enforce_pinned_overlay_routes(llm_config)
    inherit_antigravity_gemini_ratings(llm_config)
    return stamp


def load_dalton_meta() -> dict[str, dict[str, Any]]:
    """Return merged `dalton_meta` across `native_overlay.json` and the native-sync runtime overlay (P0N.1)."""
    meta: dict[str, dict[str, Any]] = {}
    oauth_on = _is_native_oauth_sync_enabled()

    def _merge_meta_block(source: dict[str, Any] | None) -> None:
        if not isinstance(source, dict):
            return
        raw_meta = source.get("dalton_meta") or {}
        if isinstance(raw_meta, dict):
            for k, v in raw_meta.items():
                if not oauth_on and _is_oauth_model_key(str(k)):
                    continue
                if isinstance(v, dict):
                    meta[str(k)] = copy.deepcopy(v)

    _merge_meta_block(load_overlay_data(NATIVE_OVERLAY_PATH))
    if oauth_on:
        _merge_meta_block(load_overlay_data(NATIVE_SYNC_OVERLAY_PATH))
    return meta
