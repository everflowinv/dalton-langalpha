"""Bundled plugin split-disable inheritance & re-enable overrides (d045, Standalone Core §3.3 #3).

When ``dalton-sec`` was split out of ``dalton-data``, existing accounts may
already hold ``('bundle', 'dalton-data')`` in ``user_mcp_builtin_disables``.
This module keeps the three plugin surfaces — ``GET /api/v1/plugins``,
``PATCH /api/v1/plugins/{name}/enabled``, and MCP/skill/subagent enforcement
(``account_disabled_builtins`` / ``enforcement_owners``) — strictly consistent:

1. A legacy ``('bundle', 'dalton-data')`` disable inherits to ``dalton-sec``
   unless ``dalton_plugin_disable_overrides`` holds ``(user_id, 'dalton-sec')``.
2. Enabling ``dalton-sec`` deletes any ``('bundle', 'dalton-sec')`` disable and
   upserts ``(user_id, 'dalton-sec')`` into ``dalton_plugin_disable_overrides``
   in the same transaction as ``mcp_config_version`` bump.
3. Disabling ``dalton-sec`` removes ``(user_id, 'dalton-sec')`` from
   ``dalton_plugin_disable_overrides`` and inserts ``('bundle', 'dalton-sec')``
   in the same transaction.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from types import MappingProxyType
from typing import Any

from psycopg.errors import UndefinedTable
from psycopg.rows import dict_row

from src.dalton.capabilities import is_bundled_plugin_enabled_by_capability
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

# Child bundle name -> legacy parent bundle name(s) whose pre-split disable
# applies to the child unless overridden in ``dalton_plugin_disable_overrides``.
LEGACY_BUNDLE_SPLIT_PARENTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "dalton-sec": ("dalton-data",),
    }
)

_ALL_LEGACY_SPLIT_PARENTS: frozenset[str] = frozenset(
    parent for parents in LEGACY_BUNDLE_SPLIT_PARENTS.values() for parent in parents
)


_BUNDLE_TOGGLE_LOCK_NS = "dalton:plugin_bundle_toggle"

_ATOMIC_ACCOUNT_DISABLES_SQL = """
SELECT kind, name
FROM user_mcp_builtin_disables
WHERE user_id = %s
UNION ALL
SELECT 'override'::text AS kind, bundle_name AS name
FROM dalton_plugin_disable_overrides
WHERE user_id = %s
"""

_FALLBACK_ACCOUNT_DISABLES_SQL = """
SELECT kind, name
FROM user_mcp_builtin_disables
WHERE user_id = %s
"""


def expand_effective_disabled_bundles(
    raw_bundles: Collection[str],
    reenabled_bundles: Collection[str] = (),
) -> frozenset[str]:
    """Compute effective disabled bundle names given raw disables and re-enable overrides."""
    effective = set(raw_bundles)
    reenabled = set(reenabled_bundles)
    for child, parents in LEGACY_BUNDLE_SPLIT_PARENTS.items():
        if child in effective:
            continue
        if any(parent in raw_bundles for parent in parents) and child not in reenabled:
            effective.add(child)
    return frozenset(effective)


async def read_account_disables_atomic(
    db: Any,
    user_id: str,
    *,
    in_existing_tx: bool = False,
) -> tuple[frozenset[str], frozenset[str]]:
    """Read disables and d045 re-enable overrides in one transaction and one MVCC snapshot.

    Uses a single ``UNION ALL`` query inside one transaction so concurrent
    toggles on ``user_mcp_builtin_disables`` and ``dalton_plugin_disable_overrides``
    can never be observed across two different commits.

    When the backend starts via an upstream-only entry point (``make up`` /
    ``scripts/docker_entrypoint.sh``) that has not run Dalton migrations yet,
    ``dalton_plugin_disable_overrides`` does not exist; only ``UndefinedTable``
    falls back to reading ``user_mcp_builtin_disables`` with empty overrides so
    all other database errors propagate unchanged.
    """
    async def _run_atomic() -> list[Mapping[str, Any]]:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(_ATOMIC_ACCOUNT_DISABLES_SQL, (user_id, user_id))
            return await cur.fetchall()

    async def _run_fallback() -> list[Mapping[str, Any]]:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(_FALLBACK_ACCOUNT_DISABLES_SQL, (user_id,))
            return await cur.fetchall()

    try:
        # psycopg3 uses a SAVEPOINT when already inside an outer transaction
        # (`in_existing_tx=True`) and starts a transaction otherwise, so an
        # UndefinedTable rolls back cleanly without aborting a caller's tx.
        async with db.transaction():
            rows = await _run_atomic()
    except UndefinedTable:
        logger.warning(
            "[plugin_disable_overrides] table dalton_plugin_disable_overrides is "
            "missing; falling back to user_mcp_builtin_disables with no overrides"
        )
        if in_existing_tx:
            rows = await _run_fallback()
        else:
            async with db.transaction():
                rows = await _run_fallback()

    servers = frozenset(r["name"] for r in rows if r["kind"] == "server")
    raw_bundles = frozenset(r["name"] for r in rows if r["kind"] == "bundle")
    reenabled = frozenset(r["name"] for r in rows if r["kind"] == "override")
    effective_bundles = expand_effective_disabled_bundles(raw_bundles, reenabled)
    return servers, effective_bundles


async def list_reenabled_bundles(
    user_id: str,
    *,
    conn: Any = None,
) -> frozenset[str]:
    """Return bundle names explicitly re-enabled by ``user_id`` in ``dalton_plugin_disable_overrides``.

    Treats a missing table (``UndefinedTable``) as no re-enable overrides; other
    database errors propagate.
    """
    async with get_db_connection(conn) as db:
        try:
            async with db.transaction():
                async with db.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT bundle_name FROM dalton_plugin_disable_overrides WHERE user_id = %s",
                        (user_id,),
                    )
                    rows = await cur.fetchall()
        except UndefinedTable:
            logger.warning(
                "[plugin_disable_overrides] table dalton_plugin_disable_overrides is "
                "missing; treating re-enable overrides as empty"
            )
            return frozenset()
    return frozenset(
        str(r["bundle_name"])
        for r in rows
        if isinstance(r, Mapping) and r.get("bundle_name")
    )


async def resolve_effective_disabled_bundles(
    user_id: str,
    raw_bundles: frozenset[str],
    *,
    conn: Any = None,
) -> frozenset[str]:
    """Expand ``raw_bundles`` with legacy split inheritance minus explicit d045 overrides.

    Fast-paths without touching ``dalton_plugin_disable_overrides`` whenever
    ``raw_bundles`` contains no legacy split parent (or every affected child is
    already explicitly disabled).
    """
    if not (raw_bundles & _ALL_LEGACY_SPLIT_PARENTS):
        return raw_bundles

    needs_override_lookup = any(
        child not in raw_bundles and any(parent in raw_bundles for parent in parents)
        for child, parents in LEGACY_BUNDLE_SPLIT_PARENTS.items()
    )
    if not needs_override_lookup:
        return raw_bundles

    reenabled = await list_reenabled_bundles(user_id, conn=conn)
    return expand_effective_disabled_bundles(raw_bundles, reenabled)


async def acquire_bundle_toggle_lock_in_tx(
    cur: Any,
    user_id: str,
    kind: str,
    name: str,
) -> None:
    """Serialize concurrent enable/disable writes for the same ``(user_id, bundle)``.

    Must be called first inside ``set_account_disable``'s transaction on BOTH
    enable and disable paths so lock acquisition order is identical and no
    interleaving can leave both a disable row and an override row behind.
    """
    if kind != "bundle":
        return
    await cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
        (_BUNDLE_TOGGLE_LOCK_NS, f"{user_id}:{name}"),
    )


async def sync_plugin_disable_override_in_tx(
    cur: Any,
    user_id: str,
    kind: str,
    name: str,
    *,
    disabled: bool,
) -> None:
    """Keep ``dalton_plugin_disable_overrides`` consistent inside ``set_account_disable``'s tx.

    Raises HTTP 503 if the Dalton migration (``d045``) creating
    ``dalton_plugin_disable_overrides`` has not been applied yet, rolling back
    the enclosing transaction cleanly.
    """
    if kind != "bundle" or name not in LEGACY_BUNDLE_SPLIT_PARENTS:
        return

    try:
        if disabled:
            await cur.execute(
                """
                DELETE FROM dalton_plugin_disable_overrides
                WHERE user_id = %s AND bundle_name = %s
                """,
                (user_id, name),
            )
        else:
            await cur.execute(
                """
                INSERT INTO dalton_plugin_disable_overrides (user_id, bundle_name, reenabled_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (user_id, bundle_name)
                DO UPDATE SET reenabled_at = EXCLUDED.reenabled_at
                """,
                (user_id, name),
            )
    except UndefinedTable as exc:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=503,
            detail=(
                "Database table 'dalton_plugin_disable_overrides' is missing. "
                "Run Dalton database migrations "
                "(alembic -c src/dalton/alembic.ini upgrade head) "
                f"before toggling plugin '{name}'."
            ),
        ) from exc
    logger.info(
        "[plugin_disable_overrides] synced user_id=%s bundle=%s disabled=%s",
        user_id,
        name,
        disabled,
    )


def ensure_bundled_plugin_capability(bundle_name: str) -> None:
    """Raise HTTP 404 when toggling a bundled plugin whose capability is disabled."""
    from fastapi import HTTPException

    if not is_bundled_plugin_enabled_by_capability(bundle_name):
        raise HTTPException(status_code=404, detail="Plugin not found")
