"""Dalton d009: OpenClaw conversation archive tables, pgvector HNSW index, and pg_trgm search index.

Requires the PostgreSQL 18 image built from ``scripts/dalton/Dockerfile.postgres``
(``dalton-postgres:18-pgvector``, Debian trixie / glibc 2.41 with ``postgresql-18-pgvector``)
so ``CREATE EXTENSION IF NOT EXISTS vector`` and ``pg_trgm`` are available without changing
the base OS collation of the existing PG18 data volume.
"""

from __future__ import annotations

from alembic import op

revision = "d009"
down_revision = "d008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_conversation_cards (
            card_id          TEXT PRIMARY KEY,
            source_type      TEXT NOT NULL DEFAULT 'conversation' CHECK (source_type = 'conversation'),
            agent            TEXT NOT NULL,
            session_id       TEXT NOT NULL,
            session_key      TEXT NOT NULL,
            generation       TEXT NOT NULL DEFAULT '',
            channel          TEXT NOT NULL DEFAULT '',
            chat_type        TEXT NOT NULL DEFAULT '',
            event_ids        TEXT[] NOT NULL DEFAULT '{}',
            seq_range        JSONB NOT NULL DEFAULT '[]'::jsonb,
            ts               TIMESTAMPTZ NOT NULL,
            date_et          DATE NOT NULL,
            speaker          JSONB NOT NULL,
            speaker_role     TEXT NOT NULL CHECK (speaker_role IN ('owner', 'participant', 'agent')),
            tickers          TEXT[] NOT NULL DEFAULT '{}',
            entities         TEXT[] NOT NULL DEFAULT '{}',
            sectors          TEXT[] NOT NULL DEFAULT '{}',
            topic_label      TEXT NOT NULL DEFAULT '',
            type             TEXT NOT NULL CHECK (type IN (
                'fact', 'estimate', 'model_assumption', 'opinion', 'question', 'decision', 'todo'
            )),
            claim            TEXT NOT NULL,
            figures          JSONB NOT NULL DEFAULT '[]'::jsonb,
            quote            TEXT NOT NULL,
            quote_event_id   TEXT NOT NULL,
            cited_sources    JSONB NOT NULL DEFAULT '[]'::jsonb,
            confidence       DOUBLE PRECISION NOT NULL DEFAULT 0.8 CHECK (confidence >= 0.0 AND confidence <= 1.0),
            status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'retracted')),
            supersedes       TEXT NULL REFERENCES dalton_conversation_cards(card_id) ON DELETE SET NULL,
            extractor_model  TEXT NOT NULL,
            prompt_version   TEXT NOT NULL,
            content_hash     TEXT NOT NULL UNIQUE,
            search_text      TEXT NOT NULL DEFAULT '',
            embedding        vector(3072),
            rendered_locations JSONB NOT NULL DEFAULT '[]'::jsonb,
            files_written_at TIMESTAMPTZ,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    # pgvector HNSW index: 3072-d vectors use halfvec(3072) expression index on PG18 pgvector 0.7+/0.8+
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_conv_cards_embedding_hnsw
        ON dalton_conversation_cards
        USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops)
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_conv_cards_search_trgm
        ON dalton_conversation_cards
        USING gin (search_text gin_trgm_ops)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_conv_cards_tickers_gin
        ON dalton_conversation_cards
        USING gin (tickers)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_conv_cards_status_date
        ON dalton_conversation_cards (status, date_et DESC, ts DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_conv_cards_speaker_type
        ON dalton_conversation_cards (speaker_role, type)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_conversation_processed (
            agent           TEXT NOT NULL,
            session_id      TEXT NOT NULL,
            event_id        TEXT NOT NULL,
            chunk_idx       INTEGER NOT NULL DEFAULT 0,
            chunk_count     INTEGER NOT NULL DEFAULT 1,
            chunk_max_chars INTEGER NOT NULL DEFAULT 6000,
            chunk_spans     JSONB NOT NULL DEFAULT '[]'::jsonb,
            status          TEXT NOT NULL CHECK (status IN ('extracted', 'discarded')),
            processed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (agent, session_id, event_id, chunk_idx)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_conversation_runs (
            run_id               TEXT PRIMARY KEY,
            mode                 TEXT NOT NULL CHECK (mode IN ('run', 'backfill', 'dry_run')),
            window_start         DATE,
            window_end           DATE,
            status               TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'interrupted')),
            extractor_model      TEXT NOT NULL,
            embedding_model      TEXT,
            prompt_version       TEXT NOT NULL,
            events_read          INTEGER NOT NULL DEFAULT 0,
            segments_processed   INTEGER NOT NULL DEFAULT 0,
            cards_extracted      INTEGER NOT NULL DEFAULT 0,
            cards_inserted       INTEGER NOT NULL DEFAULT 0,
            cards_deduped        INTEGER NOT NULL DEFAULT 0,
            cards_superseded     INTEGER NOT NULL DEFAULT 0,
            quotes_rejected      INTEGER NOT NULL DEFAULT 0,
            segments_discarded   INTEGER NOT NULL DEFAULT 0,
            input_tokens         BIGINT NOT NULL DEFAULT 0,
            output_tokens        BIGINT NOT NULL DEFAULT 0,
            total_tokens         BIGINT NOT NULL DEFAULT 0,
            stats                JSONB NOT NULL DEFAULT '{}'::jsonb,
            error_text           TEXT,
            started_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at         TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_conv_runs_started_at
        ON dalton_conversation_runs (started_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dalton_conversation_runs CASCADE")
    op.execute("DROP TABLE IF EXISTS dalton_conversation_processed CASCADE")
    op.execute("DROP TABLE IF EXISTS dalton_conversation_cards CASCADE")
