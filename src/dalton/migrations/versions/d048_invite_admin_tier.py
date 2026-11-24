"""Dalton d048: allow ``admin`` target tier on ``dalton_invites``.

Expands ``ck_dalton_invites_tier`` (added in ``d047``) from
``('internal', 'external')`` to ``('admin', 'internal', 'external')`` so
administrators in ``DALTON_AUTH_MODE=local`` can issue single-use invites for
any access tier (external, internal, or administrator).

Touches only the Dalton-owned ``dalton_invites`` table (expand-only).

Revision ID: d048
Revises: d047
Create Date: 2026-11-23
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d048"
down_revision: Union[str, None] = "d047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE dalton_invites
            DROP CONSTRAINT IF EXISTS ck_dalton_invites_tier;
        ALTER TABLE dalton_invites
            ADD CONSTRAINT ck_dalton_invites_tier
            CHECK (tier IN ('admin', 'internal', 'external'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE dalton_invites
           SET revoked_at = NOW()
         WHERE tier = 'admin'
           AND used_at IS NULL
           AND revoked_at IS NULL;
        ALTER TABLE dalton_invites
            DROP CONSTRAINT IF EXISTS ck_dalton_invites_tier;
        ALTER TABLE dalton_invites
            ADD CONSTRAINT ck_dalton_invites_tier
            CHECK (
                tier IN ('internal', 'external')
                OR (tier = 'admin' AND (used_at IS NOT NULL OR revoked_at IS NOT NULL))
            );
        """
    )
