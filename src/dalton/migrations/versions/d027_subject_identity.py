"""Dalton d027: subject identity on dalton_watch_state and dalton_publications (sector layer P3).

Both tables historically keyed their subject on a NOT NULL ``ticker``. A sector
coverage unit has no ticker and must never get one (design §③.2: ticker 只用于
证券相关功能), so P3's sector tracking and publication rows need a subject
identity instead:

- ``ticker`` becomes nullable;
- ``unit_id`` (nullable UUID) and ``subject_kind`` ('company' | 'sector', NOT
  NULL, backfilled 'company') are added;
- a CHECK pins the combination: company ⇒ ticker; sector ⇒ unit_id and no
  ticker.

Backfill rides on ``ADD COLUMN ... NOT NULL DEFAULT 'company'`` — a catalog-only
change on Postgres 11+ that rewrites no rows, which also matters because
``dalton_publications`` carries a ``BEFORE UPDATE OR DELETE`` immutability
trigger (``dalton_forbid_mutation``): DDL never fires row triggers, and no
``UPDATE`` is issued here. Existing tickers are untouched.

Downgrade refuses while any sector row exists (``RAISE EXCEPTION USING
MESSAGE``), because restoring ``ticker NOT NULL`` would either fail or force
deleting sector history; the operator must archive or migrate those rows first.
"""

from __future__ import annotations

from alembic import op

revision = "d027"
down_revision = "d026"
branch_labels = None
depends_on = None

# Same constraint on both tables; sector ⇒ unit_id and no ticker, company ⇒
# ticker. subject_kind is NOT NULL with the backfilled default, so the OR
# below also pins it to exactly the two allowed values.
_SUBJECT_KIND_CHECK = """
        ADD CONSTRAINT {name} CHECK (
            (subject_kind = 'company' AND ticker IS NOT NULL)
            OR (subject_kind = 'sector' AND ticker IS NULL AND unit_id IS NOT NULL)
        )
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # dalton_watch_state: sector tracking rows (agenda/delta state) carry
    # unit_id + subject_kind instead of a ticker.
    op.execute("ALTER TABLE dalton_watch_state ALTER COLUMN ticker DROP NOT NULL")
    op.execute("ALTER TABLE dalton_watch_state ADD COLUMN unit_id UUID")
    op.execute(
        "ALTER TABLE dalton_watch_state ADD COLUMN subject_kind TEXT NOT NULL DEFAULT 'company'"
    )
    op.execute(
        "ALTER TABLE dalton_watch_state"
        + _SUBJECT_KIND_CHECK.format(name="ck_dalton_watch_state_subject")
    )
    # dalton_publications: same subject identity. DDL only — the immutability
    # trigger on this table forbids row rewrites, and none happen here.
    op.execute("ALTER TABLE dalton_publications ALTER COLUMN ticker DROP NOT NULL")
    op.execute("ALTER TABLE dalton_publications ADD COLUMN unit_id UUID")
    op.execute(
        "ALTER TABLE dalton_publications ADD COLUMN subject_kind TEXT NOT NULL DEFAULT 'company'"
    )
    op.execute(
        "ALTER TABLE dalton_publications"
        + _SUBJECT_KIND_CHECK.format(name="ck_dalton_publications_subject")
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Refuse while sector rows exist: restoring NOT NULL on ticker would fail
    # against them, and silently deleting published history or watch state is
    # never acceptable. Message names the tables so the operator knows what to
    # archive or migrate before retrying.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM dalton_publications WHERE subject_kind = 'sector') THEN
                RAISE EXCEPTION USING MESSAGE =
                    'd027 downgrade refused: dalton_publications still holds sector-subject rows; archive or migrate them before downgrading';
            END IF;
            IF EXISTS (SELECT 1 FROM dalton_watch_state WHERE subject_kind = 'sector') THEN
                RAISE EXCEPTION USING MESSAGE =
                    'd027 downgrade refused: dalton_watch_state still holds sector-subject rows; archive or migrate them before downgrading';
            END IF;
        END
        $$
        """
    )
    op.execute("ALTER TABLE dalton_publications DROP CONSTRAINT IF EXISTS ck_dalton_publications_subject")
    op.execute("ALTER TABLE dalton_publications DROP COLUMN IF EXISTS subject_kind")
    op.execute("ALTER TABLE dalton_publications DROP COLUMN IF EXISTS unit_id")
    op.execute("ALTER TABLE dalton_publications ALTER COLUMN ticker SET NOT NULL")
    op.execute("ALTER TABLE dalton_watch_state DROP CONSTRAINT IF EXISTS ck_dalton_watch_state_subject")
    op.execute("ALTER TABLE dalton_watch_state DROP COLUMN IF EXISTS subject_kind")
    op.execute("ALTER TABLE dalton_watch_state DROP COLUMN IF EXISTS unit_id")
    op.execute("ALTER TABLE dalton_watch_state ALTER COLUMN ticker SET NOT NULL")
