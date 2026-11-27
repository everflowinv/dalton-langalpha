"""Process-local immutable credential snapshot and authoritative group read contract (§2.3).

Every consumer (personal integrations, internal Feishu compatibility endpoint,
channel gateway sync) reads deployment-level credentials through this module's
``get_credential_group`` and ``resolve_credential_group`` APIs so source
precedence (``env > saved > vault_readonly / vault_error > unset``) and
single-revision consistency are guaranteed in one place.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from src.dalton.deployment_credentials.catalog import (
    FEISHU_APP_ID_KEY,
    FEISHU_APP_SECRET_KEY,
    get_credential_group_spec,
)
from src.dalton.deployment_credentials.redirect_uri import google_redirect_uri_eligible
from src.dalton.services.channel_vault_credentials import (
    DEDICATED_FEISHU_VAULT_KEYS,
    ChannelVaultCredentialError,
    load_dedicated_feishu_vault_credentials,
)
from src.dalton.settings import get_dalton_public_app_url
from src.server.services.setup_settings import environment as setup_env

logger = logging.getLogger(__name__)

CredentialSource = Literal["env", "saved", "vault_readonly", "vault_error", "unset"]

_EMPTY_MAPPING: Mapping[str, str] = MappingProxyType({})
_FEISHU_APP_KEYS: tuple[str, str] = (FEISHU_APP_ID_KEY, FEISHU_APP_SECRET_KEY)
_VAULT_UNSET_CODES: frozenset[str] = frozenset(
    {"dalton_disabled", "owner_unconfigured", "vault_credentials_missing"}
)


@dataclass(frozen=True)
class CredentialSnapshot:
    """Immutable worker-local view of saved deployment settings and preloaded Feishu Vault."""

    revision: int
    values: MappingProxyType[str, str]
    feishu_vault_pair: Mapping[str, str] | None = None
    feishu_vault_error: bool = False


@dataclass(frozen=True)
class CredentialGroupResult:
    """Authoritative read result returned by ``get_credential_group`` / ``resolve_credential_group``."""

    group: str
    source: CredentialSource
    revision: int
    ready: bool
    values: Mapping[str, str]


_INITIAL_SNAPSHOT = CredentialSnapshot(
    revision=0,
    values=MappingProxyType({}),
    feishu_vault_pair=None,
    feishu_vault_error=False,
)

# Single module-level immutable reference replaced atomically after export(rows) or Vault refresh.
_active_snapshot: CredentialSnapshot = _INITIAL_SNAPSHOT

# Request-scoped detail for the internal Feishu compatibility endpoint so it preserves
# exact ChannelVaultCredentialError codes without issuing a second Vault DB query.
_last_feishu_vault_error: contextvars.ContextVar[ChannelVaultCredentialError | None] = (
    contextvars.ContextVar("_last_feishu_vault_error", default=None)
)


def get_active_snapshot() -> CredentialSnapshot:
    """Return the worker's current immutable ``CredentialSnapshot``."""
    return _active_snapshot


def set_active_snapshot(snapshot: CredentialSnapshot) -> None:
    """Atomically replace the worker's active snapshot via a single reference assignment."""
    global _active_snapshot
    _active_snapshot = snapshot


def build_snapshot(
    revision: int,
    rows: Mapping[str, str],
    *,
    feishu_vault_pair: Mapping[str, str] | None = None,
    feishu_vault_error: bool = False,
) -> CredentialSnapshot:
    """Construct an immutable ``CredentialSnapshot`` from read results."""
    frozen_vault = (
        MappingProxyType(dict(feishu_vault_pair))
        if feishu_vault_pair is not None
        else None
    )
    return CredentialSnapshot(
        revision=int(revision),
        values=MappingProxyType(dict(rows)),
        feishu_vault_pair=frozen_vault,
        feishu_vault_error=bool(feishu_vault_error),
    )


def update_active_snapshot_vault(
    feishu_vault_pair: Mapping[str, str] | None,
    *,
    feishu_vault_error: bool,
) -> CredentialSnapshot:
    """Atomically update only the Vault slot of ``_active_snapshot`` preserving revision and saved rows."""
    current = _active_snapshot
    frozen_vault = (
        MappingProxyType(dict(feishu_vault_pair))
        if feishu_vault_pair is not None
        else None
    )
    updated = CredentialSnapshot(
        revision=current.revision,
        values=current.values,
        feishu_vault_pair=frozen_vault,
        feishu_vault_error=bool(feishu_vault_error),
    )
    set_active_snapshot(updated)
    return updated


def reset_active_snapshot_for_test() -> None:
    """Reset the worker snapshot to its empty initial state (test helper)."""
    set_active_snapshot(_INITIAL_SNAPSHOT)
    _last_feishu_vault_error.set(None)


def consume_last_feishu_vault_error() -> ChannelVaultCredentialError | None:
    """Return and clear the most recent Vault error captured in this request context."""
    err = _last_feishu_vault_error.get()
    _last_feishu_vault_error.set(None)
    return err


def _validate_credential_value(raw: str | None) -> tuple[str | None, bool]:
    """Return ``(cleaned_value, is_invalid)``.

    Missing/blank returns ``(None, False)``. Values containing internal whitespace,
    NUL bytes, or non-printable characters return ``(None, True)``.
    """
    if not isinstance(raw, str):
        return None, False
    val = raw.strip()
    if not val:
        return None, False
    if any(ch.isspace() or ch == "\x00" for ch in val) or not val.isprintable():
        return None, True
    return val, False


def _evaluate_group_from_mapping(
    keys: tuple[str, ...],
    mapping: Mapping[str, str],
    *,
    pair_required: bool,
) -> tuple[bool, Mapping[str, str]]:
    """Evaluate group readiness and resolved values strictly from one mapping."""
    resolved: dict[str, str] = {}
    missing: list[str] = []
    invalid: list[str] = []

    for key in keys:
        cleaned, is_invalid = _validate_credential_value(mapping.get(key))
        if is_invalid:
            invalid.append(key)
        elif cleaned is None:
            missing.append(key)
        else:
            resolved[key] = cleaned

    if invalid:
        return False, _EMPTY_MAPPING
    if pair_required:
        if missing:
            return False, _EMPTY_MAPPING
        return True, MappingProxyType(resolved)
    if not resolved:
        return False, _EMPTY_MAPPING
    return True, MappingProxyType(resolved)


def inspect_feishu_app_incomplete(source: Literal["env", "saved"]) -> tuple[str, str, list[str]]:
    """Return ``(error_code, message, missing_or_invalid_keys)`` when env/saved feishu_app is not ready."""
    mapping = setup_env._snapshot() if source == "env" else get_active_snapshot().values
    missing: list[str] = []
    invalid: list[str] = []
    for key in _FEISHU_APP_KEYS:
        cleaned, is_invalid = _validate_credential_value(mapping.get(key))
        if is_invalid:
            invalid.append(key)
        elif cleaned is None:
            missing.append(key)
    if missing:
        return (
            f"{source}_credentials_missing",
            f"Dedicated Feishu {source} credentials missing or empty: {', '.join(missing)}",
            missing,
        )
    if invalid:
        return (
            f"{source}_credentials_invalid",
            f"Dedicated Feishu {source} credentials have invalid format: {', '.join(invalid)}",
            invalid,
        )
    return (
        f"{source}_credentials_missing",
        f"Dedicated Feishu {source} credentials are not configured.",
        list(_FEISHU_APP_KEYS),
    )


async def query_feishu_vault_pair_strict() -> tuple[
    Mapping[str, str] | None,
    bool,
    ChannelVaultCredentialError | None,
]:
    """Query primary admin Vault strictly without mutating ``user_vault_secrets``.

    Returns ``(pair_or_none, vault_error_flag, original_error_or_none)``.
    All Vault and strict primary-admin lookup exceptions are caught here so
    callers in ``setup_settings`` never fail boot or refresh.
    """
    try:
        creds = await load_dedicated_feishu_vault_credentials(strict_admin_lookup=True)
        return MappingProxyType(dict(creds)), False, None
    except ChannelVaultCredentialError as exc:
        if exc.code in _VAULT_UNSET_CODES:
            return None, False, exc
        logger.warning(
            "Dedicated Feishu Vault preload returned error code=%s",
            exc.code,
        )
        return None, True, exc
    except Exception as exc:
        logger.warning(
            "Dedicated Feishu Vault preload raised unexpected error (%s)",
            type(exc).__name__,
        )
        wrapped = ChannelVaultCredentialError(
            "Dedicated Feishu Vault lookup failed.",
            code="vault_unavailable",
            status_code=503,
        )
        return None, True, wrapped


async def preload_feishu_vault_pair(
    rows: Mapping[str, str],
) -> tuple[Mapping[str, str] | None, bool]:
    """Preload primary admin Vault Feishu pair only when neither env nor saved rows hold ``feishu_app``.

    Returns ``(feishu_vault_pair, feishu_vault_error)``. Never raises.
    """
    if any(setup_env.set_outside_app(key) for key in _FEISHU_APP_KEYS):
        return None, False
    if any(key in rows for key in _FEISHU_APP_KEYS):
        return None, False
    pair, vault_error, _err = await query_feishu_vault_pair_strict()
    return pair, vault_error


def get_credential_group(group: str) -> CredentialGroupResult:
    """Synchronously read one credential group from the active worker snapshot.

    Enforces single-source precedence per group:
    ``env > saved > vault_readonly / vault_error (feishu_app only) > unset``.
    Half-configured ``env`` or ``saved`` groups lock to that source with ``ready=False``
    and empty ``values`` rather than falling back to lower-priority sources.
    """
    spec = get_credential_group_spec(group)
    if spec is None:
        raise ValueError(f"Unknown credential group: {group!r}")

    snap = get_active_snapshot()
    at_start = setup_env._snapshot()

    # 1. Startup environment lock (any key non-empty in _at_start locks the whole group).
    if any(setup_env.set_outside_app(key) for key in spec.keys):
        ready, values = _evaluate_group_from_mapping(
            spec.keys, at_start, pair_required=spec.pair_required
        )
        return CredentialGroupResult(
            group=group,
            source="env",
            revision=snap.revision,
            ready=ready,
            values=values,
        )

    # 2. Saved DB snapshot at a single REPEATABLE READ revision.
    if any(key in snap.values for key in spec.keys):
        ready, values = _evaluate_group_from_mapping(
            spec.keys, snap.values, pair_required=spec.pair_required
        )
        return CredentialGroupResult(
            group=group,
            source="saved",
            revision=snap.revision,
            ready=ready,
            values=values,
        )

    # 3. Primary admin Vault read-only fallback (Batch 1: feishu_app only).
    if spec.vault_fallback:
        if snap.feishu_vault_error:
            return CredentialGroupResult(
                group=group,
                source="vault_error",
                revision=snap.revision,
                ready=False,
                values=_EMPTY_MAPPING,
            )
        if snap.feishu_vault_pair is not None:
            ready, values = _evaluate_group_from_mapping(
                spec.keys, snap.feishu_vault_pair, pair_required=spec.pair_required
            )
            return CredentialGroupResult(
                group=group,
                source="vault_readonly",
                revision=snap.revision,
                ready=ready,
                values=values,
            )

    # 4. Unset.
    return CredentialGroupResult(
        group=group,
        source="unset",
        revision=snap.revision,
        ready=False,
        values=_EMPTY_MAPPING,
    )


async def resolve_credential_group(group: str) -> CredentialGroupResult:
    """Asynchronously resolve one credential group using the same single-source precedence.

    When ``group == "feishu_app"`` and neither startup ``env`` nor ``saved`` rows hold
    any ``feishu_app`` key, queries the primary administrator's Vault live and atomically
    updates this worker's ``_active_snapshot`` Vault slot before returning.
    """
    spec = get_credential_group_spec(group)
    if spec is None:
        raise ValueError(f"Unknown credential group: {group!r}")

    snap = get_active_snapshot()
    at_start = setup_env._snapshot()

    # 1. Startup environment lock.
    if any(setup_env.set_outside_app(key) for key in spec.keys):
        ready, values = _evaluate_group_from_mapping(
            spec.keys, at_start, pair_required=spec.pair_required
        )
        return CredentialGroupResult(
            group=group,
            source="env",
            revision=snap.revision,
            ready=ready,
            values=values,
        )

    # 2. Saved DB snapshot.
    if any(key in snap.values for key in spec.keys):
        ready, values = _evaluate_group_from_mapping(
            spec.keys, snap.values, pair_required=spec.pair_required
        )
        return CredentialGroupResult(
            group=group,
            source="saved",
            revision=snap.revision,
            ready=ready,
            values=values,
        )

    # 3. Live Vault lookup for feishu_app with atomic worker-slot update.
    if spec.vault_fallback:
        _last_feishu_vault_error.set(None)
        pair, vault_error, err = await query_feishu_vault_pair_strict()
        if err is not None:
            _last_feishu_vault_error.set(err)
        updated = update_active_snapshot_vault(pair, feishu_vault_error=vault_error)
        if vault_error:
            return CredentialGroupResult(
                group=group,
                source="vault_error",
                revision=updated.revision,
                ready=False,
                values=_EMPTY_MAPPING,
            )
        if pair is not None:
            ready, values = _evaluate_group_from_mapping(
                spec.keys, pair, pair_required=spec.pair_required
            )
            return CredentialGroupResult(
                group=group,
                source="vault_readonly",
                revision=updated.revision,
                ready=ready,
                values=values,
            )
        return CredentialGroupResult(
            group=group,
            source="unset",
            revision=updated.revision,
            ready=False,
            values=_EMPTY_MAPPING,
        )

    # 4. Unset.
    return CredentialGroupResult(
        group=group,
        source="unset",
        revision=snap.revision,
        ready=False,
        values=_EMPTY_MAPPING,
    )


async def resolve_feishu_app_credentials() -> CredentialGroupResult:
    """Resolve the ``feishu_app`` credential group (live Vault refresh when sourced from Vault)."""
    return await resolve_credential_group("feishu_app")


def get_feishu_event_credentials() -> CredentialGroupResult:
    """Synchronously read the ``feishu_event`` credential group from the active snapshot."""
    return get_credential_group("feishu_event")


def google_oauth_ready(*, public_app_url: str | None = None) -> bool:
    """Return True when ``google_oauth`` credentials are complete and the redirect URI is eligible."""
    if not get_credential_group("google_oauth").ready:
        return False
    url = get_dalton_public_app_url() if public_app_url is None else public_app_url
    eligible, _reason = google_redirect_uri_eligible(url)
    return eligible


def feishu_app_ready() -> bool:
    """Return True when ``feishu_app`` credentials are complete in the active snapshot."""
    return get_credential_group("feishu_app").ready


def active_deployment_secret_values() -> tuple[tuple[str, str], ...]:
    """Return ``(env_name, value)`` pairs for active deployment secrets (for redaction)."""
    from src.dalton.deployment_credentials.catalog import DEPLOYMENT_SECRET_ENV_NAMES
    import os

    snap_values = get_active_snapshot().values
    seen: dict[str, str] = {}
    for name in DEPLOYMENT_SECRET_ENV_NAMES:
        for raw in (os.environ.get(name), snap_values.get(name)):
            if isinstance(raw, str) and raw:
                seen[name] = raw
                break
    return tuple(seen.items())


__all__ = [
    "CredentialGroupResult",
    "CredentialSnapshot",
    "CredentialSource",
    "DEDICATED_FEISHU_VAULT_KEYS",
    "active_deployment_secret_values",
    "build_snapshot",
    "consume_last_feishu_vault_error",
    "feishu_app_ready",
    "get_active_snapshot",
    "get_credential_group",
    "get_feishu_event_credentials",
    "google_oauth_ready",
    "inspect_feishu_app_incomplete",
    "preload_feishu_vault_pair",
    "query_feishu_vault_pair_strict",
    "reset_active_snapshot_for_test",
    "resolve_credential_group",
    "resolve_feishu_app_credentials",
    "set_active_snapshot",
    "update_active_snapshot_vault",
]
