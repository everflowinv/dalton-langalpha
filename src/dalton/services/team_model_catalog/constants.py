"""Constants, normalization helpers, and error types for the team model catalog."""

from __future__ import annotations

import hashlib
import json
from typing import Any

CATALOG_SCHEMA = "dalton-team-model-catalog/1"
TURN_SNAPSHOT_SCHEMA = "dalton-turn-catalog-snapshot/1"
CATALOG_LOCK_KEY = "dalton_team_model_catalog"
CATALOG_CHANGED_CHANNEL = "dalton:team_model_catalog:changed"
CATALOG_REVISION_HEADER = "X-Dalton-Catalog-Revision"
CATALOG_VARIANT_CREDENTIAL_SOURCE_PREFIX = "catalog_variant:"
COMPILED_CATALOG_CACHE_MAXSIZE = 8

VALID_LEGACY_IMPORT_STATUSES = frozenset(
    {"pending", "not_applicable", "imported", "failed", "abandoned"}
)

VALID_CATALOG_ACTIONS = frozenset(
    {
        "init_empty",
        "bootstrap_import",
        "abandon_legacy_file",
        "create_model",
        "update_model",
        "hide_model",
        "unhide_model",
        "retire_model",
        "restore_model",
        "native_sync",
        "native_retire",
        "native_restore",
        "upsert_provider_variant",
        "update_curated",
        "confirm_price",
        "rollback",
    }
)


def empty_catalog_payload() -> dict[str, Any]:
    """Return a fresh empty team model catalog payload dict."""
    return {"schema": CATALOG_SCHEMA}


def norm_id(value: str | None) -> str:
    """Normalize a model/pricing identifier identically to ``find_model_pricing``.

    Lowercases, strips surrounding whitespace, and maps ``'.'`` to ``'-'``.
    """
    return str(value or "").strip().lower().replace(".", "-")


def compute_payload_sha256(payload: dict[str, Any]) -> str:
    """Return deterministic SHA-256 hex digest for a catalog JSON payload."""
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def make_key_fingerprint(api_key: str) -> str:
    """Build a non-reversible, non-secret fingerprint for a variant API key.

    Combines a masked prefix/suffix hint (for admin audit readability) with a
    short SHA-256 digest so two different keys with the same prefix/suffix
    always produce distinct fingerprints for in-flight credential rotation checks.
    """
    cleaned = str(api_key or "")
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12]
    if len(cleaned) >= 8:
        hint = f"{cleaned[:3]}...{cleaned[-4:]}"
    elif len(cleaned) >= 4:
        hint = f"...{cleaned[-4:]}"
    else:
        hint = "***"
    return f"{hint}#{digest}"


def catalog_variant_source_tag(provider_slug: str) -> str:
    """Return the exact credential source tag required for a catalog variant."""
    return f"{CATALOG_VARIANT_CREDENTIAL_SOURCE_PREFIX}{str(provider_slug).strip()}"


class CatalogError(Exception):
    """Base domain error for team model catalog operations."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.extra = dict(extra or {})

    def to_http_detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.extra:
            detail.update(self.extra)
        return detail


class CatalogValidationError(CatalogError):
    """Raised on invalid input or failed pre-save catalog validation."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code, message, status_code=400, extra=extra)


class CatalogConflictError(CatalogError):
    """Raised on CAS revision mismatch, key collision, or blocking references."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code, message, status_code=409, extra=extra)


class CatalogVariantCredentialRevokedError(RuntimeError):
    """Terminal config error raised when an in-flight or resumed call's catalog variant
    credential was deleted, rotated, or rebound to a different URL."""

    code = "catalog_variant_credential_revoked"

    def __init__(self, provider_slug: str, reason: str) -> None:
        self.provider_slug = provider_slug
        self.reason = reason
        super().__init__(
            f"Catalog variant '{provider_slug}' credential binding is no longer valid: {reason}"
        )


class CatalogVariantSecurityError(RuntimeError):
    """Raised when a catalog variant client fails construct-time or send-time invariants."""

    code = "catalog_variant_security_violation"
