"""Single authoritative subscription-vs-cash billing resolver for team catalog models.

Consolidates all billing classification rules in one place so save validation,
live pricing (`src/llms/pricing_utils.py`), snapshot generation & lookup
(`snapshot.py`), turn/between-turn settlement (`src/utils/tracking/core.py`,
`thread_maintenance.py`), notional pricing (`src/dalton/llm/notional_pricing.py`),
and budget accounting (`src/dalton/budget/service.py`) always agree.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal

from src.dalton.services.team_model_catalog.constants import norm_id

BillingKind = Literal["subscription", "cash"]

_SCOPED_DALTON_META: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "_SCOPED_DALTON_META", default=None
)


@contextmanager
def bind_billing_dalton_meta(dalton_meta: Mapping[str, Any] | None) -> Iterator[None]:
    """Bind an already-loaded ``dalton_meta`` map for nested billing lookups (no DB fallback)."""
    token = _SCOPED_DALTON_META.set(dalton_meta)
    try:
        yield
    finally:
        _SCOPED_DALTON_META.reset(token)


def _is_oauth_name(name: str | None) -> bool:
    if not name:
        return False
    from src.dalton.llm.overlay import _is_oauth_model_key

    return _is_oauth_model_key(str(name).strip().lower())


def _lookup_catalog_entry(
    llm_cfg: Mapping[str, Any],
    model_key: str,
) -> tuple[str | None, Mapping[str, Any] | None]:
    if not model_key or not isinstance(llm_cfg, Mapping):
        return None, None
    exact = llm_cfg.get(model_key)
    if isinstance(exact, Mapping):
        return model_key, exact
    low = model_key.lower()
    for k, v in llm_cfg.items():
        if isinstance(v, Mapping) and str(k).lower() == low:
            return str(k), v
    return None, None


def _entry_matches_explicit_route(
    entry: Mapping[str, Any],
    *,
    provider: str,
    model_id: str,
) -> bool:
    if provider:
        e_prov = str(entry.get("provider") or "").strip().lower()
        e_sys = str(entry.get("system_provider") or "").strip().lower()
        if provider.lower() not in {p for p in (e_prov, e_sys) if p}:
            return False
    if model_id:
        e_mid = norm_id(str(entry.get("model_id") or ""))
        if e_mid and e_mid != norm_id(model_id):
            return False
    return True


def _find_matching_keys_by_route(
    llm_cfg: Mapping[str, Any],
    *,
    provider: str,
    model_id: str,
) -> list[str]:
    if not provider or not model_id or not isinstance(llm_cfg, Mapping):
        return []
    want_prov = provider.lower()
    want_mid = norm_id(model_id)
    matches: list[str] = []
    for key, entry in llm_cfg.items():
        if not isinstance(entry, Mapping):
            continue
        e_prov = str(entry.get("provider") or "").strip().lower()
        e_sys = str(entry.get("system_provider") or "").strip().lower()
        if want_prov not in {p for p in (e_prov, e_sys) if p}:
            continue
        e_mid = norm_id(str(entry.get("model_id") or key))
        if e_mid == want_mid:
            matches.append(str(key))
    return matches


def resolve_model_billing(
    model_key: str | None = None,
    provider: str | None = None,
    model_id: str | None = None,
    *,
    billing_type: str = "",
    model_config: Any = None,
    provider_info: Mapping[str, Any] | None = None,
    dalton_meta: Mapping[str, Any] | None = None,
    dalton_meta_entry: Mapping[str, Any] | None = None,
    snapshot: Mapping[str, Any] | None = None,
) -> BillingKind:
    """Resolve whether a model/route is ``"subscription"`` or ``"cash"`` billed.

    Unified rules (evaluated in priority order):
    1. Explicit ``snapshot`` entry (when ``snapshot`` is supplied and matches the route).
    2. Call-level ``billing_type == "oauth"``.
    3. Provider-level rules: ``provider_info.billing == "subscription"``,
       ``provider == "z-ai-cn-coding"``, or OAuth provider suffix (``*-oauth``).
    4. Model-level ``dalton_meta[key].billing`` (explicit ``"subscription"`` or ``"cash"``),
       or OAuth model key/id suffix (``*-oauth``), scoped strictly to the actual route so a
       custom BYOK alias reusing a subscription display name on a cash route still bills as cash.
    """
    mk = str(model_key or "").strip()
    prov = str(provider or "").strip()
    mid = str(model_id or "").strip()
    btype = str(billing_type or "").strip().lower()

    if isinstance(snapshot, Mapping):
        from src.dalton.services.team_model_catalog.snapshot import _find_snapshot_entry

        snap_entry = _find_snapshot_entry(
            snapshot,
            model_name=mk or None,
            provider=prov or None,
            model_id=mid or None,
        )
        if isinstance(snap_entry, Mapping):
            return (
                "subscription"
                if str(snap_entry.get("billing") or "").strip().lower() == "subscription"
                else "cash"
            )

    if btype == "oauth":
        return "subscription"

    mc = model_config
    if mc is None and (provider_info is None or (dalton_meta is None and dalton_meta_entry is None)):
        try:
            from src.llms.llm import LLM

            mc = LLM.get_model_config()
        except Exception:
            mc = None

    llm_cfg = getattr(mc, "llm_config", None) if mc is not None else None
    if not isinstance(llm_cfg, Mapping):
        llm_cfg = {}

    matched_mk, mk_entry = _lookup_catalog_entry(llm_cfg, mk)
    # Also support callers that pass a manifest key in model_id with model_key omitted
    if matched_mk is None and not mk and mid and not prov:
        matched_mk, mk_entry = _lookup_catalog_entry(llm_cfg, mid)

    if mk_entry is not None:
        mk_is_authoritative = _entry_matches_explicit_route(
            mk_entry, provider=prov, model_id=mid
        )
    elif provider_info is not None or dalton_meta_entry is not None:
        # Save/validation/snapshot callers pass explicit metadata for a model being defined.
        mk_is_authoritative = True
    elif prov and mid:
        # Unknown display alias paired with an explicit stamped route (e.g. custom BYOK alias
        # shadowing a subscription name on a cash route): evaluate the stamped route, not the alias.
        mk_is_authoritative = False
    else:
        mk_is_authoritative = True

    eff_prov = prov
    if not eff_prov and mk_is_authoritative and isinstance(mk_entry, Mapping):
        eff_prov = str(mk_entry.get("provider") or "").strip()

    eff_pinfo: Mapping[str, Any]
    if isinstance(provider_info, Mapping):
        eff_pinfo = provider_info
    elif mc is not None and eff_prov:
        try:
            eff_pinfo = mc.get_provider_info(eff_prov) or {}
        except Exception:
            eff_pinfo = {}
    else:
        eff_pinfo = {}

    prov_billing = str(eff_pinfo.get("billing") or "").strip().lower()
    if (
        prov_billing == "subscription"
        or eff_prov == "z-ai-cn-coding"
        or _is_oauth_name(eff_prov)
    ):
        return "subscription"

    # Model-level dalton_meta.billing (explicit entry takes precedence when supplied)
    if isinstance(dalton_meta_entry, Mapping):
        meta_billing = str(dalton_meta_entry.get("billing") or "").strip().lower()
        if meta_billing == "subscription":
            return "subscription"
        if meta_billing == "cash":
            return "cash"

    meta_map: Mapping[str, Any]
    if isinstance(dalton_meta, Mapping):
        meta_map = dalton_meta
    else:
        # The price-source annotation binds its already-loaded map; every other caller keeps the
        # original load_dalton_meta() path unchanged.
        scoped_meta = _SCOPED_DALTON_META.get()
        if isinstance(scoped_meta, Mapping):
            meta_map = scoped_meta
        else:
            try:
                from src.dalton.llm.overlay import load_dalton_meta

                meta_map = load_dalton_meta() or {}
            except Exception:
                meta_map = {}

    # 1) When the caller's own model_key matched a catalog entry on this route, decide strictly
    # from that key's own dalton_meta (never letting another key on the same (provider, model_id)
    # route override a cash model to subscription, even on legacy mixed data).
    if mk_is_authoritative and mk_entry is not None:
        for cand in (matched_mk, mk):
            if not cand:
                continue
            dm = meta_map.get(cand)
            if not isinstance(dm, Mapping):
                continue
            dm_billing = str(dm.get("billing") or "").strip().lower()
            if dm_billing == "subscription":
                return "subscription"
            if dm_billing == "cash":
                return "cash"
        if _is_oauth_name(mk) or _is_oauth_name(mid):
            return "subscription"
        return "cash"

    route_keys = (
        _find_matching_keys_by_route(llm_cfg, provider=eff_prov, model_id=mid)
        if eff_prov and mid
        else []
    )

    # 2) Model key absent from current llm_cfg (e.g. validation of a new model, or retired
    # mid-flight before settlement): honor explicit dalton_meta[mk] when route does not conflict.
    if mk and (mk_is_authoritative or (mk_entry is None and not route_keys)) and mk in meta_map:
        dm = meta_map.get(mk)
        if isinstance(dm, Mapping):
            dm_billing = str(dm.get("billing") or "").strip().lower()
            if dm_billing == "subscription":
                return "subscription"
            if dm_billing == "cash":
                return "cash"

    # 3) No authoritative model_key matched: fall back to route-level lookup only when
    # unambiguous (all matching keys on (provider, norm_id(model_id)) agree on subscription;
    # on legacy mixed cash+subscription routes, never let a sibling subscription key zero out cash).
    if route_keys:
        route_billings: set[str] = set()
        for rk in route_keys:
            dm = meta_map.get(rk)
            dm_billing = (
                str(dm.get("billing") or "").strip().lower()
                if isinstance(dm, Mapping)
                else ""
            )
            if dm_billing == "subscription":
                route_billings.add("subscription")
            elif dm_billing == "cash":
                route_billings.add("cash")
            elif _is_oauth_name(rk):
                route_billings.add("subscription")
            else:
                route_billings.add("cash")
        if route_billings == {"subscription"}:
            return "subscription"
        return "cash"

    if mid and not mk and not eff_prov:
        dm = meta_map.get(mid)
        if isinstance(dm, Mapping):
            dm_billing = str(dm.get("billing") or "").strip().lower()
            if dm_billing == "subscription":
                return "subscription"
            if dm_billing == "cash":
                return "cash"

    # OAuth suffix fallback on authoritative model_key or explicit model_id
    if (mk_is_authoritative and _is_oauth_name(mk)) or _is_oauth_name(mid):
        return "subscription"

    # Legacy bare-id check: only when no explicit cash provider was specified (e.g. notional callers
    # passing a bare model_id without provider where `<id>-oauth` is subscription-only).
    if not eff_prov and mid:
        oauth_dm = meta_map.get(f"{mid}-oauth")
        base_dm = meta_map.get(mid)
        if (
            not isinstance(base_dm, Mapping)
            and isinstance(oauth_dm, Mapping)
            and str(oauth_dm.get("billing") or "").strip().lower() == "subscription"
        ):
            return "subscription"

    return "cash"


def is_subscription_billing(
    model_key: str | None = None,
    provider: str | None = None,
    model_id: str | None = None,
    *,
    billing_type: str = "",
    model_config: Any = None,
    provider_info: Mapping[str, Any] | None = None,
    dalton_meta: Mapping[str, Any] | None = None,
    dalton_meta_entry: Mapping[str, Any] | None = None,
    snapshot: Mapping[str, Any] | None = None,
) -> bool:
    """Boolean convenience wrapper over :func:`resolve_model_billing`."""
    return (
        resolve_model_billing(
            model_key=model_key,
            provider=provider,
            model_id=model_id,
            billing_type=billing_type,
            model_config=model_config,
            provider_info=provider_info,
            dalton_meta=dalton_meta,
            dalton_meta_entry=dalton_meta_entry,
            snapshot=snapshot,
        )
        == "subscription"
    )
