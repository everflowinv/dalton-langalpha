"""Dalton d003: company-knowledge versions and event verdicts."""

from __future__ import annotations

from alembic import op

revision = "d003"
down_revision = "d002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_knowledge_versions (
            knowledge_version_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id        VARCHAR(255) NOT NULL DEFAULT 'local_dev_user',
            workspace_id   UUID NOT NULL,
            ticker         TEXT,
            doc_key        TEXT NOT NULL CHECK (doc_key IN ('company','thesis','debate','drivers','management','lessons','agenda','models')),
            version_no     INTEGER NOT NULL CHECK (version_no >= 1),
            source         TEXT NOT NULL CHECK (source IN ('tool','direct_edit','import','publish','server')),
            content        JSONB NOT NULL,
            content_sha256 VARCHAR(64) NOT NULL,
            ops            JSONB,
            reason         TEXT NOT NULL,
            evidence_refs  JSONB NOT NULL DEFAULT '[]',
            warnings       JSONB NOT NULL DEFAULT '[]',
            schema_valid   BOOLEAN NOT NULL DEFAULT TRUE,
            edited_path    TEXT,
            conversation_thread_id UUID,
            run_id         UUID,
            tool_call_id   TEXT,
            files_written_at TIMESTAMPTZ,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (workspace_id, doc_key, version_no)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_kv_latest
        ON dalton_knowledge_versions (workspace_id, doc_key, version_no DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_verdicts (
            verdict_id     TEXT PRIMARY KEY,
            user_id        VARCHAR(255) NOT NULL DEFAULT 'local_dev_user',
            workspace_id   UUID NOT NULL,
            ticker         TEXT,
            verdict_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            word           TEXT NOT NULL CHECK (word IN ('NO_CHANGE','THESIS_STRENGTHENED','THESIS_WEAKENED','THESIS_BROKEN','NEW_THESIS')),
            trigger_kind   TEXT NOT NULL,
            trigger_ref    TEXT,
            progress       TEXT NOT NULL CHECK (progress IN ('none','some','resolved')),
            next_due       DATE,
            payload        JSONB NOT NULL,
            conversation_thread_id UUID,
            run_id         UUID,
            automation_execution_id UUID,
            origin_type    TEXT,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dalton_verdicts_ws_time ON dalton_verdicts (workspace_id, verdict_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dalton_verdicts_user_time ON dalton_verdicts (user_id, verdict_at DESC)"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dalton_forbid_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'dalton: % on % is not allowed (append-only)', TG_OP, TG_TABLE_NAME;
        END $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_dalton_verdicts_immutable ON dalton_verdicts;
        CREATE TRIGGER trg_dalton_verdicts_immutable
        BEFORE UPDATE OR DELETE ON dalton_verdicts
        FOR EACH ROW EXECUTE FUNCTION dalton_forbid_mutation()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dalton_kv_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               OR NEW.content IS DISTINCT FROM OLD.content
               OR NEW.reason IS DISTINCT FROM OLD.reason
               OR NEW.version_no IS DISTINCT FROM OLD.version_no THEN
                RAISE EXCEPTION 'dalton: knowledge versions are immutable';
            END IF;
            RETURN NEW;
        END $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_dalton_kv_guard ON dalton_knowledge_versions;
        CREATE TRIGGER trg_dalton_kv_guard
        BEFORE UPDATE OR DELETE ON dalton_knowledge_versions
        FOR EACH ROW EXECUTE FUNCTION dalton_kv_guard()
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dalton_verdicts CASCADE")
    op.execute("DROP TABLE IF EXISTS dalton_knowledge_versions CASCADE")
    op.execute("DROP FUNCTION IF EXISTS dalton_kv_guard()")
    op.execute("DROP FUNCTION IF EXISTS dalton_forbid_mutation()")
