"""Add dalton_members and dalton_invites tables for invite signup."""

from __future__ import annotations

from alembic import op

revision = "d016"
down_revision = "d015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_members (
            email TEXT PRIMARY KEY CHECK (email = LOWER(email)),
            username TEXT UNIQUE NULL CHECK (username IS NULL OR username = LOWER(username)),
            source TEXT NOT NULL CHECK (source IN ('env', 'invite')),
            invited_by_user_id TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_invites (
            invite_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            token_sha256 TEXT NOT NULL UNIQUE,
            created_by_user_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            redeeming_username TEXT NULL CHECK (redeeming_username IS NULL OR redeeming_username = LOWER(redeeming_username)),
            redeeming_at TIMESTAMPTZ NULL,
            used_at TIMESTAMPTZ NULL,
            used_by_email TEXT NULL,
            supabase_user_id TEXT NULL,
            revoked_at TIMESTAMPTZ NULL
        );
        CREATE INDEX IF NOT EXISTS idx_dalton_invites_created_at
            ON dalton_invites (created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dalton_invites;")
    op.execute("DROP TABLE IF EXISTS dalton_members;")
