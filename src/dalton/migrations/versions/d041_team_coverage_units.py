"""Dalton d041: team coverage units — global uniqueness, lead analyst, and normalized ticker_key.

Revision ID: d041
Revises: d040
Create Date: 2026-11-19
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

from src.dalton.coverage.identity import normalize_ticker_key

revision: str = "d041"
down_revision: Union[str, None] = "d040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UPGRADE_SLUG_CONFLICT_SQL = """
SELECT unit_id::text AS unit_id,
       owner_user_id,
       kind,
       slug,
       ticker,
       ticker_key,
       workspace_id::text AS workspace_id
FROM dalton_coverage_units
WHERE status = 'active'
  AND (kind, slug) IN (
      SELECT kind, slug
      FROM dalton_coverage_units
      WHERE status = 'active'
      GROUP BY kind, slug
      HAVING COUNT(*) > 1
  )
ORDER BY kind, slug, created_at, unit_id
"""

UPGRADE_TICKER_CONFLICT_SQL = """
SELECT unit_id::text AS unit_id,
       owner_user_id,
       kind,
       slug,
       ticker,
       ticker_key,
       workspace_id::text AS workspace_id
FROM dalton_coverage_units
WHERE status = 'active'
  AND kind = 'company'
  AND ticker_key IS NOT NULL
  AND ticker_key IN (
      SELECT ticker_key
      FROM dalton_coverage_units
      WHERE status = 'active'
        AND kind = 'company'
        AND ticker_key IS NOT NULL
      GROUP BY ticker_key
      HAVING COUNT(*) > 1
  )
ORDER BY ticker_key, created_at, unit_id
"""

DOWNGRADE_OWNER_SLUG_CONFLICT_SQL = """
SELECT unit_id::text AS unit_id,
       owner_user_id,
       kind,
       slug,
       status,
       workspace_id::text AS workspace_id
FROM dalton_coverage_units
WHERE (owner_user_id, kind, slug) IN (
    SELECT owner_user_id, kind, slug
    FROM dalton_coverage_units
    GROUP BY owner_user_id, kind, slug
    HAVING COUNT(*) > 1
)
ORDER BY owner_user_id, kind, slug, created_at, unit_id
"""

DOWNGRADE_CROSS_OWNER_BINDING_SQL = """
SELECT u.unit_id::text AS unit_id,
       u.owner_user_id,
       u.lead_analyst_user_id,
       u.kind,
       u.slug,
       u.workspace_id::text AS workspace_id,
       w.user_id AS workspace_owner_id
FROM dalton_coverage_units u
JOIN workspaces w ON w.workspace_id = u.workspace_id
WHERE u.status = 'active'
  AND u.workspace_id IS NOT NULL
  AND w.status <> 'deleted'
  AND (u.owner_user_id <> w.user_id OR u.lead_analyst_user_id <> w.user_id)
ORDER BY u.created_at, u.unit_id
"""

DOWNGRADE_COLLEAGUE_MANAGED_AUTOMATIONS_SQL = """
SELECT a.automation_id::text AS automation_id,
       a.user_id,
       a.workspace_id::text AS workspace_id,
       a.name,
       a.scope,
       w.user_id AS workspace_owner_id
FROM automations a
JOIN workspaces w ON w.workspace_id = a.workspace_id
WHERE a.status = 'active'
  AND a.workspace_id IS NOT NULL
  AND w.status <> 'deleted'
  AND a.user_id <> w.user_id
  AND a.scope IN ('research_tracking', 'company')
ORDER BY a.automation_id
"""


def _row_field(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return mapping.get(name)
    return getattr(row, name, None)


def _format_conflict_row(row: Any) -> str:
    return (
        f"(unit_id={_row_field(row, 'unit_id')}, "
        f"owner_user_id={_row_field(row, 'owner_user_id')}, "
        f"kind={_row_field(row, 'kind')}, "
        f"slug={_row_field(row, 'slug')}, "
        f"ticker={_row_field(row, 'ticker')}, "
        f"ticker_key={_row_field(row, 'ticker_key')}, "
        f"workspace_id={_row_field(row, 'workspace_id')})"
    )


def compute_backfill_rows(
    rows: Iterable[Any],
    *,
    workspaces_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Compute d041 ``created_by_user_id``, ``lead_analyst_user_id``, and ``ticker_key`` backfill values (pure)."""
    ws_map = workspaces_by_id or {}
    updates: list[dict[str, Any]] = []
    for row in rows:
        unit_id = _row_field(row, "unit_id")
        kind = str(_row_field(row, "kind") or "").strip().lower()
        raw_ticker = _row_field(row, "ticker")
        cleaned_ticker = str(raw_ticker).strip() if raw_ticker is not None else ""
        ws_id = _row_field(row, "workspace_id")
        ws_key = str(ws_id).strip() if ws_id else ""
        ws = ws_map.get(ws_key) if ws_key else None
        existing_lead = _row_field(row, "lead_analyst_user_id")
        if existing_lead:
            lead = str(existing_lead)
        elif ws is not None and str(ws.get("status") or "") != "deleted" and ws.get("user_id"):
            lead = str(ws["user_id"])
        else:
            lead = str(_row_field(row, "owner_user_id") or "")
        created_by = str(
            _row_field(row, "created_by_user_id") or _row_field(row, "owner_user_id") or ""
        )

        if kind == "company" and cleaned_ticker:
            ticker_key = normalize_ticker_key(cleaned_ticker)
        else:
            ticker_key = None

        updates.append(
            {
                "unit_id": unit_id,
                "created_by_user_id": created_by,
                "lead_analyst_user_id": lead,
                "ticker": cleaned_ticker if ticker_key else None,
                "ticker_key": ticker_key,
            }
        )
    return updates


def detect_upgrade_conflicts(
    rows: Iterable[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Identify active ``(kind, slug)`` and ``ticker_key`` conflicts across rows."""
    by_slug: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_ticker_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in rows:
        status = str(_row_field(raw, "status") or "active").strip().lower()
        if status != "active":
            continue
        item = {
            "unit_id": _row_field(raw, "unit_id"),
            "owner_user_id": _row_field(raw, "owner_user_id"),
            "kind": _row_field(raw, "kind"),
            "slug": _row_field(raw, "slug"),
            "ticker": _row_field(raw, "ticker"),
            "ticker_key": _row_field(raw, "ticker_key"),
            "workspace_id": _row_field(raw, "workspace_id"),
        }
        by_slug[(str(item["kind"] or ""), str(item["slug"] or ""))].append(item)
        if str(item["kind"] or "") == "company" and item["ticker_key"]:
            by_ticker_key[str(item["ticker_key"])].append(item)

    slug_conflicts = [
        item for group in by_slug.values() if len(group) > 1 for item in group
    ]
    ticker_conflicts = [
        item for group in by_ticker_key.values() if len(group) > 1 for item in group
    ]
    return slug_conflicts, ticker_conflicts


def format_upgrade_conflict_error(
    slug_conflicts: Sequence[Any],
    ticker_conflicts: Sequence[Any],
) -> str | None:
    """Build an error message listing every conflicting active unit, or None when clean."""
    if not slug_conflicts and not ticker_conflicts:
        return None
    parts: list[str] = []
    if slug_conflicts:
        lines = ", ".join(_format_conflict_row(r) for r in slug_conflicts)
        parts.append(f"active (kind, slug) conflicts: [{lines}]")
    if ticker_conflicts:
        lines = ", ".join(_format_conflict_row(r) for r in ticker_conflicts)
        parts.append(f"active ticker_key conflicts: [{lines}]")
    return "Cannot migrate dalton_coverage_units to d041 due to " + "; ".join(parts)


def detect_downgrade_conflicts(
    units: Iterable[Any],
    *,
    workspaces_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    automations: Iterable[Any] = (),
) -> dict[str, list[dict[str, Any]]]:
    """Identify conflicts blocking d041 downgrade (pure)."""
    ws_map = workspaces_by_id or {}
    by_owner_slug: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    cross_owner_bindings: list[dict[str, Any]] = []

    for raw in units:
        owner = str(_row_field(raw, "owner_user_id") or "")
        lead = str(_row_field(raw, "lead_analyst_user_id") or owner)
        kind = str(_row_field(raw, "kind") or "")
        slug = str(_row_field(raw, "slug") or "")
        status = str(_row_field(raw, "status") or "")
        ws_id = _row_field(raw, "workspace_id")
        item = {
            "unit_id": _row_field(raw, "unit_id"),
            "owner_user_id": owner,
            "lead_analyst_user_id": lead,
            "kind": kind,
            "slug": slug,
            "status": status,
            "workspace_id": ws_id,
        }
        by_owner_slug[(owner, kind, slug)].append(item)

        if status == "active" and ws_id:
            ws = ws_map.get(str(ws_id))
            if ws is not None and str(ws.get("status") or "") != "deleted":
                ws_owner = str(ws.get("user_id") or "")
                if owner != ws_owner or lead != ws_owner:
                    cross_owner_bindings.append(
                        {**item, "workspace_owner_id": ws_owner}
                    )

    owner_slug_duplicates = [
        item for group in by_owner_slug.values() if len(group) > 1 for item in group
    ]

    colleague_automations: list[dict[str, Any]] = []
    for raw in automations:
        status = str(_row_field(raw, "status") or "")
        ws_id = _row_field(raw, "workspace_id")
        if status != "active" or not ws_id:
            continue
        ws = ws_map.get(str(ws_id))
        if ws is None or str(ws.get("status") or "") == "deleted":
            continue
        auto_uid = str(_row_field(raw, "user_id") or "")
        ws_owner = str(ws.get("user_id") or "")
        if auto_uid == ws_owner:
            continue
        scope = str(_row_field(raw, "scope") or "").strip().lower()
        if scope in ("research_tracking", "company"):
            colleague_automations.append(
                {
                    "automation_id": _row_field(raw, "automation_id"),
                    "user_id": auto_uid,
                    "workspace_id": ws_id,
                    "name": _row_field(raw, "name"),
                    "scope": scope,
                    "workspace_owner_id": ws_owner,
                }
            )

    return {
        "owner_slug_duplicates": owner_slug_duplicates,
        "cross_owner_bindings": cross_owner_bindings,
        "colleague_automations": colleague_automations,
    }


def format_downgrade_conflict_error(
    conflicts: Mapping[str, Sequence[Any]],
) -> str | None:
    """Build an error message for d041 downgrade blockers, or None when safe."""
    parts: list[str] = []
    dups = conflicts.get("owner_slug_duplicates") or ()
    if dups:
        lines = ", ".join(
            f"(unit_id={_row_field(r, 'unit_id')}, owner_user_id={_row_field(r, 'owner_user_id')}, "
            f"kind={_row_field(r, 'kind')}, slug={_row_field(r, 'slug')}, status={_row_field(r, 'status')})"
            for r in dups
        )
        parts.append(f"owner (owner_user_id, kind, slug) duplicates: [{lines}]")
    bindings = conflicts.get("cross_owner_bindings") or ()
    if bindings:
        lines = ", ".join(
            f"(unit_id={_row_field(r, 'unit_id')}, owner_user_id={_row_field(r, 'owner_user_id')}, "
            f"lead_analyst_user_id={_row_field(r, 'lead_analyst_user_id')}, "
            f"workspace_id={_row_field(r, 'workspace_id')}, workspace_owner_id={_row_field(r, 'workspace_owner_id')})"
            for r in bindings
        )
        parts.append(f"cross-owner active bindings: [{lines}]")
    autos = conflicts.get("colleague_automations") or ()
    if autos:
        lines = ", ".join(
            f"(automation_id={_row_field(r, 'automation_id')}, user_id={_row_field(r, 'user_id')}, "
            f"workspace_id={_row_field(r, 'workspace_id')}, name={_row_field(r, 'name')}, "
            f"workspace_owner_id={_row_field(r, 'workspace_owner_id')})"
            for r in autos
        )
        parts.append(f"colleague active managed automations: [{lines}]")
    if not parts:
        return None
    return "Refusing destructive downgrade of Dalton d041 due to " + "; ".join(parts)


def backfill_d041(conn: Any) -> None:
    """Backfill ``created_by_user_id``, ``lead_analyst_user_id``, and ``ticker_key`` inside ``d041.upgrade``."""
    conn.execute(
        sa.text(
            """
            UPDATE dalton_coverage_units u
            SET created_by_user_id = COALESCE(u.created_by_user_id, u.owner_user_id),
                lead_analyst_user_id = COALESCE(
                    u.lead_analyst_user_id,
                    (
                        SELECT w.user_id
                        FROM workspaces w
                        WHERE w.workspace_id = u.workspace_id
                          AND w.status <> 'deleted'
                    ),
                    u.owner_user_id
                )
            WHERE u.created_by_user_id IS NULL OR u.lead_analyst_user_id IS NULL
            """
        )
    )
    conn.execute(
        sa.text(
            """
            UPDATE dalton_coverage_units
            SET ticker = NULL,
                ticker_key = NULL
            WHERE ticker IS NOT NULL AND ticker ~ '^\\s*$'
            """
        )
    )
    conn.execute(
        sa.text(
            """
            UPDATE dalton_coverage_units
            SET ticker_key = NULL
            WHERE ticker IS NULL AND ticker_key IS NOT NULL
            """
        )
    )
    conn.execute(
        sa.text(
            """
            UPDATE dalton_coverage_units
            SET ticker = NULL,
                ticker_key = NULL
            WHERE kind <> 'company' AND (ticker IS NOT NULL OR ticker_key IS NOT NULL)
            """
        )
    )
    rows = conn.execute(
        sa.text(
            """
            SELECT unit_id, ticker
            FROM dalton_coverage_units
            WHERE kind = 'company' AND ticker IS NOT NULL AND ticker !~ '^\\s*$'
            """
        )
    ).fetchall()
    for row in rows:
        unit_id = _row_field(row, "unit_id")
        ticker = _row_field(row, "ticker")
        cleaned_ticker = str(ticker).strip() if ticker is not None else ""
        norm_key = normalize_ticker_key(cleaned_ticker)
        if not norm_key:
            conn.execute(
                sa.text(
                    """
                    UPDATE dalton_coverage_units
                    SET ticker = NULL,
                        ticker_key = NULL
                    WHERE unit_id = :unit_id
                    """
                ),
                {"unit_id": unit_id},
            )
            continue
        conn.execute(
            sa.text(
                """
                UPDATE dalton_coverage_units
                SET ticker = :ticker,
                    ticker_key = :ticker_key
                WHERE unit_id = :unit_id
                """
            ),
            {"ticker": cleaned_ticker, "ticker_key": norm_key, "unit_id": unit_id},
        )


def check_upgrade_conflicts(conn: Any) -> None:
    """Abort ``d041.upgrade`` if any active ``(kind, slug)`` or ``ticker_key`` collides."""
    slug_conflicts = conn.execute(sa.text(UPGRADE_SLUG_CONFLICT_SQL)).fetchall()
    ticker_conflicts = conn.execute(sa.text(UPGRADE_TICKER_CONFLICT_SQL)).fetchall()
    err = format_upgrade_conflict_error(slug_conflicts, ticker_conflicts)
    if err:
        print(err)
        raise RuntimeError(err)


def check_downgrade_conflicts(conn: Any) -> None:
    """Abort ``d041.downgrade`` if d040 cannot safely hold current rows/automations."""
    owner_dups = conn.execute(sa.text(DOWNGRADE_OWNER_SLUG_CONFLICT_SQL)).fetchall()
    cross_bindings = conn.execute(sa.text(DOWNGRADE_CROSS_OWNER_BINDING_SQL)).fetchall()
    colleague_autos = conn.execute(
        sa.text(DOWNGRADE_COLLEAGUE_MANAGED_AUTOMATIONS_SQL)
    ).fetchall()
    err = format_downgrade_conflict_error(
        {
            "owner_slug_duplicates": owner_dups,
            "cross_owner_bindings": cross_bindings,
            "colleague_automations": colleague_autos,
        }
    )
    if err:
        print(err)
        raise RuntimeError(err)


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    conn.execute(sa.text("LOCK TABLE dalton_coverage_units IN ACCESS EXCLUSIVE MODE"))

    conn.execute(
        sa.text(
            """
            ALTER TABLE dalton_coverage_units
                ADD COLUMN IF NOT EXISTS created_by_user_id   TEXT,
                ADD COLUMN IF NOT EXISTS lead_analyst_user_id TEXT,
                ADD COLUMN IF NOT EXISTS ticker_key           TEXT
            """
        )
    )

    backfill_d041(conn)
    check_upgrade_conflicts(conn)

    conn.execute(
        sa.text(
            """
            ALTER TABLE dalton_coverage_units
                ALTER COLUMN created_by_user_id SET NOT NULL,
                ALTER COLUMN lead_analyst_user_id SET NOT NULL,
                DROP CONSTRAINT IF EXISTS ck_dalton_coverage_units_ticker_key_sync,
                ADD CONSTRAINT ck_dalton_coverage_units_ticker_key_sync CHECK (
                    (ticker IS NULL AND ticker_key IS NULL)
                    OR (
                        ticker IS NOT NULL
                        AND kind = 'company'
                        AND ticker_key IS NOT NULL
                        AND ticker_key !~ '^\\s*$'
                    )
                ),
                DROP CONSTRAINT IF EXISTS uq_dalton_coverage_units_owner_kind_slug
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_dalton_coverage_units_active_kind_slug
                ON dalton_coverage_units (kind, slug)
                WHERE status = 'active'
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_dalton_coverage_units_active_ticker_key
                ON dalton_coverage_units (ticker_key)
                WHERE status = 'active' AND kind = 'company' AND ticker_key IS NOT NULL
            """
        )
    )
    conn.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS idx_dalton_coverage_units_lead_kind
                ON dalton_coverage_units (lead_analyst_user_id, kind, created_at)
            """
        )
    )
    conn.execute(
        sa.text(
            """
            ALTER TABLE automation_executions
                ADD COLUMN IF NOT EXISTS coverage_unit_id UUID,
                ADD COLUMN IF NOT EXISTS coverage_unit_version TIMESTAMPTZ
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    conn.execute(sa.text("LOCK TABLE dalton_coverage_units IN ACCESS EXCLUSIVE MODE"))

    check_downgrade_conflicts(conn)

    conn.execute(
        sa.text(
            """
            ALTER TABLE automation_executions
                DROP COLUMN IF EXISTS coverage_unit_version,
                DROP COLUMN IF EXISTS coverage_unit_id
            """
        )
    )
    conn.execute(sa.text("DROP INDEX IF EXISTS idx_dalton_coverage_units_lead_kind"))
    conn.execute(
        sa.text("DROP INDEX IF EXISTS uq_dalton_coverage_units_active_ticker_key")
    )
    conn.execute(
        sa.text("DROP INDEX IF EXISTS uq_dalton_coverage_units_active_kind_slug")
    )
    conn.execute(
        sa.text(
            """
            ALTER TABLE dalton_coverage_units
                DROP CONSTRAINT IF EXISTS ck_dalton_coverage_units_ticker_key_sync,
                ADD CONSTRAINT uq_dalton_coverage_units_owner_kind_slug
                    UNIQUE (owner_user_id, kind, slug),
                DROP COLUMN IF EXISTS ticker_key,
                DROP COLUMN IF EXISTS lead_analyst_user_id,
                DROP COLUMN IF EXISTS created_by_user_id
            """
        )
    )
