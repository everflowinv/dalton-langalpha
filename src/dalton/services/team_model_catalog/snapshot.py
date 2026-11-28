"""Turn-scoped ``effective_catalog_snapshot`` persistence, audit lookup, and compare-and-reject resume guard.

At turn START (``conversation_responses.metadata``) and subagent START
(``subagent_runs.replay_facts``), the server persists a self-contained snapshot
of the candidate models for that run:
- Cash pricing card (``pricing``)
- Nominal subscription pricing card (``notional_card``) and ``billing``
- ``input_modalities``, ``context``, ``max_output``, ``compaction_profile``
- ``reasoning`` ladder/default/write surface
- Fixed route (``provider``, ``model_id``)
- Non-secret catalog variant binding identifiers (``bound_base_url``,
  ``canonical_bound_url``, ``key_fingerprint``)

On any resume (main-turn restart resume, ``/retry``, web HITL resume, automation
refire, or subagent resume), the server resolves the model and route normally
from the live catalog and calls :func:`verify_resume_route` **before**
constructing any LLM client. If the resolved route fingerprint matches the
persisted snapshot, the resume proceeds; if any route field changed, the resume
is refused with HTTP 400 ``catalog_route_changed``. Snapshots are strictly
read-only audit/comparison records and are never used to reconstruct old
clients.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from contextlib import aclosing, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from src.dalton.llm.egress_policy import canonical_url_key
from src.dalton.services.team_model_catalog.billing import resolve_model_billing
from src.dalton.services.team_model_catalog.constants import (
    TURN_SNAPSHOT_SCHEMA,
    build_catalog_route_changed_http_exception,
    norm_id,
    normalize_cash_pricing_card,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RouteFingerprint:
    """Live-resolved route fingerprint compared against a persisted run snapshot on resume."""

    model_key: str
    provider: str
    model_id: str
    bound_base_url: str | None = None
    key_fingerprint: str | None = None
    billing: str = "cash"


@dataclass(frozen=True)
class ResumeVerifyTarget:
    """Read-only target run identified by a resume entry point for pre-client route verification."""

    run_id: str | None
    thread_id: str | None = None
    run_row: Mapping[str, Any] | None = None


_ACTIVE_CATALOG_SNAPSHOT: ContextVar[dict[str, Any] | None] = ContextVar(
    "dalton_active_catalog_snapshot", default=None
)
_ACTIVE_RESUME_VERIFY_TARGET: ContextVar[ResumeVerifyTarget | None] = ContextVar(
    "dalton_active_resume_verify_target", default=None
)
_ACTIVE_VERIFIED_RESUME_VARIANT_BINDING: ContextVar[RouteFingerprint | None] = ContextVar(
    "dalton_active_verified_resume_variant_binding", default=None
)


def get_active_catalog_snapshot() -> dict[str, Any] | None:
    """Return the ``effective_catalog_snapshot`` bound to the current async/thread context."""
    return _ACTIVE_CATALOG_SNAPSHOT.get()


def get_active_resume_verify_target() -> ResumeVerifyTarget | None:
    """Return the active :class:`ResumeVerifyTarget` bound for pre-client route comparison."""
    return _ACTIVE_RESUME_VERIFY_TARGET.get()


def get_active_verified_resume_variant_binding() -> RouteFingerprint | None:
    """Return the verified catalog-variant :class:`RouteFingerprint` pinned across client construction."""
    return _ACTIVE_VERIFIED_RESUME_VARIANT_BINDING.get()


@contextmanager
def bind_verified_resume_variant_binding(
    route: RouteFingerprint | None,
) -> Iterator[RouteFingerprint | None]:
    """Pin the verified catalog-variant binding so ``resolve_catalog_variant_binding_*`` catches TOCTOU rotation."""
    if route is None or not route.provider or (route.key_fingerprint is None and route.bound_base_url is None):
        yield None
        return
    token = _ACTIVE_VERIFIED_RESUME_VARIANT_BINDING.set(route)
    try:
        yield route
    finally:
        _ACTIVE_VERIFIED_RESUME_VARIANT_BINDING.reset(token)


@contextmanager
def bind_catalog_snapshot(snapshot: dict[str, Any] | None) -> Iterator[dict[str, Any] | None]:
    """Bind ``snapshot`` to the current execution context for in-flight audit/settlement lookup."""
    token = _ACTIVE_CATALOG_SNAPSHOT.set(snapshot)
    try:
        yield snapshot
    finally:
        _ACTIVE_CATALOG_SNAPSHOT.reset(token)


@contextmanager
def resume_verify_scope(
    run_id: str | None,
    *,
    thread_id: str | None = None,
    run_row: Mapping[str, Any] | None = None,
) -> Iterator[ResumeVerifyTarget | None]:
    """Bind a read-only resume target so ``resolve_llm_config`` runs ``verify_resume_route`` before building clients."""
    cleaned = str(run_id or "").strip() or None
    if cleaned is None and run_row is None:
        yield None
        return
    target = ResumeVerifyTarget(run_id=cleaned, thread_id=thread_id, run_row=run_row)
    token = _ACTIVE_RESUME_VERIFY_TARGET.set(target)
    try:
        yield target
    finally:
        _ACTIVE_RESUME_VERIFY_TARGET.reset(token)


async def run_with_catalog_snapshot(
    snapshot: dict[str, Any] | None,
    stream: AsyncIterator[Any],
) -> AsyncIterator[Any]:
    """Drive a turn's async stream with ``effective_catalog_snapshot`` bound for settlement/audit."""
    if snapshot is None:
        async with aclosing(stream):
            async for event in stream:
                yield event
        return
    token = _ACTIVE_CATALOG_SNAPSHOT.set(snapshot)
    try:
        async with aclosing(stream):
            async for event in stream:
                yield event
    finally:
        with suppress(ValueError):
            _ACTIVE_CATALOG_SNAPSHOT.reset(token)


def _extract_model_key_value(val: Any) -> str | None:
    """Extract a model key string from either a scalar key or a ``SubagentModel`` dict ``{"id": ...}``."""
    if isinstance(val, str) and val.strip():
        return val.strip()
    if isinstance(val, Mapping):
        inner = val.get("id")
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
    return None


def _extract_primary_model_key_from_run(
    run_row: Mapping[str, Any],
    *,
    is_subagent: bool,
    snapshot: Mapping[str, Any] | None = None,
) -> str | None:
    """Extract the actual selected model key of the original persisted run."""
    container_key = "replay_facts" if is_subagent else "metadata"
    container = run_row.get(container_key)
    field_order = (
        ("actual_model_key", "model", "model_override", "llm_model")
        if is_subagent
        else ("llm_model", "main_model", "model", "model_override")
    )
    if isinstance(container, Mapping):
        for fname in field_order:
            extracted = _extract_model_key_value(container.get(fname))
            if extracted:
                return extracted
    for fname in field_order:
        extracted = _extract_model_key_value(run_row.get(fname))
        if extracted:
            return extracted
    if isinstance(snapshot, Mapping):
        models = snapshot.get("models")
        if isinstance(models, Mapping) and len(models) == 1:
            only_key = next(iter(models.keys()))
            if isinstance(only_key, str) and only_key.strip():
                return only_key.strip()
    return None


async def _fetch_persisted_run_row(
    run_id: str,
    *,
    thread_id: str | None = None,
    is_subagent: bool = False,
    conn: Any = None,
) -> dict[str, Any] | None:
    cleaned_id = str(run_id or "").strip()
    if not cleaned_id:
        return None
    from psycopg.rows import dict_row

    from src.server.database.pool import get_db_connection

    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            if is_subagent:
                await cur.execute(
                    """
                    SELECT task_run_id::text AS task_run_id, thread_id, replay_facts
                    FROM subagent_runs
                    WHERE task_run_id = %s::uuid
                    """,
                    (cleaned_id,),
                )
            elif thread_id:
                await cur.execute(
                    """
                    SELECT conversation_response_id::text AS conversation_response_id,
                           conversation_thread_id, metadata
                    FROM conversation_responses
                    WHERE conversation_response_id = %s::uuid
                      AND conversation_thread_id = %s
                    """,
                    (cleaned_id, str(thread_id)),
                )
            else:
                await cur.execute(
                    """
                    SELECT conversation_response_id::text AS conversation_response_id,
                           conversation_thread_id, metadata
                    FROM conversation_responses
                    WHERE conversation_response_id = %s::uuid
                    """,
                    (cleaned_id,),
                )
            row = await cur.fetchone()
    return dict(row) if row else None


def _canonical_url_or_none(url: Any) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    return canonical_url_key(url.strip()) or url.strip()


def _coerce_route_fingerprint(resolved_route: RouteFingerprint | Mapping[str, Any]) -> RouteFingerprint:
    if isinstance(resolved_route, RouteFingerprint):
        return resolved_route
    return RouteFingerprint(
        model_key=str(resolved_route.get("model_key") or "").strip(),
        provider=str(resolved_route.get("provider") or "").strip(),
        model_id=str(resolved_route.get("model_id") or "").strip(),
        bound_base_url=(
            str(resolved_route.get("bound_base_url")).strip()
            if resolved_route.get("bound_base_url")
            else (
                str(resolved_route.get("canonical_bound_url")).strip()
                if resolved_route.get("canonical_bound_url")
                else None
            )
        ),
        key_fingerprint=(
            str(resolved_route.get("key_fingerprint")).strip()
            if resolved_route.get("key_fingerprint")
            else None
        ),
        billing=str(resolved_route.get("billing") or "cash").strip().lower() or "cash",
    )


async def resolve_live_route_fingerprint(
    model_key: str,
    *,
    user_id: str | None = None,
    model_pref: Mapping[str, Any] | None = None,
    ignore_custom_models: bool = False,
    conn: Any = None,
) -> RouteFingerprint:
    """Resolve the current live catalog (or personal custom model) route fingerprint for ``model_key``.

    Reads only live configuration and non-secret credential metadata (never decrypts API keys
    and never overlays a historical snapshot).
    """
    mk = str(model_key or "").strip()
    if not mk:
        return RouteFingerprint(model_key="", provider="", model_id="")

    from src.llms.llm import LLM as LLMFactory
    from src.server.services.llm import user_models

    mc = LLMFactory.get_model_config()

    # Check personal custom model first (matching live classify_model precedence).
    custom_cm: dict[str, Any] | None = None
    if not ignore_custom_models:
        if isinstance(model_pref, Mapping):
            custom_cm = user_models.custom_model(dict(model_pref), mk)
        elif user_id:
            try:
                custom_cm = await user_models.get_custom_model_config(user_id, mk)
            except Exception:
                custom_cm = None

    if isinstance(custom_cm, Mapping):
        c_prov = str(custom_cm.get("provider") or "").strip()
        c_mid = str(custom_cm.get("model_id") or mk).strip()
        c_url = custom_cm.get("base_url")
        if not c_url and c_prov and isinstance(model_pref, Mapping):
            cp = user_models.custom_provider(dict(model_pref), c_prov)
            if isinstance(cp, Mapping):
                c_url = cp.get("base_url")
        if not c_url and c_prov:
            c_url = (mc.get_provider_info(c_prov) or {}).get("base_url")
        c_billing = resolve_model_billing(
            model_key=mk,
            provider=c_prov,
            model_id=c_mid,
            model_config=mc,
        )
        return RouteFingerprint(
            model_key=mk,
            provider=c_prov,
            model_id=c_mid,
            bound_base_url=_canonical_url_or_none(c_url),
            key_fingerprint=None,
            billing=c_billing,
        )

    entry = mc.get_model_config(mk)
    if not isinstance(entry, Mapping):
        return RouteFingerprint(model_key=mk, provider="", model_id="")

    prov = str(entry.get("provider") or "").strip()
    mid = str(entry.get("model_id") or mk).strip()
    prov_info = mc.get_provider_info(prov) if prov else {}
    billing = resolve_model_billing(
        model_key=mk,
        provider=prov,
        model_id=mid,
        model_config=mc,
        provider_info=prov_info,
    )

    bound_url: str | None = None
    key_fp: str | None = None
    if prov_info.get("catalog_defined") is True:
        try:
            from src.dalton.database.team_model_catalog import get_catalog_provider_credential

            cred = await get_catalog_provider_credential(prov, conn=conn, decrypt=False)
        except Exception as exc:
            from fastapi import HTTPException

            logger.warning(
                "[team_model_catalog] Failed to read catalog variant credential for provider=%s: %s",
                prov,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "type": "catalog_credential_check_unavailable",
                    "code": "catalog_credential_check_unavailable",
                    "message": "暂时无法校验团队模型凭据，请稍后重试。",
                    "provider": prov,
                },
            ) from exc
        if isinstance(cred, Mapping):
            bound_url = _canonical_url_or_none(
                cred.get("canonical_bound_url") or cred.get("bound_base_url")
            )
            if cred.get("key_fingerprint"):
                key_fp = str(cred["key_fingerprint"]).strip()
    else:
        bound_url = _canonical_url_or_none(prov_info.get("base_url"))

    return RouteFingerprint(
        model_key=mk,
        provider=prov,
        model_id=mid,
        bound_base_url=bound_url,
        key_fingerprint=key_fp,
        billing=billing,
    )


def extract_snapshot_route_fingerprint(
    snapshot: Mapping[str, Any] | None,
    model_key: str,
) -> RouteFingerprint | None:
    """Extract the 6-tuple :class:`RouteFingerprint` recorded for ``model_key`` in ``snapshot``."""
    if not isinstance(snapshot, Mapping) or not model_key:
        return None
    models = snapshot.get("models")
    if not isinstance(models, Mapping):
        return None
    snap_entry = models.get(model_key)
    if not isinstance(snap_entry, Mapping):
        return None
    snap_prov = str(snap_entry.get("provider") or "").strip()
    snap_mid = str(snap_entry.get("model_id") or model_key).strip()
    snap_billing = str(snap_entry.get("billing") or "cash").strip().lower() or "cash"
    variants = snapshot.get("variants")
    v_entry = variants.get(snap_prov) if isinstance(variants, Mapping) else None
    snap_pinfo = (
        snap_entry.get("provider_info")
        if isinstance(snap_entry.get("provider_info"), Mapping)
        else {}
    )
    snap_url = _canonical_url_or_none(
        snap_entry.get("canonical_bound_url")
        or snap_entry.get("bound_base_url")
        or (
            v_entry.get("canonical_bound_url") or v_entry.get("bound_base_url")
            if isinstance(v_entry, Mapping)
            else None
        )
        or snap_pinfo.get("base_url")
    )
    snap_fp_raw = snap_entry.get("key_fingerprint") or (
        v_entry.get("key_fingerprint") if isinstance(v_entry, Mapping) else None
    )
    snap_fp = str(snap_fp_raw).strip() if snap_fp_raw else None
    return RouteFingerprint(
        model_key=model_key,
        provider=snap_prov,
        model_id=snap_mid,
        bound_base_url=snap_url,
        key_fingerprint=snap_fp,
        billing=snap_billing,
    )


def diff_route_fingerprints(
    snap_route: RouteFingerprint,
    live_route: RouteFingerprint,
) -> dict[str, dict[str, Any]]:
    """Return field-level diffs across all 6 route fingerprint fields."""
    snap_url = _canonical_url_or_none(snap_route.bound_base_url)
    live_url = _canonical_url_or_none(live_route.bound_base_url)
    snap_fp = str(snap_route.key_fingerprint).strip() if snap_route.key_fingerprint else None
    live_fp = str(live_route.key_fingerprint).strip() if live_route.key_fingerprint else None
    diffs: dict[str, dict[str, Any]] = {}
    if live_route.model_key != snap_route.model_key:
        diffs["model_key"] = {"snapshot": snap_route.model_key, "live": live_route.model_key}
    if live_route.provider.lower() != snap_route.provider.lower():
        diffs["provider"] = {"snapshot": snap_route.provider, "live": live_route.provider}
    if live_route.model_id != snap_route.model_id:
        diffs["model_id"] = {"snapshot": snap_route.model_id, "live": live_route.model_id}
    if snap_url != live_url:
        diffs["bound_base_url"] = {"snapshot": snap_url, "live": live_url}
    if snap_fp != live_fp:
        diffs["key_fingerprint"] = {"snapshot": snap_fp, "live": live_fp}
    if live_route.billing != snap_route.billing:
        diffs["billing"] = {"snapshot": snap_route.billing, "live": live_route.billing}
    return diffs


async def verify_candidate_route_against_snapshot(
    model_key: str,
    resolved_route: RouteFingerprint | Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    thread_id: str | None = None,
    is_subagent: bool = False,
    run_row: Mapping[str, Any] | None = None,
    conn: Any = None,
    allow_unrecorded: bool = False,
) -> tuple[bool, dict[str, dict[str, Any]]]:
    """Compare a candidate model's live :class:`RouteFingerprint` against the turn snapshot.

    Used before activating a fallback candidate at runtime or during wakeup/retry resume:
    - When no snapshot exists (legacy run), returns ``(True, {})``.
    - When ``model_key`` is recorded in ``snapshot["models"]``, verifies all 6 fields and
      returns ``(not diffs, diffs)``.
    - When ``model_key`` is absent from ``snapshot["models"]``, returns ``(True, {})`` if
      ``allow_unrecorded=True`` else ``(False, {"model_key": ...})``.
    """
    snap: Mapping[str, Any] | None = snapshot if isinstance(snapshot, Mapping) else None
    if snap is None:
        row: dict[str, Any] | None = dict(run_row) if isinstance(run_row, Mapping) else None
        if row is None and run_id:
            row = await _fetch_persisted_run_row(
                str(run_id),
                thread_id=thread_id,
                is_subagent=is_subagent,
                conn=conn,
            )
        if row is not None:
            container_key = "replay_facts" if is_subagent else "metadata"
            container = row.get(container_key)
            if isinstance(container, Mapping):
                existing = container.get("effective_catalog_snapshot")
                if isinstance(existing, Mapping) and existing.get("schema") == TURN_SNAPSHOT_SCHEMA:
                    snap = existing
    if snap is None:
        snap = get_active_catalog_snapshot()
    if not isinstance(snap, Mapping):
        return True, {}
    models = snap.get("models")
    if not isinstance(models, Mapping) or not models:
        return True, {}

    live = _coerce_route_fingerprint(resolved_route)
    snap_route = extract_snapshot_route_fingerprint(snap, model_key)
    if snap_route is None:
        if allow_unrecorded:
            return True, {}
        return False, {"model_key": {"snapshot": None, "live": live.model_key or model_key}}
    diffs = diff_route_fingerprints(snap_route, live)
    return (len(diffs) == 0), diffs


async def verify_resume_route(
    run_id: str | None,
    resolved_route: RouteFingerprint | Mapping[str, Any],
    *,
    thread_id: str | None = None,
    is_subagent: bool = False,
    run_row: Mapping[str, Any] | None = None,
    snapshot: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> None:
    """Compare ``resolved_route`` against the interrupted run's persisted snapshot before client creation.

    - Legacy runs with no persisted ``effective_catalog_snapshot`` proceed unchanged (returns ``None``).
    - When a persisted snapshot exists for the original run's model (or an unattended run's
      recorded ``active_fallback_route``), verifies all 6 route fields:
      ``(model_key, provider, model_id, bound_base_url, key_fingerprint, billing)``.
    - Raises HTTP 400 (``catalog_route_changed``) on any mismatch so the caller never sends requests
      under a mutated catalog route or attempts to reconstruct a stale client.
    """
    row: dict[str, Any] | None = dict(run_row) if isinstance(run_row, Mapping) else None
    snap: Mapping[str, Any] | None = snapshot if isinstance(snapshot, Mapping) else None

    if snap is None:
        if row is None and run_id:
            row = await _fetch_persisted_run_row(
                str(run_id),
                thread_id=thread_id,
                is_subagent=is_subagent,
                conn=conn,
            )
        if not row:
            return None
        container_key = "replay_facts" if is_subagent else "metadata"
        container = row.get(container_key)
        if not isinstance(container, Mapping):
            return None
        existing = container.get("effective_catalog_snapshot")
        if not isinstance(existing, Mapping) or existing.get("schema") != TURN_SNAPSHOT_SCHEMA:
            return None
        snap = existing

    models = snap.get("models")
    if not isinstance(models, Mapping) or not models:
        return None

    live = _coerce_route_fingerprint(resolved_route)
    orig_model_key: str | None = None
    if row is not None:
        container_key = "replay_facts" if is_subagent else "metadata"
        container = row.get(container_key)
        if isinstance(container, Mapping):
            active_fb = container.get("active_fallback_route")
            if isinstance(active_fb, Mapping):
                fb_mk = _extract_model_key_value(active_fb.get("model_key"))
                if fb_mk and fb_mk in models:
                    orig_model_key = fb_mk
        if not orig_model_key:
            orig_model_key = _extract_primary_model_key_from_run(
                row, is_subagent=is_subagent, snapshot=snap
            )
    else:
        orig_model_key = next(iter(models.keys())) if len(models) == 1 else live.model_key or None
    if not orig_model_key:
        return None

    snap_route = extract_snapshot_route_fingerprint(snap, orig_model_key)
    # When an unattended turn switches during resume to a fallback candidate that was
    # already recorded in the interrupted run's snapshot, verify that candidate's 6-tuple
    # against the original snapshot instead of rejecting the model_key change itself.
    if live.model_key and live.model_key != orig_model_key and live.model_key in models:
        from src.dalton.llm.unattended_fallback import get_active_unattended_turn_context
        from src.dalton.settings import is_unattended_quota_fallback_enabled

        _uf_ctx = get_active_unattended_turn_context()
        if is_unattended_quota_fallback_enabled() and _uf_ctx is not None and _uf_ctx.is_unattended:
            cand_snap = extract_snapshot_route_fingerprint(snap, live.model_key)
            if cand_snap is not None:
                snap_route = cand_snap
    if snap_route is None:
        # Original run was not on a snapshotted team catalog model (e.g. personal custom model).
        return None

    diffs = diff_route_fingerprints(snap_route, live)

    if diffs:
        resolved_run_id = str(
            run_id
            or (
                row.get("task_run_id" if is_subagent else "conversation_response_id")
                if row is not None
                else ""
            )
            or ""
        ).strip() or None
        logger.info(
            "[team_model_catalog] Refusing resume for run=%s due to catalog_route_changed: %s",
            resolved_run_id,
            sorted(diffs.keys()),
        )
        raise build_catalog_route_changed_http_exception(diffs=diffs, run_id=resolved_run_id)
    return None


async def load_run_catalog_snapshot(
    run_id: str,
    *,
    thread_id: str | None = None,
    is_subagent: bool = False,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Load (or conditionally backfill) ``effective_catalog_snapshot`` for audit/settlement paths."""
    row = await _fetch_persisted_run_row(
        run_id, thread_id=thread_id, is_subagent=is_subagent, conn=conn
    )
    if not row:
        return None
    return await ensure_run_catalog_snapshot(row, is_subagent=is_subagent, conn=conn)


_SNAPSHOT_PROVIDER_INFO_KEYS = (
    "sdk",
    "access_type",
    "billing",
    "base_url",
    "use_response_api",
    "use_previous_response_id",
    "prompt_cache_key",
    "default_headers",
    "session_header",
    "parent_provider",
    "catalog_defined",
    "own_api_key",
    "env_key",
)


def serialize_catalog_snapshot(snapshot: Mapping[str, Any]) -> str:
    """Serialize ``snapshot`` with the shared PG-safe JSON encoder (identical across first-write and backfill)."""
    from src.server.utils.pg_sanitize import _safe_dumps

    return _safe_dumps(snapshot)


def _identity_key(provider: str | None, model_id: str | None) -> str:
    """Lossless JSON-safe identity key for ``(provider.lower(), norm_id(model_id))`` (no NUL bytes)."""
    return json.dumps(
        [str(provider or "").strip().lower(), norm_id(model_id)],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _legacy_nul_identity_key(provider: str | None, model_id: str | None) -> str:
    return f"{str(provider or '').strip().lower()}\0{norm_id(model_id)}"


def _legacy_stripped_identity_key(provider: str | None, model_id: str | None) -> str:
    return f"{str(provider or '').strip().lower()}{norm_id(model_id)}"


def _snapshot_provider_info_subset(
    prov_info: Mapping[str, Any] | None,
    *,
    is_catalog_variant: bool,
    bound_base_url: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(prov_info, Mapping):
        for k in _SNAPSHOT_PROVIDER_INFO_KEYS:
            if is_catalog_variant and k == "env_key":
                continue
            val = prov_info.get(k)
            if val is not None:
                out[k] = copy.deepcopy(val)
    if is_catalog_variant:
        out["catalog_defined"] = True
        out["own_api_key"] = True
        out.pop("env_key", None)
        out.pop("models_url", None)
        out.pop("platform", None)
        if bound_base_url:
            out["base_url"] = bound_base_url
    return out


def _extract_cash_pricing_card(
    mc: Any,
    provider: str,
    model_id: str,
) -> dict[str, Any] | None:
    """Resolve the cash pricing card for ``(provider, model_id)`` from ``mc.manifest``.

    Uses the unified provider-before-parent order matching ``pricing_utils.find_model_pricing``:
    within each provider (own provider first, then parent), exact case-insensitive ID/alias
    runs before normalized ID/alias.
    """
    manifest_models = (getattr(mc, "manifest", None) or {}).get("models") or {}
    if not isinstance(manifest_models, Mapping):
        return None
    target_lower = model_id.lower()
    target_norm = norm_id(model_id)
    search_providers: list[str] = []
    if provider:
        search_providers.append(provider)
        parent = mc.get_parent_provider(provider)
        if parent and parent != provider:
            search_providers.append(parent)
    for prov in search_providers:
        cards = manifest_models.get(prov)
        if not isinstance(cards, list):
            continue
        # Pass 1 within provider: exact ID and exact alias (case-insensitive)
        for card in cards:
            if not isinstance(card, Mapping):
                continue
            cid = str(card.get("id") or "")
            if cid.lower() == target_lower:
                pricing = card.get("pricing")
                return normalize_cash_pricing_card(pricing) if isinstance(pricing, Mapping) else None
            aliases = card.get("alias") or []
            if isinstance(aliases, list):
                for alias in aliases:
                    if str(alias).lower() == target_lower:
                        pricing = card.get("pricing")
                        return (
                            normalize_cash_pricing_card(pricing)
                            if isinstance(pricing, Mapping)
                            else None
                        )
        # Pass 2 within provider: normalized ID and normalized alias
        for card in cards:
            if not isinstance(card, Mapping):
                continue
            cid = str(card.get("id") or "")
            if norm_id(cid) == target_norm:
                pricing = card.get("pricing")
                return normalize_cash_pricing_card(pricing) if isinstance(pricing, Mapping) else None
            aliases = card.get("alias") or []
            if isinstance(aliases, list):
                for alias in aliases:
                    if norm_id(str(alias)) == target_norm:
                        pricing = card.get("pricing")
                        return (
                            normalize_cash_pricing_card(pricing)
                            if isinstance(pricing, Mapping)
                            else None
                        )
    return None


def build_effective_catalog_snapshot(
    model_keys: Iterable[str],
    *,
    compiled: Any = None,
    variant_credentials: Mapping[str, Mapping[str, Any]] | None = None,
    excluded_custom_keys: Iterable[str] | None = None,
) -> dict[str, Any] | None:
    """Build a self-contained ``effective_catalog_snapshot`` dict for ``model_keys``.

    Returns ``None`` if any requested model uses a catalog-defined provider variant and
    reading its credential metadata failed (so callers skip persisting an incomplete
    snapshot instead of writing one with ``key_fingerprint=None``).
    """
    from src.dalton.llm.notional_pricing import resolve_notional_card
    from src.dalton.services.team_model_catalog.compiled import get_compiled_catalog_sync

    cat = compiled if compiled is not None else get_compiled_catalog_sync()
    mc = cat.model_config
    llm_cfg = mc.llm_config or {}
    meta_map = cat.dalton_meta or {}

    cleaned_excluded = list(
        dict.fromkeys(
            str(k).strip()
            for k in (excluded_custom_keys or ())
            if str(k or "").strip()
        )
    )
    excluded_set = set(cleaned_excluded)
    unique_keys = [
        k
        for k in dict.fromkeys(str(k).strip() for k in model_keys if str(k or "").strip())
        if k not in excluded_set
    ]
    needs_variant_creds = any(
        isinstance(llm_cfg.get(k), Mapping)
        and bool(
            (mc.get_provider_info(str(llm_cfg[k].get("provider") or "").strip()) or {}).get(
                "catalog_defined"
            )
            is True
        )
        for k in unique_keys
    )

    creds_map: Mapping[str, Mapping[str, Any]] = (
        variant_credentials if variant_credentials is not None else {}
    )
    if variant_credentials is None and needs_variant_creds:
        try:
            from src.dalton.database.team_model_catalog import (
                list_catalog_provider_credentials_sync,
            )

            creds_map = list_catalog_provider_credentials_sync()
        except Exception as exc:
            logger.warning(
                "[team_model_catalog] Skipping effective_catalog_snapshot because variant "
                "credential read failed: %s",
                type(exc).__name__,
            )
            return None

    models_out: dict[str, dict[str, Any]] = {}
    by_identity: dict[str, dict[str, Any]] = {}
    ambiguous_identities: set[str] = set()
    variants_out: dict[str, dict[str, Any]] = {}

    for key in unique_keys:
        entry = llm_cfg.get(key)
        if not isinstance(entry, Mapping):
            continue
        provider = str(entry.get("provider") or "").strip()
        model_id = str(entry.get("model_id") or key).strip()
        prov_info = mc.get_provider_info(provider) if provider else {}
        is_catalog_variant = bool(prov_info.get("catalog_defined"))

        meta_entry = meta_map.get(key) if isinstance(meta_map.get(key), Mapping) else {}
        billing = resolve_model_billing(
            model_key=key,
            provider=provider,
            model_id=model_id,
            model_config=mc,
            provider_info=prov_info,
            dalton_meta=meta_map,
            dalton_meta_entry=meta_entry,
        )
        cash_card = (
            None
            if billing == "subscription"
            else _extract_cash_pricing_card(mc, provider, model_id)
        )
        notional_card = resolve_notional_card(
            key,
            pricing_model_id=model_id,
            pricing_provider=provider,
            index=cat.notional_price_index,
        )

        modalities = entry.get("input_modalities")
        if not isinstance(modalities, list) or not modalities:
            modalities = ["text"]

        params = entry.get("parameters") if isinstance(entry.get("parameters"), Mapping) else {}
        extra_body = (
            entry.get("extra_body") if isinstance(entry.get("extra_body"), Mapping) else {}
        )
        additional_betas = (
            list(entry.get("additional_betas"))
            if isinstance(entry.get("additional_betas"), list)
            else []
        )
        max_output = params.get("max_tokens") or params.get("max_output_tokens")

        bound_base_url: str | None = None
        canonical_bound: str | None = None
        key_fingerprint: str | None = None
        if is_catalog_variant:
            cred = creds_map.get(provider)
            raw_url = (cred or {}).get("bound_base_url") or prov_info.get("base_url")
            if isinstance(raw_url, str) and raw_url.strip():
                bound_base_url = raw_url.strip()
                canonical_bound = (cred or {}).get("canonical_bound_url") or canonical_url_key(
                    bound_base_url
                )
            if cred and cred.get("key_fingerprint"):
                key_fingerprint = str(cred["key_fingerprint"])

        snap_prov_info = _snapshot_provider_info_subset(
            prov_info,
            is_catalog_variant=is_catalog_variant,
            bound_base_url=bound_base_url,
        )
        if is_catalog_variant:
            variants_out[provider] = {
                "provider_slug": provider,
                "bound_base_url": bound_base_url,
                "canonical_bound_url": canonical_bound,
                "key_fingerprint": key_fingerprint,
                "provider_info": copy.deepcopy(snap_prov_info),
            }

        snap_entry: dict[str, Any] = {
            "model_key": key,
            "provider": provider,
            "model_id": model_id,
            "billing": billing,
            "pricing": cash_card,
            "notional_card": notional_card,
            "input_modalities": list(modalities),
            "context": entry.get("context"),
            "max_output": max_output,
            "compaction_profile": entry.get("compaction_profile"),
            "parameters": copy.deepcopy(dict(params)),
            "extra_body": copy.deepcopy(dict(extra_body)),
            "reasoning": copy.deepcopy(entry.get("reasoning"))
            if isinstance(entry.get("reasoning"), Mapping)
            else None,
            "catalog_defined": is_catalog_variant,
            "provider_info": snap_prov_info,
        }
        if additional_betas:
            snap_entry["additional_betas"] = additional_betas
        if entry.get("system_provider") is not None:
            snap_entry["system_provider"] = entry.get("system_provider")
        if entry.get("use_response_api") is not None:
            snap_entry["use_response_api"] = entry.get("use_response_api")
        if is_catalog_variant:
            snap_entry["bound_base_url"] = bound_base_url
            snap_entry["canonical_bound_url"] = canonical_bound
            snap_entry["key_fingerprint"] = key_fingerprint

        models_out[key] = snap_entry
        if provider and model_id:
            id_key = _identity_key(provider, model_id)
            id_record = {
                "model_key": key,
                "provider": provider,
                "model_id": model_id,
                "billing": billing,
                "pricing": cash_card,
                "notional_card": notional_card,
            }
            if id_key in ambiguous_identities:
                pass
            elif id_key in by_identity and by_identity[id_key].get("billing") != billing:
                by_identity.pop(id_key, None)
                ambiguous_identities.add(id_key)
            else:
                by_identity[id_key] = id_record

    snapshot_out: dict[str, Any] = {
        "schema": TURN_SNAPSHOT_SCHEMA,
        "catalog_revision": int(cat.revision),
        "models": models_out,
        "by_identity": by_identity,
        "variants": variants_out,
    }
    if cleaned_excluded:
        snapshot_out["excluded_custom_keys"] = cleaned_excluded
    return snapshot_out


def _entry_matches_request(
    entry: Mapping[str, Any],
    *,
    provider: str | None = None,
    model_id: str | None = None,
) -> bool:
    """Verify ``entry`` matches the actual requested ``provider`` and ``model_id`` when specified."""
    if provider:
        entry_prov = str(entry.get("provider") or "").strip().lower()
        if entry_prov != provider.strip().lower():
            return False
    if model_id:
        entry_mid = norm_id(str(entry.get("model_id") or ""))
        if entry_mid != norm_id(model_id):
            return False
    return True


def _find_snapshot_entry(
    snapshot: Mapping[str, Any] | None,
    *,
    model_name: str | None = None,
    provider: str | None = None,
    model_id: str | None = None,
) -> Mapping[str, Any] | None:
    if not isinstance(snapshot, Mapping):
        return None
    want_id = model_id or model_name
    models = snapshot.get("models")
    # 1) Always match the caller's own model_key first.
    if isinstance(models, Mapping):
        if model_name and isinstance(models.get(model_name), Mapping):
            cand = models[model_name]
            if _entry_matches_request(cand, provider=provider, model_id=model_id):
                return cand
        if model_id and isinstance(models.get(model_id), Mapping):
            cand = models[model_id]
            if _entry_matches_request(cand, provider=provider, model_id=model_id):
                return cand
    # 2) Unkeyed / alias fallback via by_identity, then unambiguous route scan in models.
    by_identity = snapshot.get("by_identity")
    if provider and want_id and isinstance(by_identity, Mapping):
        for key_candidate in (
            _identity_key(provider, want_id),
            _legacy_nul_identity_key(provider, want_id),
            _legacy_stripped_identity_key(provider, want_id),
        ):
            hit = by_identity.get(key_candidate)
            if isinstance(hit, Mapping) and _entry_matches_request(
                hit, provider=provider, model_id=want_id
            ):
                return hit
    if isinstance(models, Mapping) and provider and want_id:
        matches = [
            item
            for item in models.values()
            if isinstance(item, Mapping)
            and _entry_matches_request(item, provider=provider, model_id=want_id)
        ]
        if len(matches) == 1:
            return matches[0]
        if matches and len({str(m.get("billing") or "") for m in matches}) == 1:
            return matches[0]
    return None


def lookup_snapshot_model_entry(
    model_key: str,
    *,
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the persisted model snapshot entry for ``model_key``, or ``None`` if absent."""
    snap = snapshot if snapshot is not None else get_active_catalog_snapshot()
    if not isinstance(snap, Mapping):
        return None
    models = snap.get("models")
    if isinstance(models, Mapping):
        hit = models.get(model_key)
        if isinstance(hit, Mapping):
            return copy.deepcopy(dict(hit))
    return None


def lookup_snapshot_pricing(
    model_name: str,
    provider: str | None = None,
    *,
    model_id: str | None = None,
    snapshot: Mapping[str, Any] | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Return ``(hit_in_snapshot, pricing_card_or_subscription_marker)``."""
    snap = snapshot if snapshot is not None else get_active_catalog_snapshot()
    entry = _find_snapshot_entry(snap, model_name=model_name, provider=provider, model_id=model_id)
    if entry is None:
        return False, None
    if str(entry.get("billing") or "").strip().lower() == "subscription":
        return True, {"pricing_type": "subscription"}
    pricing = entry.get("pricing")
    return True, (normalize_cash_pricing_card(pricing) if isinstance(pricing, Mapping) else None)


def lookup_snapshot_notional(
    model_name: str,
    *,
    pricing_model_id: str | None = None,
    pricing_provider: str | None = None,
    snapshot: Mapping[str, Any] | None = None,
) -> tuple[bool, dict[str, Any] | None, bool]:
    """Return ``(hit_in_snapshot, notional_card, is_subscription)``."""
    snap = snapshot if snapshot is not None else get_active_catalog_snapshot()
    entry = _find_snapshot_entry(
        snap,
        model_name=model_name,
        provider=pricing_provider,
        model_id=pricing_model_id,
    )
    if entry is None:
        return False, None, False
    is_sub = str(entry.get("billing") or "") == "subscription"
    card = entry.get("notional_card")
    return True, (copy.deepcopy(dict(card)) if isinstance(card, Mapping) else None), is_sub


def lookup_snapshot_modalities(
    model_name: str,
    *,
    snapshot: Mapping[str, Any] | None = None,
) -> list[str] | None:
    """Return snapshotted ``input_modalities`` for ``model_name``, or ``None`` if absent."""
    snap = snapshot if snapshot is not None else get_active_catalog_snapshot()
    entry = _find_snapshot_entry(snap, model_name=model_name)
    if entry is None:
        return None
    mods = entry.get("input_modalities")
    if isinstance(mods, list) and mods:
        return [str(m) for m in mods]
    return None


def lookup_snapshot_variant_binding(
    provider_slug: str,
    *,
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return ``{bound_base_url, canonical_bound_url, key_fingerprint}`` from active snapshot."""
    snap = snapshot if snapshot is not None else get_active_catalog_snapshot()
    if not isinstance(snap, Mapping):
        return None
    variants = snap.get("variants")
    if isinstance(variants, Mapping):
        hit = variants.get(provider_slug)
        if isinstance(hit, Mapping):
            return dict(hit)
    models = snap.get("models")
    if isinstance(models, Mapping):
        for entry in models.values():
            if (
                isinstance(entry, Mapping)
                and entry.get("provider") == provider_slug
                and entry.get("catalog_defined")
            ):
                return {
                    "provider_slug": provider_slug,
                    "bound_base_url": entry.get("bound_base_url"),
                    "canonical_bound_url": entry.get("canonical_bound_url"),
                    "key_fingerprint": entry.get("key_fingerprint"),
                }
    return None


def _extract_candidate_models_from_run(
    run_row: Mapping[str, Any],
    extra_model_keys: Iterable[str] | None = None,
) -> list[str]:
    keys: list[str] = []
    for k in extra_model_keys or ():
        if isinstance(k, str) and k.strip():
            keys.append(k.strip())
    for container_key in ("metadata", "replay_facts"):
        container = run_row.get(container_key)
        if not isinstance(container, Mapping):
            continue
        for field_name in ("model", "llm_model", "model_override", "actual_model_key", "main_model"):
            extracted = _extract_model_key_value(container.get(field_name))
            if extracted:
                keys.append(extracted)
        for list_field in ("fallback_models", "unattended_fallback_models"):
            fallbacks = container.get(list_field)
            if isinstance(fallbacks, list):
                for fb in fallbacks:
                    if isinstance(fb, str) and fb.strip():
                        keys.append(fb.strip())
        active_fb = container.get("active_fallback_route")
        if isinstance(active_fb, Mapping):
            fb_mk = _extract_model_key_value(active_fb.get("model_key"))
            if fb_mk:
                keys.append(fb_mk)
    for field_name in ("model", "llm_model"):
        extracted = _extract_model_key_value(run_row.get(field_name))
        if extracted:
            keys.append(extracted)
    return list(dict.fromkeys(keys))


def _extract_excluded_custom_keys_from_run(run_row: Mapping[str, Any]) -> list[str]:
    """Extract persisted ``excluded_custom_keys`` from a run row or its JSON container."""
    excluded: list[str] = []
    sources: list[Any] = [run_row.get("excluded_custom_keys")]
    for container_key in ("metadata", "replay_facts"):
        container = run_row.get(container_key)
        if not isinstance(container, Mapping):
            continue
        sources.append(container.get("excluded_custom_keys"))
        existing_snap = container.get("effective_catalog_snapshot")
        if isinstance(existing_snap, Mapping):
            sources.append(existing_snap.get("excluded_custom_keys"))
    for src in sources:
        if isinstance(src, (list, tuple, set, frozenset)):
            for item in src:
                mk = _extract_model_key_value(item)
                if mk:
                    excluded.append(mk)
    return list(dict.fromkeys(excluded))


async def ensure_run_catalog_snapshot(
    run_row: Mapping[str, Any] | None,
    *,
    model_keys: Iterable[str] | None = None,
    is_subagent: bool = False,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Return persisted ``effective_catalog_snapshot`` or conditionally backfill it for settlement audit."""
    if not isinstance(run_row, Mapping):
        return None
    container_key = "replay_facts" if is_subagent else "metadata"
    container = run_row.get(container_key)
    if isinstance(container, Mapping):
        existing = container.get("effective_catalog_snapshot")
        if (
            isinstance(existing, Mapping)
            and existing.get("schema") == TURN_SNAPSHOT_SCHEMA
            and isinstance(existing.get("models"), Mapping)
        ):
            return dict(existing)

    excluded_custom = _extract_excluded_custom_keys_from_run(run_row)
    excluded_set = set(excluded_custom)
    candidates = [
        k
        for k in _extract_candidate_models_from_run(run_row, model_keys)
        if k not in excluded_set
    ]
    if not candidates and not excluded_custom:
        logger.info(
            "[team_model_catalog] Legacy run missing effective_catalog_snapshot and model metadata; "
            "falling back to pre-feature handling"
        )
        return None

    from src.dalton.database.team_model_catalog import (
        backfill_run_catalog_snapshot,
        list_catalog_provider_credentials,
    )
    from src.dalton.services.team_model_catalog.compiled import get_compiled_catalog_async

    compiled = await get_compiled_catalog_async(conn=conn)
    llm_cfg = compiled.model_config.llm_config or {}
    needs_variant_creds = any(
        isinstance(llm_cfg.get(k), Mapping)
        and (
            (
                compiled.model_config.get_provider_info(
                    str(llm_cfg[k].get("provider") or "").strip()
                )
                or {}
            ).get("catalog_defined")
            is True
        )
        for k in candidates
    )
    creds: Mapping[str, Mapping[str, Any]] | None = {}
    if needs_variant_creds:
        try:
            creds = await list_catalog_provider_credentials(conn=conn)
        except Exception as exc:
            logger.warning(
                "[team_model_catalog] Skipping snapshot backfill because variant credential read failed: %s",
                type(exc).__name__,
            )
            return None

    snapshot = build_effective_catalog_snapshot(
        candidates,
        compiled=compiled,
        variant_credentials=creds,
        excluded_custom_keys=excluded_custom or None,
    )
    if not snapshot or (not snapshot.get("models") and not excluded_custom):
        logger.info(
            "[team_model_catalog] Legacy run models %s missing from current catalog; "
            "cannot backfill snapshot, falling back to pre-feature handling",
            candidates,
        )
        return None

    run_id = str(
        run_row.get("task_run_id" if is_subagent else "conversation_response_id")
        or run_row.get("run_id")
        or ""
    ).strip()
    if run_id:
        try:
            await backfill_run_catalog_snapshot(
                run_id,
                snapshot,
                is_subagent=is_subagent,
                conn=conn,
            )
        except Exception as exc:
            logger.warning(
                "[team_model_catalog] Conditional snapshot backfill failed for run=%s: %s",
                run_id,
                type(exc).__name__,
            )
    return snapshot
