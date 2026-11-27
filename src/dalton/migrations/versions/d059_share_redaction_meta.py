"""Dalton d059: share redaction metadata tables.

Creates two Dalton-owned tables without touching upstream table schemas:
- ``dalton_org_library_title_meta``: records whether an ``org_library_items``
  entry was explicitly titled by its publisher after upgrade (``custom_title``).
  Pre-migration entries (no row) and ``custom_title = FALSE`` entries always
  render as ``"Shared Conversation"`` when the linked thread has effective
  ``allow_files = FALSE``.
- ``dalton_storage_key_migrations``: reconciliation ledger mapping legacy
  object keys to migrated ``private-media/`` keys per storage backend.

Revision ID: d059
Revises: d055
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d059"
down_revision: str | None = "d055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_org_library_title_meta (
            item_id      UUID PRIMARY KEY,
            custom_title BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_storage_key_migrations (
            backend     TEXT NOT NULL DEFAULT 'upstream',
            old_key     TEXT NOT NULL,
            new_key     TEXT NOT NULL,
            sha256      TEXT,
            byte_len    BIGINT,
            migrated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (backend, old_key)
        )
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS dalton_storage_key_migrations")
    op.execute("DROP TABLE IF EXISTS dalton_org_library_title_meta")
