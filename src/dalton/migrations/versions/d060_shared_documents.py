"""Dalton d060: non-HTML shared document registry and member ACL.

Non-HTML workspace files (md, pdf, xlsx, etc.) are shared with 4-tier visibility
without creating monitor panel entries in ``dalton_monitors``.

Revision ID: d060
Revises: d059
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d060"
down_revision: str | None = "d059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_shared_documents (
            doc_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            artifact_id UUID NOT NULL UNIQUE
                REFERENCES share_links(artifact_id) ON DELETE CASCADE,
            owner_user_id VARCHAR(255) NOT NULL,
            title TEXT NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'own'
                CHECK (visibility IN ('own', 'members', 'company', 'public')),
            published_seq INTEGER NOT NULL DEFAULT 0 CHECK (published_seq >= 0),
            keep_updated BOOLEAN NOT NULL DEFAULT FALSE,
            acl_revision INTEGER NOT NULL DEFAULT 1 CHECK (acl_revision >= 1),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT dalton_shared_documents_title_length CHECK (
                char_length(BTRIM(title)) BETWEEN 1 AND 300
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_shared_documents_owner
            ON dalton_shared_documents (owner_user_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_shared_documents_updated
            ON dalton_shared_documents (updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_shared_document_members (
            doc_id UUID NOT NULL
                REFERENCES dalton_shared_documents(doc_id) ON DELETE CASCADE,
            user_id VARCHAR(255) NOT NULL,
            PRIMARY KEY (doc_id, user_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_shared_document_members_user
            ON dalton_shared_document_members (user_id)
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS dalton_shared_document_members")
    op.execute("DROP TABLE IF EXISTS dalton_shared_documents")
