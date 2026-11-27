"""Lifecycle hooks connecting upstream ``setup_settings`` and ``vault_invalidation`` to Dalton.

Upstream files keep only 1–5 line thin hooks marked ``DALTON-HOOK(deployment-credentials)``
that delegate all Dalton-specific logic here.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from src.dalton.deployment_credentials.snapshot import (
    build_snapshot,
    preload_feishu_vault_pair,
    set_active_snapshot,
)
from src.dalton.deployment_credentials.store import read_settings_snapshot
from src.dalton.services.channel_vault_credentials import DEDICATED_FEISHU_VAULT_KEYS
from src.dalton.settings import is_dalton_enabled

logger = logging.getLogger(__name__)

_DEDICATED_FEISHU_KEY_SET = frozenset(DEDICATED_FEISHU_VAULT_KEYS)


@dataclass(frozen=True)
class DaltonReadState:
    """Consistent snapshot bundle fetched under ``setup_settings.apply._lock``."""

    revision: int
    rows: dict[str, str]
    feishu_vault_pair: Mapping[str, str] | None
    feishu_vault_error: bool


async def read_dalton_snapshot_state() -> DaltonReadState | None:
    """Read ``(revision, rows)`` and preload Feishu Vault when Dalton mode is enabled.

    Returns ``None`` when Dalton is disabled so upstream OSS deployments follow
    their original ``read_deployment_settings()`` path unchanged.
    """
    if not is_dalton_enabled():
        return None
    revision, rows = await read_settings_snapshot()
    vault_pair, vault_error = await preload_feishu_vault_pair(rows)
    return DaltonReadState(
        revision=revision,
        rows=rows,
        feishu_vault_pair=vault_pair,
        feishu_vault_error=vault_error,
    )


def commit_dalton_snapshot(state: DaltonReadState) -> None:
    """Atomically install ``state`` as this worker's immutable ``CredentialSnapshot``."""
    set_active_snapshot(
        build_snapshot(
            state.revision,
            state.rows,
            feishu_vault_pair=state.feishu_vault_pair,
            feishu_vault_error=state.feishu_vault_error,
        )
    )


def on_deployment_settings_boot_loaded(
    exported: set[str],
    rows: Mapping[str, str],
    revision: int,
) -> None:
    """Post-boot hook invoked after ``export(rows)`` completes during ``load_at_boot()``."""
    _ = (exported, rows, revision)


async def on_deployment_settings_refreshed(
    changed: set[str],
    rows: Mapping[str, str],
    revision: int,
) -> None:
    """Post-refresh hook invoked before ``_unapplied`` is cleared in ``apply.refresh()``."""
    _ = (changed, rows, revision)


async def on_vault_secrets_changed(secret_names: Iterable[str]) -> None:
    """Refresh deployment settings and broadcast when dedicated Feishu Vault keys mutate.

    Never raises so the caller's vault invalidation pipeline continues unimpeded.
    """
    if not is_dalton_enabled():
        return
    touched = _DEDICATED_FEISHU_KEY_SET & set(secret_names)
    if not touched:
        return

    from src.server.services.setup_settings import apply as setup_apply
    from src.server.services.setup_settings import follow as setup_follow

    try:
        await setup_apply.refresh()
    except Exception as exc:
        logger.warning(
            "Refreshing deployment settings after Vault change failed (%s)",
            type(exc).__name__,
        )
    finally:
        try:
            await setup_follow.publish_changed()
        except Exception as exc:
            logger.warning(
                "Publishing deployment settings change after Vault mutation failed (%s)",
                type(exc).__name__,
            )
