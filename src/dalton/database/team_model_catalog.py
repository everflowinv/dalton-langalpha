"""Raw psycopg3 SQL layer for ``dalton_team_model_catalog`` and related tables.

All SQL for the team model catalog lives here (never in routers or services):
- Singleton current catalog row (``dalton_team_model_catalog``)
- Authoritative revision audit & rollback history (``dalton_team_model_catalog_revisions``)
- Write-only encrypted catalog variant credentials (``dalton_catalog_provider_credentials``)
- Outermost catalog advisory locks (shared for model-reference writers, exclusive for
  catalog mutations / retirements / rollbacks / startup import)
- Legacy native-sync history detection & conditional run snapshot backfill
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.dalton.llm.egress_policy import canonical_url_key
from src.dalton.services.team_model_catalog.constants import (
    CATALOG_LOCK_KEY,
    CatalogConflictError,
    compute_payload_sha256,
    empty_catalog_payload,
    make_key_fingerprint,
)
from src.server.database.encryption import get_encryption_key as _get_encryption_key
from src.server.database.pool import get_db_connection, get_db_connection_string
from src.server.utils.pg_sanitize import SafeJson

logger = logging.getLogger(__name__)

_UNSET = object()


# ---------------------------------------------------------------------------
# Outermost catalog advisory locks
# ---------------------------------------------------------------------------


async def acquire_catalog_shared_lock(conn: Any) -> None:
    """Acquire transaction-scoped shared advisory lock on ``dalton_team_model_catalog``.

    MUST be acquired at the outermost layer of every model-reference writer
    (before coverage-unit ``FOR SHARE`` locks and before ``lock_user_automations`` /
    ``lock_user_subagents`` / ``overrides_locked``) to guarantee deadlock-free
    ordering against exclusive catalog retire/rollback transactions.
    """
    await conn.execute(
        "SELECT pg_advisory_xact_lock_shared(hashtext(%s))",
        (CATALOG_LOCK_KEY,),
    )


async def acquire_catalog_exclusive_lock(conn: Any) -> None:
    """Acquire transaction-scoped exclusive advisory lock on ``dalton_team_model_catalog``."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (CATALOG_LOCK_KEY,),
    )


# ---------------------------------------------------------------------------
# Catalog head & singleton row reads (async + sync)
# ---------------------------------------------------------------------------


def _normalize_catalog_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    out = dict(row)
    out["revision"] = int(out.get("revision") or 0)
    out["schema_version"] = int(out.get("schema_version") or 1)
    payload = out.get("payload")
    out["payload"] = dict(payload) if isinstance(payload, dict) else empty_catalog_payload()
    out["payload_sha256"] = str(out.get("payload_sha256") or compute_payload_sha256(out["payload"]))
    out["legacy_sync_import_status"] = str(out.get("legacy_sync_import_status") or "pending")
    out["last_known_legacy_retired"] = [
        str(k) for k in (out.get("last_known_legacy_retired") or []) if str(k).strip()
    ]
    for ts_key in ("legacy_sync_imported_at", "updated_at"):
        val = out.get(ts_key)
        if hasattr(val, "isoformat"):
            out[ts_key] = val.isoformat()
    return out


async def get_catalog_head(*, conn: Any = None) -> dict[str, Any] | None:
    """Lightweight primary-key point read of the current catalog head revision."""
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT revision, payload_sha256, legacy_sync_import_status,
                       last_known_legacy_retired
                FROM dalton_team_model_catalog
                WHERE singleton_id = 1
                """
            )
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "revision": int(row["revision"]),
                "payload_sha256": str(row["payload_sha256"]),
                "legacy_sync_import_status": str(row["legacy_sync_import_status"]),
                "last_known_legacy_retired": [
                    str(k) for k in (row.get("last_known_legacy_retired") or []) if str(k).strip()
                ],
            }


def is_conversation_db_pool_open() -> bool:
    """Return True iff the conversation DB pool is already initialized and open.

    Used to guard sync PG probes (avoiding unexpected ``localhost`` DNS/socket
    connect attempts in unit tests or offline callers) and conditional locking.
    """
    from src.server.database.pool import _conversation_db_pool_cache

    pool = _conversation_db_pool_cache.get(get_db_connection_string())
    return pool is not None and not pool.closed


def get_catalog_head_sync() -> dict[str, Any] | None:
    """Synchronous lightweight head check for non-async callers (e.g. ``LLM.get_model_config``)."""
    if not is_conversation_db_pool_open():
        return None
    import psycopg

    with psycopg.connect(get_db_connection_string(), connect_timeout=2) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT revision, payload_sha256, legacy_sync_import_status,
                       last_known_legacy_retired
                FROM dalton_team_model_catalog
                WHERE singleton_id = 1
                """
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "revision": int(row["revision"]),
                "payload_sha256": str(row["payload_sha256"]),
                "legacy_sync_import_status": str(row["legacy_sync_import_status"]),
                "last_known_legacy_retired": [
                    str(k) for k in (row.get("last_known_legacy_retired") or []) if str(k).strip()
                ],
            }


async def get_catalog_row(
    *, conn: Any = None, for_update: bool = False
) -> dict[str, Any] | None:
    """Read the full singleton row from ``dalton_team_model_catalog``."""
    lock_clause = " FOR UPDATE" if for_update else ""
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT singleton_id, revision, schema_version, payload, payload_sha256,
                       legacy_sync_import_status, legacy_sync_imported_at,
                       legacy_sync_import_error, last_known_legacy_retired,
                       updated_by, updated_at
                FROM dalton_team_model_catalog
                WHERE singleton_id = 1{lock_clause}
                """
            )
            return _normalize_catalog_row(await cur.fetchone())


def get_catalog_row_sync() -> dict[str, Any] | None:
    """Synchronous full singleton row read for sync cache miss compilation."""
    if not is_conversation_db_pool_open():
        return None
    import psycopg

    with psycopg.connect(get_db_connection_string(), connect_timeout=2) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT singleton_id, revision, schema_version, payload, payload_sha256,
                       legacy_sync_import_status, legacy_sync_imported_at,
                       legacy_sync_import_error, last_known_legacy_retired,
                       updated_by, updated_at
                FROM dalton_team_model_catalog
                WHERE singleton_id = 1
                """
            )
            return _normalize_catalog_row(cur.fetchone())


# ---------------------------------------------------------------------------
# Catalog CAS write & revision history
# ---------------------------------------------------------------------------


async def save_catalog_revision(
    conn: Any,
    *,
    expected_revision: int | None,
    action: str,
    target_keys: list[str],
    payload: dict[str, Any],
    diff_summary: dict[str, Any],
    side_effects: list[dict[str, Any]],
    actor_user_id: str,
    actor_label: str | None = None,
    comment: str | None = None,
    restored_from: int | None = None,
    legacy_sync_import_status: str | None = None,
    legacy_sync_imported_at_now: bool = False,
    legacy_sync_import_error: Any = _UNSET,
    last_known_legacy_retired: list[str] | None = None,
) -> dict[str, Any]:
    """Commit ``revision = current_revision + 1`` with CAS check inside an open transaction.

    Caller MUST already hold ``acquire_catalog_exclusive_lock(conn)`` in the same
    transaction.
    """
    current = await get_catalog_row(conn=conn, for_update=True)
    if current is None:
        raise RuntimeError("dalton_team_model_catalog singleton row is missing (run d051 migration)")
    current_rev = int(current["revision"])
    if expected_revision is not None and current_rev != int(expected_revision):
        raise CatalogConflictError(
            "revision_conflict",
            f"Catalog revision conflict: expected {expected_revision}, current is {current_rev}.",
            extra={
                "expected_revision": int(expected_revision),
                "current_revision": current_rev,
                "current_payload_sha256": current["payload_sha256"],
            },
        )

    new_rev = current_rev + 1
    payload_sha = compute_payload_sha256(payload)
    next_legacy_status = (
        legacy_sync_import_status
        if legacy_sync_import_status is not None
        else current["legacy_sync_import_status"]
    )
    next_legacy_error = (
        current.get("legacy_sync_import_error")
        if legacy_sync_import_error is _UNSET
        else legacy_sync_import_error
    )
    next_known_retired = (
        sorted({str(k).strip() for k in last_known_legacy_retired if str(k).strip()})
        if last_known_legacy_retired is not None
        else list(current.get("last_known_legacy_retired") or [])
    )

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            UPDATE dalton_team_model_catalog
            SET revision = %s,
                payload = %s,
                payload_sha256 = %s,
                legacy_sync_import_status = %s,
                legacy_sync_imported_at = CASE WHEN %s THEN NOW() ELSE legacy_sync_imported_at END,
                legacy_sync_import_error = %s,
                last_known_legacy_retired = %s,
                updated_by = %s,
                updated_at = NOW()
            WHERE singleton_id = 1
            RETURNING singleton_id, revision, schema_version, payload, payload_sha256,
                      legacy_sync_import_status, legacy_sync_imported_at,
                      legacy_sync_import_error, last_known_legacy_retired,
                      updated_by, updated_at
            """,
            (
                new_rev,
                Json(payload),
                payload_sha,
                next_legacy_status,
                bool(legacy_sync_imported_at_now),
                next_legacy_error,
                next_known_retired,
                actor_user_id,
            ),
        )
        updated_row = _normalize_catalog_row(await cur.fetchone())

        await cur.execute(
            """
            INSERT INTO dalton_team_model_catalog_revisions (
                revision, action, target_keys, payload, payload_sha256,
                diff_summary, side_effects, comment, restored_from,
                actor_user_id, actor_label
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING revision, action, target_keys, payload_sha256, diff_summary,
                      side_effects, comment, restored_from, actor_user_id, actor_label,
                      created_at
            """,
            (
                new_rev,
                action,
                list(target_keys),
                Json(payload),
                payload_sha,
                Json(diff_summary or {}),
                Json(side_effects or []),
                comment,
                restored_from,
                actor_user_id,
                actor_label,
            ),
        )
        rev_row = dict(await cur.fetchone())
        if hasattr(rev_row.get("created_at"), "isoformat"):
            rev_row["created_at"] = rev_row["created_at"].isoformat()

    assert updated_row is not None
    return {"catalog": updated_row, "revision_record": rev_row}


async def update_legacy_import_state_only(
    conn: Any,
    *,
    status: str,
    error: str | None = None,
    last_known_legacy_retired: list[str] | None = None,
    updated_by: str = "system:startup",
) -> dict[str, Any]:
    """Update legacy import metadata columns without bumping revision (used for ``not_applicable`` / ``failed``)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        if last_known_legacy_retired is not None:
            cleaned = sorted({str(k).strip() for k in last_known_legacy_retired if str(k).strip()})
            await cur.execute(
                """
                UPDATE dalton_team_model_catalog
                SET legacy_sync_import_status = %s,
                    legacy_sync_import_error = %s,
                    last_known_legacy_retired = %s,
                    updated_by = %s,
                    updated_at = NOW()
                WHERE singleton_id = 1
                RETURNING singleton_id, revision, schema_version, payload, payload_sha256,
                          legacy_sync_import_status, legacy_sync_imported_at,
                          legacy_sync_import_error, last_known_legacy_retired,
                          updated_by, updated_at
                """,
                (status, error, cleaned, updated_by),
            )
        else:
            await cur.execute(
                """
                UPDATE dalton_team_model_catalog
                SET legacy_sync_import_status = %s,
                    legacy_sync_import_error = %s,
                    updated_by = %s,
                    updated_at = NOW()
                WHERE singleton_id = 1
                RETURNING singleton_id, revision, schema_version, payload, payload_sha256,
                          legacy_sync_import_status, legacy_sync_imported_at,
                          legacy_sync_import_error, last_known_legacy_retired,
                          updated_by, updated_at
                """,
                (status, error, updated_by),
            )
        row = _normalize_catalog_row(await cur.fetchone())
        assert row is not None
        return row


def _normalize_revision_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    out = dict(row)
    out["revision"] = int(out["revision"])
    if out.get("restored_from") is not None:
        out["restored_from"] = int(out["restored_from"])
    out["target_keys"] = list(out.get("target_keys") or [])
    out["diff_summary"] = dict(out.get("diff_summary") or {})
    out["side_effects"] = list(out.get("side_effects") or [])
    if hasattr(out.get("created_at"), "isoformat"):
        out["created_at"] = out["created_at"].isoformat()
    return out


async def list_catalog_revisions(
    *, limit: int = 50, offset: int = 0, conn: Any = None
) -> list[dict[str, Any]]:
    """Return revision audit rows ordered newest first (omitting full payload for list efficiency)."""
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT revision, action, target_keys, payload_sha256, diff_summary,
                       side_effects, comment, restored_from, actor_user_id, actor_label,
                       created_at
                FROM dalton_team_model_catalog_revisions
                ORDER BY revision DESC
                LIMIT %s OFFSET %s
                """,
                (max(1, min(int(limit), 200)), max(0, int(offset))),
            )
            return [_normalize_revision_row(r) for r in await cur.fetchall() if r]  # type: ignore[misc]


async def get_catalog_revision(
    revision: int, *, conn: Any = None
) -> dict[str, Any] | None:
    """Return a single revision row including its full ``payload``."""
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT revision, action, target_keys, payload, payload_sha256,
                       diff_summary, side_effects, comment, restored_from,
                       actor_user_id, actor_label, created_at
                FROM dalton_team_model_catalog_revisions
                WHERE revision = %s
                """,
                (int(revision),),
            )
            return _normalize_revision_row(await cur.fetchone())


async def list_revisions_between(
    after_revision_exclusive: int,
    up_to_revision_inclusive: int,
    *,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Return revisions in ``(after_revision_exclusive, up_to_revision_inclusive]`` ordered oldest→newest."""
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT revision, action, target_keys, diff_summary, side_effects,
                       restored_from, actor_user_id, created_at
                FROM dalton_team_model_catalog_revisions
                WHERE revision > %s AND revision <= %s
                ORDER BY revision ASC
                """,
                (int(after_revision_exclusive), int(up_to_revision_inclusive)),
            )
            return [_normalize_revision_row(r) for r in await cur.fetchall() if r]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Catalog-defined provider variant write-only encrypted credentials
# ---------------------------------------------------------------------------


def _normalize_cred_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    out = dict(row)
    if hasattr(out.get("updated_at"), "isoformat"):
        out["updated_at"] = out["updated_at"].isoformat()
    return out


async def upsert_catalog_provider_credential(
    conn: Any,
    *,
    provider_slug: str,
    api_key: str,
    bound_base_url: str,
    updated_by: str,
) -> dict[str, Any]:
    """Encrypt and bind an API key to a catalog variant's full normalized ``bound_base_url``."""
    slug = str(provider_slug or "").strip()
    if not slug:
        raise ValueError("provider_slug is required")
    cleaned_key = str(api_key or "").strip()
    if not cleaned_key:
        raise ValueError("api_key is required for catalog provider variant")
    canonical_bound = canonical_url_key(bound_base_url)
    if not canonical_bound:
        raise ValueError(f"Invalid bound_base_url for catalog variant {slug!r}")
    fingerprint = make_key_fingerprint(cleaned_key)
    enc_key = _get_encryption_key()
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO dalton_catalog_provider_credentials (
                provider_slug, encrypted_api_key, bound_base_url,
                canonical_bound_url, key_fingerprint, updated_by, updated_at
            )
            VALUES (%s, pgp_sym_encrypt(%s, %s), %s, %s, %s, %s, NOW())
            ON CONFLICT (provider_slug) DO UPDATE SET
                encrypted_api_key = EXCLUDED.encrypted_api_key,
                bound_base_url = EXCLUDED.bound_base_url,
                canonical_bound_url = EXCLUDED.canonical_bound_url,
                key_fingerprint = EXCLUDED.key_fingerprint,
                updated_by = EXCLUDED.updated_by,
                updated_at = NOW()
            RETURNING provider_slug, bound_base_url, canonical_bound_url,
                      key_fingerprint, updated_by, updated_at
            """,
            (
                slug,
                cleaned_key,
                enc_key,
                bound_base_url.strip(),
                canonical_bound,
                fingerprint,
                updated_by,
            ),
        )
        row = _normalize_cred_row(await cur.fetchone())
        assert row is not None
        return row


async def get_catalog_provider_credential(
    provider_slug: str,
    *,
    conn: Any = None,
    decrypt: bool = True,
) -> dict[str, Any] | None:
    """Read a catalog variant credential row (optionally decrypting ``api_key``)."""
    slug = str(provider_slug or "").strip()
    if not slug:
        return None
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            if decrypt:
                enc_key = _get_encryption_key()
                await cur.execute(
                    """
                    SELECT provider_slug,
                           pgp_sym_decrypt(encrypted_api_key, %s) AS api_key,
                           bound_base_url, canonical_bound_url,
                           key_fingerprint, updated_by, updated_at
                    FROM dalton_catalog_provider_credentials
                    WHERE provider_slug = %s
                    """,
                    (enc_key, slug),
                )
            else:
                await cur.execute(
                    """
                    SELECT provider_slug, bound_base_url, canonical_bound_url,
                           key_fingerprint, updated_by, updated_at
                    FROM dalton_catalog_provider_credentials
                    WHERE provider_slug = %s
                    """,
                    (slug,),
                )
            return _normalize_cred_row(await cur.fetchone())


def get_catalog_provider_credential_sync(
    provider_slug: str,
    *,
    decrypt: bool = True,
) -> dict[str, Any] | None:
    """Synchronous variant credential lookup for sync send-time verification and ``create_llm``."""
    import psycopg

    slug = str(provider_slug or "").strip()
    if not slug:
        return None
    with psycopg.connect(get_db_connection_string(), connect_timeout=3) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if decrypt:
                enc_key = _get_encryption_key()
                cur.execute(
                    """
                    SELECT provider_slug,
                           pgp_sym_decrypt(encrypted_api_key, %s) AS api_key,
                           bound_base_url, canonical_bound_url,
                           key_fingerprint, updated_by, updated_at
                    FROM dalton_catalog_provider_credentials
                    WHERE provider_slug = %s
                    """,
                    (enc_key, slug),
                )
            else:
                cur.execute(
                    """
                    SELECT provider_slug, bound_base_url, canonical_bound_url,
                           key_fingerprint, updated_by, updated_at
                    FROM dalton_catalog_provider_credentials
                    WHERE provider_slug = %s
                    """,
                    (slug,),
                )
            return _normalize_cred_row(cur.fetchone())


async def list_catalog_provider_credentials(
    *, conn: Any = None
) -> dict[str, dict[str, Any]]:
    """Return metadata (NEVER plaintext keys) for all catalog variant credentials."""
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT provider_slug, bound_base_url, canonical_bound_url,
                       key_fingerprint, updated_by, updated_at
                FROM dalton_catalog_provider_credentials
                ORDER BY provider_slug
                """
            )
            return {
                str(row["provider_slug"]): _normalize_cred_row(row)  # type: ignore[misc]
                for row in await cur.fetchall()
                if row and row.get("provider_slug")
            }


def list_catalog_provider_credentials_sync() -> dict[str, dict[str, Any]]:
    """Synchronous metadata listing for all catalog variant credentials."""
    import psycopg

    with psycopg.connect(get_db_connection_string(), connect_timeout=2) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT provider_slug, bound_base_url, canonical_bound_url,
                       key_fingerprint, updated_by, updated_at
                FROM dalton_catalog_provider_credentials
                ORDER BY provider_slug
                """
            )
            return {
                str(row["provider_slug"]): _normalize_cred_row(row)  # type: ignore[misc]
                for row in cur.fetchall()
                if row and row.get("provider_slug")
            }


async def delete_catalog_provider_credential(conn: Any, provider_slug: str) -> bool:
    """Delete a catalog variant's bound credential row."""
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM dalton_catalog_provider_credentials WHERE provider_slug = %s",
            (str(provider_slug).strip(),),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Legacy native_sync_overlay history detection (dalton_openclaw_sync_runs)
# ---------------------------------------------------------------------------


async def has_legacy_native_sync_history(*, conn: Any = None) -> bool:
    """Return True if ``dalton_openclaw_sync_runs`` holds any succeeded non-dry-run native write."""
    async with get_db_connection(conn) as db:
        async with db.cursor() as cur:
            await cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM dalton_openclaw_sync_runs
                    WHERE source = 'native'
                      AND dry_run = FALSE
                      AND status = 'succeeded'
                )
                """
            )
            row = await cur.fetchone()
            return bool(row and row[0])


async def fetch_legacy_retired_history(*, conn: Any = None) -> set[str]:
    """Reconstruct known retired model keys from succeeded native retire/restore runs."""
    retired: set[str] = set()
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT diff
                FROM dalton_openclaw_sync_runs
                WHERE source = 'native'
                  AND dry_run = FALSE
                  AND status = 'succeeded'
                  AND COALESCE(diff->>'kind', '') IN ('retire', 'restore')
                ORDER BY started_at ASC, finished_at ASC NULLS LAST
                """
            )
            for row in await cur.fetchall():
                diff = row.get("diff")
                if not isinstance(diff, dict):
                    continue
                kind = str(diff.get("kind") or "")
                results = diff.get("results") or []
                if not isinstance(results, list):
                    continue
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    key = str(item.get("key") or "").strip()
                    status = str(item.get("status") or "").strip()
                    if not key:
                        continue
                    if kind == "retire" and status == "retired":
                        retired.add(key)
                    elif kind == "restore" and status == "restored":
                        retired.discard(key)
    return retired


# ---------------------------------------------------------------------------
# Conditional run snapshot backfill for pre-feature legacy runs
# ---------------------------------------------------------------------------


async def backfill_run_catalog_snapshot(
    run_id: str,
    snapshot: dict[str, Any],
    *,
    is_subagent: bool = False,
    conn: Any = None,
) -> bool:
    """Conditionally persist ``effective_catalog_snapshot`` only when currently absent."""
    if not run_id or not isinstance(snapshot, dict):
        return False
    # Share the exact same SafeJson (_safe_dumps) serializer as first-write paths
    # (lifecycle.py / subagent_runs.py) so keys and JSONB encoding never diverge.
    payload_bind = SafeJson(snapshot)
    async with get_db_connection(conn) as db:
        async with db.cursor() as cur:
            if is_subagent:
                await cur.execute(
                    """
                    UPDATE subagent_runs
                    SET replay_facts = COALESCE(replay_facts, '{}'::jsonb)
                        || jsonb_build_object(
                            'catalog_revision', %s::bigint,
                            'effective_catalog_snapshot', %s::jsonb
                        )
                    WHERE task_run_id = %s::uuid
                      AND (
                          replay_facts IS NULL
                          OR NOT (replay_facts ? 'effective_catalog_snapshot')
                      )
                    """,
                    (
                        int(snapshot.get("catalog_revision") or 0),
                        payload_bind,
                        str(run_id),
                    ),
                )
            else:
                await cur.execute(
                    """
                    UPDATE conversation_responses
                    SET metadata = COALESCE(metadata, '{}'::jsonb)
                        || jsonb_build_object(
                            'catalog_revision', %s::bigint,
                            'effective_catalog_snapshot', %s::jsonb
                        )
                    WHERE conversation_response_id = %s::uuid
                      AND (
                          metadata IS NULL
                          OR NOT (metadata ? 'effective_catalog_snapshot')
                      )
                    """,
                    (
                        int(snapshot.get("catalog_revision") or 0),
                        payload_bind,
                        str(run_id),
                    ),
                )
            return cur.rowcount > 0
