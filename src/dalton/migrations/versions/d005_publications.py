"""Dalton d005: publication requests, immutable publications, and archived file bytes (PK.8)."""

from __future__ import annotations

from alembic import op

revision = "d005"
down_revision = "d004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_publish_requests (
            publish_request_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id        VARCHAR(255) NOT NULL DEFAULT 'local_dev_user',
            workspace_id   UUID NOT NULL,
            conversation_thread_id UUID NOT NULL,
            tool_call_id   TEXT NOT NULL,
            ticker         TEXT,
            kind           TEXT NOT NULL CHECK (kind IN ('initial_coverage','full_coverage','bullbear','event_note','earnings_preview',
                                                         'earnings_review','thesis_update','topic','conviction_call')),
            title          TEXT NOT NULL,
            slug           TEXT NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{2,60}$'),
            draft_path     TEXT NOT NULL,
            attachments    JSONB NOT NULL DEFAULT '[]',
            bundle_sha256  VARCHAR(64) NOT NULL,
            updates_thesis BOOLEAN NOT NULL DEFAULT FALSE,
            thesis_version_no INTEGER,
            status         TEXT NOT NULL CHECK (status IN ('auditing','audit_failed','reviewing','review_revise',
                             'review_unavailable','awaiting_pm','approved','publishing','published','rejected','superseded','expired')),
            audit          JSONB,
            review         JSONB,
            decided_by     VARCHAR(255),
            decided_at     TIMESTAMPTZ,
            decision_message TEXT,
            publication_id UUID,
            error          TEXT,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (conversation_thread_id, tool_call_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dalton_pr_ws_status ON dalton_publish_requests (workspace_id, status)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_publications (
            publication_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            publish_request_id UUID NOT NULL UNIQUE REFERENCES dalton_publish_requests(publish_request_id),
            user_id        VARCHAR(255) NOT NULL DEFAULT 'local_dev_user',
            workspace_id   UUID NOT NULL,
            ticker         TEXT NOT NULL,
            kind           TEXT NOT NULL,
            title          TEXT NOT NULL,
            published_date DATE NOT NULL DEFAULT CURRENT_DATE,
            files          JSONB NOT NULL DEFAULT '[]',
            bundle_sha256  VARCHAR(64) NOT NULL,
            sources        JSONB NOT NULL DEFAULT '[]',
            audit_summary  JSONB NOT NULL DEFAULT '{}',
            reviewer       JSONB NOT NULL DEFAULT '{}',
            approver_user_id VARCHAR(255) NOT NULL,
            approved_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            thesis_version_no INTEGER,
            supersedes_publication_id UUID REFERENCES dalton_publications(publication_id),
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dalton_pub_ws_date ON dalton_publications (workspace_id, published_date DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_publication_files (
            publication_id UUID NOT NULL REFERENCES dalton_publications(publication_id),
            path           TEXT NOT NULL,
            sha256         VARCHAR(64) NOT NULL,
            mime           TEXT NOT NULL,
            content        BYTEA NOT NULL,
            PRIMARY KEY (publication_id, path)
        )
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_dalton_pub_immutable ON dalton_publications;
        CREATE TRIGGER trg_dalton_pub_immutable
        BEFORE UPDATE OR DELETE ON dalton_publications
        FOR EACH ROW EXECUTE FUNCTION dalton_forbid_mutation()
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_dalton_pubfiles_immutable ON dalton_publication_files;
        CREATE TRIGGER trg_dalton_pubfiles_immutable
        BEFORE UPDATE OR DELETE ON dalton_publication_files
        FOR EACH ROW EXECUTE FUNCTION dalton_forbid_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dalton_publication_files CASCADE")
    op.execute("DROP TABLE IF EXISTS dalton_publications CASCADE")
    op.execute("DROP TABLE IF EXISTS dalton_publish_requests CASCADE")
