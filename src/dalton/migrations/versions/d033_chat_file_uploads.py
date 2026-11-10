"""Chat composer large-file upload state table (d033).

Revision ID: d033
Revises: d030
Create Date: 2026-11-09
"""

from __future__ import annotations

from alembic import op

revision = "d033"
down_revision = "d030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_chat_file_uploads (
            upload_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            safe_filename TEXT NOT NULL,
            mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
            size_bytes BIGINT NOT NULL CHECK (size_bytes > 0),
            part_size INTEGER NOT NULL CHECK (part_size > 0),
            part_count INTEGER NOT NULL CHECK (part_count > 0),
            object_key TEXT NOT NULL,
            store_upload_id TEXT NOT NULL,
            uploaded_parts JSONB NOT NULL DEFAULT '{}'::jsonb,
            pending_claims JSONB NOT NULL DEFAULT '{}'::jsonb,
            expected_sha256 TEXT,
            actual_sha256 TEXT,
            state TEXT NOT NULL CHECK (state IN ('uploading', 'completing', 'ready', 'aborted', 'failed')),
            error TEXT,
            claim_token TEXT,
            lease_until TIMESTAMPTZ,
            used_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ,
            cleaned_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT chk_dalton_chat_file_uploads_cleaned_terminal
                CHECK (cleaned_at IS NULL OR state IN ('aborted', 'failed'))
        );
        """
    )

    op.execute(
        """
        ALTER TABLE dalton_chat_file_uploads
            ADD COLUMN IF NOT EXISTS claim_token TEXT;
        """
    )

    op.execute(
        """
        ALTER TABLE dalton_chat_file_uploads
            ADD COLUMN IF NOT EXISTS pending_claims JSONB NOT NULL DEFAULT '{}'::jsonb;
        """
    )

    op.execute(
        """
        ALTER TABLE dalton_chat_file_uploads
            ALTER COLUMN expires_at DROP NOT NULL;
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'chk_dalton_chat_file_uploads_cleaned_terminal'
            ) THEN
                ALTER TABLE dalton_chat_file_uploads
                    ADD CONSTRAINT chk_dalton_chat_file_uploads_cleaned_terminal
                    CHECK (cleaned_at IS NULL OR state IN ('aborted', 'failed'));
            END IF;
        END $$;
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_chat_file_uploads_user_state
            ON dalton_chat_file_uploads (user_id, state);
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_chat_file_uploads_cleanup_due
            ON dalton_chat_file_uploads (state, expires_at)
            WHERE cleaned_at IS NULL;
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_chat_file_uploads_completing_lease
            ON dalton_chat_file_uploads (lease_until)
            WHERE state = 'completing' AND cleaned_at IS NULL;
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_chat_file_uploads_workspace_retained
            ON dalton_chat_file_uploads (workspace_id, created_at ASC)
            WHERE state = 'ready' AND used_at IS NOT NULL AND cleaned_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_dalton_chat_file_uploads_workspace_retained;")
    op.execute("DROP INDEX IF EXISTS idx_dalton_chat_file_uploads_completing_lease;")
    op.execute("DROP INDEX IF EXISTS idx_dalton_chat_file_uploads_cleanup_due;")
    op.execute("DROP INDEX IF EXISTS idx_dalton_chat_file_uploads_user_state;")
    op.execute("DROP TABLE IF EXISTS dalton_chat_file_uploads;")
