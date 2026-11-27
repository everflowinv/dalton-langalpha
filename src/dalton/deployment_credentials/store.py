"""Single-snapshot REPEATABLE READ storage and transactional group writes (§2.3, §3.3).

All writes to ``deployment_settings`` in Dalton go through ``write_settings()``,
which runs on one database connection and one transaction under a transactional
advisory lock, verifies ``expected_revision`` via ``FOR UPDATE``, validates
allowlist / env locks / Batch 1 Feishu Vault read-only protection / group
completeness, encrypts via ``pgcrypto``, bumps ``revision``, and appends to the
append-only ``dalton_deployment_settings_audit`` table.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.errors import ExternalRoutineInvocationException

from src.dalton.deployment_credentials.catalog import (
    CREDENTIAL_GROUP_SPECS,
    FEISHU_APP_ID_KEY,
    FEISHU_APP_SECRET_KEY,
    DaltonDeploymentFieldSpec,
    get_credential_group_spec,
    get_field_spec,
    group_keys,
    writable_field_names,
)
from src.dalton.deployment_credentials.snapshot import query_feishu_vault_pair_strict
from src.dalton.profile import DeploymentProfile, resolve_profile
from src.server.database.encryption import encryption_configured, get_encryption_key
from src.server.database.pool import get_db_connection
from src.server.services.setup_settings import environment as setup_env

logger = logging.getLogger(__name__)

_MAX_VALUE_LENGTH = 4096
_ADVISORY_LOCK_KEY = "dalton_deployment_settings"
_FEISHU_APP_KEY_SET = frozenset({FEISHU_APP_ID_KEY, FEISHU_APP_SECRET_KEY})


class DeploymentSettingValidationError(ValueError):
    """Structured 422 validation error carrying only ``setting_name`` and reason codes (zero raw values)."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("Deployment settings validation failed")
        self.errors = errors


class DeploymentSettingConflictError(RuntimeError):
    """Structured 409 conflict error (``revision_conflict`` / ``env_locked`` / ``feishu_bot_readonly_in_batch1``)."""

    def __init__(
        self,
        code: str,
        *,
        setting_name: str | None = None,
        current_revision: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.setting_name = setting_name
        self.current_revision = current_revision


class DeploymentSettingEncryptionUnavailableError(RuntimeError):
    """Raised when ``BYOK_ENCRYPTION_KEY`` is missing during a write."""

    code = "encryption_key_unconfigured"


@dataclass(frozen=True)
class WriteResult:
    """Result returned after committing a deployment settings transaction."""

    revision: int
    changed_names: tuple[str, ...]
    pending_apply: bool = False
    warnings: tuple[str, ...] = ()


def _col(row: Any, index: int, key: str) -> Any:
    """Extract a column from either a tuple row or a psycopg ``dict_row``."""
    if isinstance(row, Mapping):
        return row.get(key)
    return row[index]


def _secret_fingerprint(enc_key: str, plaintext: str) -> str:
    """Compute the 12-hex-character ``HMAC-SHA256(BYOK_ENCRYPTION_KEY, plaintext)[:12]`` audit fingerprint."""
    return hmac.new(
        enc_key.encode("utf-8"),
        plaintext.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:12]


def _drop_incomplete_failed_groups(
    values: dict[str, str],
    failed_names: set[str],
) -> None:
    """Strip entire ``pair_required=True`` groups when any member key failed decryption."""
    if not failed_names:
        return
    for group_name, group_spec in CREDENTIAL_GROUP_SPECS.items():
        if not group_spec.pair_required:
            continue
        if failed_names & set(group_spec.keys):
            for key in group_spec.keys:
                values.pop(key, None)
            logger.warning(
                "Dropped incomplete credential group %s after row decryption failure",
                group_name,
            )


async def _decrypt_rows_in_transaction(
    conn: Any,
    cur: Any,
    names: list[str],
) -> tuple[dict[str, str], set[str]]:
    """Decrypt each row in ``names`` inside its own nested SAVEPOINT on ``conn``."""
    if not names:
        return {}, set()
    if not encryption_configured():
        logger.warning(
            "Saved deployment settings are ignored: BYOK_ENCRYPTION_KEY is not set"
        )
        return {}, set()
    enc_key = get_encryption_key()
    decrypted: dict[str, str] = {}
    failed_names: set[str] = set()
    for name in names:
        try:
            async with conn.transaction():
                await cur.execute(
                    "SELECT pgp_sym_decrypt(value, %s) AS plaintext "
                    "FROM deployment_settings WHERE name = %s",
                    (enc_key, name),
                )
                row = await cur.fetchone()
        except ExternalRoutineInvocationException as exc:
            failed_names.add(name)
            logger.warning(
                "Deployment setting %s failed decryption (%s); ignoring row",
                name,
                type(exc).__name__,
            )
            continue
        if row is not None:
            plaintext = _col(row, 0, "plaintext")
            if isinstance(plaintext, str):
                decrypted[name] = plaintext
    _drop_incomplete_failed_groups(decrypted, failed_names)
    return decrypted, failed_names


async def read_settings_snapshot_detailed(
    *,
    conn: Any = None,
) -> tuple[int, dict[str, str], dict[str, datetime]]:
    """Read ``(revision, decrypted_values, updated_at_by_name)`` in one REPEATABLE READ snapshot."""

    async def _run(db_conn: Any) -> tuple[int, dict[str, str], dict[str, datetime]]:
        async with db_conn.transaction():
            async with db_conn.cursor() as cur:
                await cur.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                await cur.execute(
                    "SELECT revision FROM dalton_deployment_settings_meta WHERE singleton = TRUE"
                )
                meta_row = await cur.fetchone()
                revision = int(_col(meta_row, 0, "revision")) if meta_row is not None else 0

                await cur.execute(
                    "SELECT name, updated_at FROM deployment_settings ORDER BY name"
                )
                name_rows = await cur.fetchall()
                names: list[str] = []
                updated_at_map: dict[str, datetime] = {}
                for r in name_rows or ():
                    key = str(_col(r, 0, "name"))
                    ts = _col(r, 1, "updated_at")
                    names.append(key)
                    if isinstance(ts, datetime):
                        updated_at_map[key] = ts

                values, _failed = await _decrypt_rows_in_transaction(db_conn, cur, names)
                # Only keep timestamps for rows that remain valid in the snapshot.
                valid_timestamps = {k: v for k, v in updated_at_map.items() if k in values}
                return revision, values, valid_timestamps

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as owned:
        return await _run(owned)


async def read_settings_snapshot(*, conn: Any = None) -> tuple[int, dict[str, str]]:
    """Read ``(revision, decrypted_values)`` in one ``REPEATABLE READ READ ONLY`` transaction."""
    revision, values, _updated_at = await read_settings_snapshot_detailed(conn=conn)
    return revision, values


def _normalize_single_value(
    name: str,
    raw: Any,
    spec: DaltonDeploymentFieldSpec,
) -> tuple[str | None, str | None]:
    """Normalize one submitted value according to ``spec.input_kind``.

    Returns ``(normalized_or_none, error_reason_or_none)``. Never echoes raw input.
    """
    if raw is None:
        return None, None

    if spec.input_kind == "boolean":
        if isinstance(raw, bool):
            return ("true" if raw else "false"), None
        if isinstance(raw, str):
            cleaned = raw.strip().lower()
            if cleaned in ("true", "false"):
                return cleaned, None
        return None, "non_printable"

    if spec.input_kind == "boolean_one":
        if isinstance(raw, bool):
            return ("1" if raw else None), None
        if isinstance(raw, str):
            cleaned = raw.strip().lower()
            if cleaned in ("1", "true"):
                return "1", None
            if cleaned in ("0", "false", ""):
                return None, None
        return None, "non_printable"

    if not isinstance(raw, str):
        return None, "non_printable"

    stripped = raw.strip()
    if not stripped:
        return None, None

    if len(stripped) > _MAX_VALUE_LENGTH:
        return None, "value_too_long"

    if spec.input_kind == "textarea":
        if "\x00" in stripped or any(
            (not ch.isprintable() and ch not in ("\n", "\r", "\t")) for ch in stripped
        ):
            return None, "non_printable"
    else:
        if "\x00" in stripped or not stripped.isprintable():
            return None, "non_printable"

    return stripped, None


def _validate_and_normalize_changes(
    changes: Mapping[str, Any],
    *,
    profile: DeploymentProfile,
) -> dict[str, str | None]:
    """Validate allowlist and field values without ever including raw values in errors."""
    allowed = writable_field_names(profile=profile)
    errors: list[dict[str, str]] = []
    normalized: dict[str, str | None] = {}

    for name, raw in changes.items():
        if name not in allowed:
            errors.append({"setting_name": str(name), "reason": "unknown_setting"})
            continue
        spec = get_field_spec(name, profile=profile)
        if spec is None:
            errors.append({"setting_name": str(name), "reason": "unknown_setting"})
            continue
        value, reason = _normalize_single_value(name, raw, spec)
        if reason is not None:
            errors.append({"setting_name": name, "reason": reason})
            continue
        normalized[name] = value

    if errors:
        raise DeploymentSettingValidationError(errors)
    return normalized


async def write_settings(
    changes: Mapping[str, Any],
    *,
    expected_revision: int | None,
    actor_user_id: str,
    client_ip: str | None = None,
    profile: DeploymentProfile | None = None,
) -> WriteResult:
    """Validate and commit deployment settings atomically on one DB connection and transaction.

    Follows §2.3 steps 1–6:
    1. Transactional advisory lock ``pg_advisory_xact_lock(hashtext('dalton_deployment_settings'))``
    2. ``FOR UPDATE`` lock on ``dalton_deployment_settings_meta`` + ``expected_revision`` check
    3. ``env_locked`` / ``feishu_bot_readonly_in_batch1`` / ``incomplete_credential_group`` checks
    4. Per-row encrypted upsert or ``DELETE``
    5. On substantive changes: ``revision + 1`` and same-transaction append to audit log
    6. Post-commit ``try/except/finally`` local ``apply.refresh()`` and ``follow.publish_changed()``
    """
    active_profile = resolve_profile() if profile is None else profile
    normalized = _validate_and_normalize_changes(changes, profile=active_profile)

    if not encryption_configured():
        raise DeploymentSettingEncryptionUnavailableError(
            "BYOK_ENCRYPTION_KEY is not configured"
        )
    enc_key = get_encryption_key()

    # Check env locks early as well as inside the transaction after reading DB state.
    def _assert_not_env_locked() -> None:
        for name in normalized:
            spec = get_field_spec(name, profile=active_profile)
            if setup_env.set_outside_app(name):
                raise DeploymentSettingConflictError("env_locked", setting_name=name)
            if spec is not None and spec.credential_group:
                for member in group_keys(spec.credential_group):
                    if setup_env.set_outside_app(member):
                        raise DeploymentSettingConflictError("env_locked", setting_name=name)

    changed_names: list[str] = []
    new_revision = 0

    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                # Step 1: serialize all writers via transactional advisory lock.
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (_ADVISORY_LOCK_KEY,),
                )

                # Step 2: lock singleton meta row and verify expected_revision.
                await cur.execute(
                    "SELECT revision FROM dalton_deployment_settings_meta "
                    "WHERE singleton = TRUE FOR UPDATE"
                )
                meta_row = await cur.fetchone()
                if meta_row is None:
                    await cur.execute(
                        "INSERT INTO dalton_deployment_settings_meta (singleton, revision) "
                        "VALUES (TRUE, 0) ON CONFLICT DO NOTHING"
                    )
                    await cur.execute(
                        "SELECT revision FROM dalton_deployment_settings_meta "
                        "WHERE singleton = TRUE FOR UPDATE"
                    )
                    meta_row = await cur.fetchone()
                current_revision = int(_col(meta_row, 0, "revision")) if meta_row is not None else 0

                if expected_revision is None or expected_revision != current_revision:
                    raise DeploymentSettingConflictError(
                        "revision_conflict",
                        current_revision=current_revision,
                    )

                # Step 3: read current DB rows inside the locked transaction and run guard checks.
                _assert_not_env_locked()

                await cur.execute("SELECT name FROM deployment_settings ORDER BY name")
                existing_name_rows = await cur.fetchall()
                existing_names = {str(_col(r, 0, "name")) for r in (existing_name_rows or ())}
                existing_values, _failed = await _decrypt_rows_in_transaction(
                    conn, cur, sorted(existing_names)
                )

                # Batch 1 Feishu Vault read-only protection (only when touching feishu_app
                # and neither env nor DB currently holds feishu_app keys).
                touched_feishu_app = _FEISHU_APP_KEY_SET & set(normalized.keys())
                if touched_feishu_app and not (existing_names & _FEISHU_APP_KEY_SET):
                    vault_pair, vault_error, _err = await query_feishu_vault_pair_strict()
                    if vault_pair is not None or vault_error:
                        raise DeploymentSettingConflictError(
                            "feishu_bot_readonly_in_batch1",
                            setting_name=sorted(touched_feishu_app)[0],
                        )

                # Group completeness check for every touched pair_required=True group.
                touched_groups: set[str] = set()
                for name in normalized:
                    spec = get_field_spec(name, profile=active_profile)
                    if spec is not None and spec.credential_group:
                        touched_groups.add(spec.credential_group)

                group_errors: list[dict[str, str]] = []
                for group_name in sorted(touched_groups):
                    group_spec = get_credential_group_spec(group_name)
                    if group_spec is None or not group_spec.pair_required:
                        continue
                    merged_present: list[str] = []
                    merged_missing: list[str] = []
                    for key in group_spec.keys:
                        eff = (
                            normalized[key]
                            if key in normalized
                            else existing_values.get(key)
                        )
                        if eff:
                            merged_present.append(key)
                        else:
                            merged_missing.append(key)
                    if merged_present and merged_missing:
                        for missing_key in merged_missing:
                            group_errors.append(
                                {
                                    "setting_name": missing_key,
                                    "reason": "incomplete_credential_group",
                                }
                            )
                if group_errors:
                    raise DeploymentSettingValidationError(group_errors)

                # Step 4: apply substantive upserts and deletes, collecting audit rows.
                audit_entries: list[tuple[str, str, str, str | None]] = []
                for name, new_val in normalized.items():
                    spec = get_field_spec(name, profile=active_profile)
                    assert spec is not None
                    had_row = name in existing_names
                    old_val = existing_values.get(name)

                    if new_val is None:
                        if not had_row:
                            continue
                        await cur.execute(
                            "DELETE FROM deployment_settings WHERE name = %s",
                            (name,),
                        )
                        changed_names.append(name)
                        audit_entries.append((name, "clear", "saved", None))
                    else:
                        if had_row and old_val == new_val:
                            continue
                        await cur.execute(
                            """
                            INSERT INTO deployment_settings (name, value, updated_at)
                            VALUES (%s, pgp_sym_encrypt(%s, %s), NOW())
                            ON CONFLICT (name) DO UPDATE
                            SET value = EXCLUDED.value, updated_at = NOW()
                            """,
                            (name, new_val, enc_key),
                        )
                        changed_names.append(name)
                        action = "rotate" if had_row else "set"
                        prev_source = "saved" if had_row else "unset"
                        fingerprint = (
                            _secret_fingerprint(enc_key, new_val)
                            if spec.secret
                            else new_val[:200]
                        )
                        audit_entries.append((name, action, prev_source, fingerprint))

                # Step 5: bump revision and append audit rows in the same transaction.
                if audit_entries:
                    new_revision = current_revision + 1
                    await cur.execute(
                        """
                        UPDATE dalton_deployment_settings_meta
                        SET revision = %s,
                            updated_at = NOW(),
                            updated_by = %s
                        WHERE singleton = TRUE
                        """,
                        (new_revision, actor_user_id),
                    )
                    for setting_name, action, prev_source, fingerprint in audit_entries:
                        await cur.execute(
                            """
                            INSERT INTO dalton_deployment_settings_audit (
                                revision,
                                actor_user_id,
                                setting_name,
                                action,
                                previous_source,
                                value_fingerprint,
                                client_ip
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                new_revision,
                                actor_user_id,
                                setting_name,
                                action,
                                prev_source,
                                fingerprint,
                                client_ip,
                            ),
                        )
                else:
                    new_revision = current_revision

    # Step 6: post-commit local refresh + guaranteed cross-worker broadcast.
    pending_apply = False
    warnings: list[str] = []
    if changed_names:
        from src.server.services.setup_settings import apply as setup_apply
        from src.server.services.setup_settings import follow as setup_follow

        try:
            await setup_apply.refresh()
        except Exception as exc:
            pending_apply = True
            warnings.append("local_refresh_deferred")
            logger.warning(
                "Local deployment settings refresh deferred after commit (%s)",
                type(exc).__name__,
            )
        finally:
            try:
                await setup_follow.publish_changed()
            except Exception as exc:
                logger.warning(
                    "Cross-worker deployment settings notification failed (%s)",
                    type(exc).__name__,
                )

    return WriteResult(
        revision=new_revision,
        changed_names=tuple(changed_names),
        pending_apply=pending_apply,
        warnings=tuple(warnings),
    )
