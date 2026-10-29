"""Dalton d022: audio transcription job ledger (`dalton_transcription_jobs`)."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d022"
down_revision: Union[str, None] = "d021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_transcription_jobs (
            job_id               TEXT PRIMARY KEY DEFAULT (gen_random_uuid()::text),
            created_by_user_id   TEXT NOT NULL,
            workspace_id         TEXT NOT NULL,
            thread_id            TEXT NOT NULL DEFAULT '',
            source_object_key    TEXT NOT NULL,
            source_size_bytes    BIGINT NOT NULL CHECK (source_size_bytes >= 0),
            source_sha256        TEXT NOT NULL,
            language             TEXT,
            hotwords             JSONB NOT NULL DEFAULT '[]'::jsonb,
            status               TEXT NOT NULL DEFAULT 'awaiting_upload'
                CHECK (status IN (
                    'awaiting_upload',
                    'queued',
                    'downloading',
                    'probing',
                    'splitting',
                    'submitting',
                    'transcribing',
                    'assembling',
                    'succeeded',
                    'failed',
                    'cancelled',
                    'indeterminate'
                )),
            stage                TEXT NOT NULL DEFAULT 'awaiting_upload',
            segment_count        INTEGER NOT NULL DEFAULT 0 CHECK (segment_count >= 0),
            completed_segments   INTEGER NOT NULL DEFAULT 0 CHECK (completed_segments >= 0),
            provider_request_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            usage                JSONB NOT NULL DEFAULT '{}'::jsonb,
            result_object_key    TEXT,
            error_category       TEXT,
            error_message        TEXT,
            idempotency_key      TEXT NOT NULL UNIQUE,
            version              BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
            created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at         TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_transcription_jobs_user_created
            ON dalton_transcription_jobs (created_by_user_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_transcription_jobs_workspace_created
            ON dalton_transcription_jobs (workspace_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_transcription_jobs_status_updated
            ON dalton_transcription_jobs (status, updated_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS idx_dalton_transcription_jobs_status_updated")
    op.execute("DROP INDEX IF EXISTS idx_dalton_transcription_jobs_workspace_created")
    op.execute("DROP INDEX IF EXISTS idx_dalton_transcription_jobs_user_created")
    op.execute("DROP TABLE IF EXISTS dalton_transcription_jobs")
