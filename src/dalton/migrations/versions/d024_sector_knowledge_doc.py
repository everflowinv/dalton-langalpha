"""Dalton d024: allow the sector knowledge doc (sector layer P1).

d003's CHECK on ``dalton_knowledge_versions.doc_key`` admits the eight company
docs only. The sector profile writes one more doc key — ``sector`` — through
the same versioned pipeline, so this migration widens the constraint to nine.
It changes no data and no other rule: the uniqueness, immutability trigger and
source CHECK of d003 stay untouched.

Downgrade refuses to run while any ``sector`` rows exist: knowledge history is
append-only by trigger, so silently deleting rows to satisfy the old constraint
would destroy record. With none present it restores d003's exact constraint.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d024"
down_revision: Union[str, None] = "d023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DOC_KEY_CONSTRAINT = "dalton_knowledge_versions_doc_key_check"

_COMPANY_DOC_KEYS = (
    "'company','thesis','debate','drivers','management','lessons','agenda','models'"
)
_SECTOR_DOC_KEYS = (
    "'company','sector','thesis','debate','drivers','management','lessons','agenda','models'"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"ALTER TABLE dalton_knowledge_versions DROP CONSTRAINT IF EXISTS {_DOC_KEY_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE dalton_knowledge_versions "
        f"ADD CONSTRAINT {_DOC_KEY_CONSTRAINT} CHECK (doc_key IN ({_SECTOR_DOC_KEYS}))"
    )


def downgrade() -> None:
    # No literal percent signs anywhere: this statement runs without bind
    # parameters, but keeping it percent-free means a later refactor that adds
    # parameters cannot turn the message into a broken format string.
    op.execute(
        """
        DO $downgrade_d024$
        DECLARE sector_rows integer;
        BEGIN
            SELECT COUNT(*) INTO sector_rows
            FROM dalton_knowledge_versions
            WHERE doc_key = 'sector';
            IF sector_rows > 0 THEN
                RAISE EXCEPTION USING MESSAGE =
                    'd024 downgrade refused: ' || sector_rows::text ||
                    ' sector knowledge version row(s) exist; knowledge history is '
                    'append-only, so restoring the eight-doc constraint would require '
                    'deleting rows. Keep the constraint, or archive the workspace '
                    'instead.';
            END IF;
        END
        $downgrade_d024$
        """
    )
    op.execute(f"ALTER TABLE dalton_knowledge_versions DROP CONSTRAINT IF EXISTS {_DOC_KEY_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE dalton_knowledge_versions "
        f"ADD CONSTRAINT {_DOC_KEY_CONSTRAINT} CHECK (doc_key IN ({_COMPANY_DOC_KEYS}))"
    )
