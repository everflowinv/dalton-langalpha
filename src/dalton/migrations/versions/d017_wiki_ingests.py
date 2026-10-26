"""Dalton d017: company-wiki ingestion audit ledger (`dalton_wiki_ingests`)."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d017"
down_revision: Union[str, None] = "d016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_wiki_ingests (
            ingest_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id      TEXT NOT NULL,
            workspace_id TEXT,
            thread_id    TEXT,
            target_type  TEXT NOT NULL
                CHECK (target_type IN ('company', 'sector')),
            target       TEXT NOT NULL,
            content_type TEXT NOT NULL,
            doc_date     TEXT NOT NULL,
            sha256       TEXT NOT NULL,
            doc_id       TEXT,
            filepath     TEXT,
            status       TEXT NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_wiki_ingests_user_created
            ON dalton_wiki_ingests (user_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_wiki_ingests_sha256
            ON dalton_wiki_ingests (sha256)
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS dalton_wiki_ingests")
