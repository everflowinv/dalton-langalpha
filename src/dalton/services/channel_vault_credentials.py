"""Narrowly scoped startup loader for dedicated Dalton Feishu application credentials.

Reads exclusively ``DALTON_FEISHU_APP_ID`` and ``DALTON_FEISHU_APP_SECRET`` from the
primary Dalton administrator's encrypted Vault (`user_vault_secrets`) for the channel
gateway startup path. Never accepts caller-selected owners or secret names, and never
includes secret values or raw decryption exceptions in logs or error payloads.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
import logging

from src.dalton.settings import get_configured_pm_user_ids, get_dalton_settings
from src.server.database.user_vault_secrets import get_user_secrets_decrypted

logger = logging.getLogger(__name__)

DEDICATED_FEISHU_VAULT_KEYS: tuple[str, str] = (
    "DALTON_FEISHU_APP_ID",
    "DALTON_FEISHU_APP_SECRET",
)


class ChannelVaultCredentialError(Exception):
    """Safe, redacted startup credential error containing at most missing/invalid key names."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int = 503,
        missing: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.missing = missing


async def load_dedicated_feishu_vault_credentials(
    *,
    get_secrets_fn: Callable[[str, Collection[str] | None], Awaitable[dict[str, str]]] | None = None,
) -> dict[str, str]:
    """Load the two dedicated Feishu credentials from the primary Dalton administrator's Vault."""
    if not get_dalton_settings().enabled:
        raise ChannelVaultCredentialError(
            "Dalton mode is disabled.",
            code="dalton_disabled",
            status_code=503,
        )

    pm_ids = get_configured_pm_user_ids()
    owner_user_id = str(pm_ids[0]).strip() if pm_ids else ""
    if not owner_user_id or any(ch.isspace() or ch == "\x00" for ch in owner_user_id):
        raise ChannelVaultCredentialError(
            "Primary Dalton administrator is not configured.",
            code="owner_unconfigured",
            status_code=503,
        )

    eff_get_secrets = (
        get_secrets_fn if get_secrets_fn is not None else get_user_secrets_decrypted
    )

    try:
        raw_secrets = await eff_get_secrets(owner_user_id, DEDICATED_FEISHU_VAULT_KEYS)
    except Exception as exc:
        logger.error(
            "Dedicated Feishu Vault lookup failed (%s)",
            type(exc).__name__,
        )
        raise ChannelVaultCredentialError(
            "Dedicated Feishu Vault lookup failed.",
            code="vault_unavailable",
            status_code=503,
        ) from None

    if not isinstance(raw_secrets, dict):
        raise ChannelVaultCredentialError(
            "Dedicated Feishu Vault returned an invalid mapping.",
            code="vault_unavailable",
            status_code=503,
        )

    cleaned: dict[str, str] = {}
    missing: list[str] = []
    invalid: list[str] = []

    for key in DEDICATED_FEISHU_VAULT_KEYS:
        raw_val = raw_secrets.get(key)
        val = raw_val.strip() if isinstance(raw_val, str) else ""
        if not val:
            missing.append(key)
            continue
        if any(ch.isspace() or ch == "\x00" for ch in val):
            invalid.append(key)
            continue
        cleaned[key] = val

    if missing:
        logger.warning(
            "Dedicated Feishu Vault credentials missing or empty: %s",
            ", ".join(missing),
        )
        raise ChannelVaultCredentialError(
            f"Dedicated Feishu Vault credentials missing or empty: {', '.join(missing)}",
            code="vault_credentials_missing",
            status_code=503,
            missing=tuple(missing),
        )

    if invalid:
        logger.warning(
            "Dedicated Feishu Vault credentials have invalid format: %s",
            ", ".join(invalid),
        )
        raise ChannelVaultCredentialError(
            f"Dedicated Feishu Vault credentials have invalid format: {', '.join(invalid)}",
            code="vault_credentials_invalid",
            status_code=503,
            missing=tuple(invalid),
        )

    return {
        "DALTON_FEISHU_APP_ID": cleaned["DALTON_FEISHU_APP_ID"],
        "DALTON_FEISHU_APP_SECRET": cleaned["DALTON_FEISHU_APP_SECRET"],
    }
