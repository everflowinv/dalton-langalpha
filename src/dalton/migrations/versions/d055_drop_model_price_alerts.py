"""Dalton d055: drop unused `dalton_model_price_alerts` table.

Scheduled third-party price watching and alert generation were cancelled before
launch; pricing metadata is maintained directly by administrators with explicit
source annotations (`official` / `estimated` / `third_party`). This migration
removes the empty `dalton_model_price_alerts` table and its index created in
`d051`, while `downgrade()` recreates them identically to `d051`.

Revision ID: d055
Revises: d054
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d055"
down_revision: str | None = "d054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS ix_dalton_price_alerts_status")
    op.execute("DROP TABLE IF EXISTS dalton_model_price_alerts")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_model_price_alerts (
            alert_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            model_key             TEXT NOT NULL,
            provider              TEXT NOT NULL,
            model_id              TEXT NOT NULL,
            card_target           TEXT NOT NULL CHECK (card_target IN ('cash', 'notional')),
            alert_kind            TEXT NOT NULL CHECK (alert_kind IN ('public_price_listed', 'public_price_changed')),
            match_confidence      TEXT NOT NULL CHECK (match_confidence IN ('exact_route', 'reference_only')),
            base_catalog_revision BIGINT NOT NULL,
            current_pricing       JSONB NOT NULL,
            detected_pricing      JSONB NOT NULL,
            source_name           TEXT NOT NULL CHECK (source_name IN ('openrouter', 'litellm')),
            source_url            TEXT NULL,
            fingerprint           TEXT NOT NULL UNIQUE,
            status                TEXT NOT NULL DEFAULT 'pending'
                                  CHECK (status IN ('pending', 'applied', 'dismissed')),
            applied_revision      BIGINT NULL REFERENCES dalton_team_model_catalog_revisions(revision),
            detected_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_by           TEXT NULL,
            resolved_at           TIMESTAMPTZ NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_price_alerts_status
            ON dalton_model_price_alerts (status, detected_at DESC)
        """
    )
