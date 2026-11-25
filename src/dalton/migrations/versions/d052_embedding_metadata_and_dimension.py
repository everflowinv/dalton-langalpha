"""Dalton d052: embedding metadata lock table (`dalton_embedding_metadata`) and 1536/3072 dimension contract.

Creates ``dalton_embedding_metadata`` to record the active embedding space for
each scope (starting with ``conversation_archive``) while separating the stored
``model_label`` (e.g. ``gemini-embedding-2-preview`` in Everflow) from the
upstream ``api_model_name`` sent over HTTP (e.g. ``gemini-embedding-2``).

Deployment & migration execution order (aligned with Batch 1 / Codex r3 #6):
- **Everflow production** (``docker-compose.dalton.yml``):
  ``scripts/dalton/docker_entrypoint_dalton.sh`` executes
  ``uv run alembic -c src/dalton/alembic.ini upgrade head`` before starting the
  backend, or operators run it manually before ``docker compose up -d --no-deps backend``.
  Existing 3072-d conversation archive data stays untouched and is seeded into
  ``dalton_embedding_metadata`` with ``model_label='gemini-embedding-2-preview'``,
  ``api_model_name='gemini-embedding-2'``, ``dimensions=3072``.
- **Standalone Core** (``docker-compose.core.yml``):
  ``backend`` overrides its entrypoint to skip migrations; operators MUST run
  the one-shot ``migrate`` service (``scripts/standalone/migrate.sh``) to
  completion (``service_completed_successfully``) BEFORE starting or recreating
  ``backend``. On a fresh empty Core database (0 embedded rows), ``d052`` aligns
  ``dalton_conversation_cards.embedding`` and ``ix_dalton_conv_cards_embedding_hnsw``
  to the configured Core dimension (default 1536-d) right away so ``EXPLAIN`` hits
  the ``halfvec(1536)`` HNSW index immediately, while keeping
  ``dalton_embedding_metadata`` unlocked until the first archive/embedding write
  so operators can still adjust dimensions before writing vectors.

Downgrade / rollback contract (1536-d vs 3072-d):
- Everflow (3072-d legacy Gemini retrieval unchanged): ``downgrade()`` verifies
  the locked metadata matches the legacy 3072-d Gemini retrieval space and drops
  ``dalton_embedding_metadata`` cleanly.
- Standalone Core with an empty ``dalton_conversation_cards`` table altered to
  1536-d: ``downgrade()`` restores ``embedding`` to ``vector(3072)`` and rebuilds
  ``ix_dalton_conv_cards_embedding_hnsw`` on ``halfvec(3072)`` before dropping
  ``dalton_embedding_metadata``.
- Standalone Core that has already written vectors using a non-3072-d dimension,
  another provider/model (such as 3072-d OpenAI), or non-retrieval task types:
  in-place downgrade raises an explicit error; restore from the pre-upgrade
  PostgreSQL backup before rolling back container versions.

Revision ID: d052
Revises: d049
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from src.dalton.embeddings.config import resolve_embedding_config
from src.dalton.embeddings.metadata import (
    build_alter_embedding_column_ddl,
    build_halfvec_index_ddl,
)
from src.dalton.profile import DeploymentProfile, resolve_profile

revision: str = "d052"
down_revision: str | None = "d049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_embedding_metadata (
            scope           TEXT PRIMARY KEY CHECK (btrim(scope) <> ''),
            provider        TEXT NOT NULL CHECK (btrim(provider) <> ''),
            model_label     TEXT NOT NULL CHECK (btrim(model_label) <> ''),
            api_model_name  TEXT NOT NULL CHECK (btrim(api_model_name) <> ''),
            dimensions      INTEGER NOT NULL CHECK (dimensions > 0 AND dimensions <= 4000),
            task_type_doc   TEXT NOT NULL DEFAULT 'RETRIEVAL_DOCUMENT'
                            CHECK (btrim(task_type_doc) <> ''),
            task_type_query TEXT NOT NULL DEFAULT 'RETRIEVAL_QUERY'
                            CHECK (btrim(task_type_query) <> ''),
            locked_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    # Always seed Everflow 3072-d metadata when existing embedded cards are
    # present in `dalton_conversation_cards`, or when migrating under the
    # Everflow profile. Fresh Standalone Core installs with an empty archive
    # stay unlocked so first initialization can lock 1536-d (or customer-chosen
    # dimensions) cleanly.
    is_everflow = resolve_profile() is DeploymentProfile.EVERFLOW
    if is_everflow:
        op.execute(
            """
            INSERT INTO dalton_embedding_metadata (
                scope, provider, model_label, api_model_name,
                dimensions, task_type_doc, task_type_query
            )
            VALUES (
                'conversation_archive',
                'gemini',
                'gemini-embedding-2-preview',
                'gemini-embedding-2',
                3072,
                'RETRIEVAL_DOCUMENT',
                'RETRIEVAL_QUERY'
            )
            ON CONFLICT (scope) DO NOTHING
            """
        )
    else:
        target_dim = resolve_embedding_config(profile=DeploymentProfile.CORE).dimensions
        alter_col_ddl = build_alter_embedding_column_ddl(target_dim)
        create_idx_ddl = build_halfvec_index_ddl(target_dim)
        op.execute(
            f"""
            DO $$
            DECLARE
                col_type TEXT;
            BEGIN
                IF to_regclass('public.dalton_conversation_cards') IS NOT NULL THEN
                    IF EXISTS (
                        SELECT 1 FROM dalton_conversation_cards WHERE embedding IS NOT NULL
                    ) THEN
                        INSERT INTO dalton_embedding_metadata (
                            scope, provider, model_label, api_model_name,
                            dimensions, task_type_doc, task_type_query
                        )
                        VALUES (
                            'conversation_archive',
                            'gemini',
                            'gemini-embedding-2-preview',
                            'gemini-embedding-2',
                            3072,
                            'RETRIEVAL_DOCUMENT',
                            'RETRIEVAL_QUERY'
                        )
                        ON CONFLICT (scope) DO NOTHING;
                    ELSE
                        SELECT format_type(a.atttypid, a.atttypmod) INTO col_type
                        FROM pg_attribute a
                        WHERE a.attrelid = 'public.dalton_conversation_cards'::regclass
                          AND a.attname = 'embedding'
                          AND NOT a.attisdropped;

                        IF col_type IS NOT NULL AND col_type <> 'vector({target_dim})' THEN
                            DROP INDEX IF EXISTS ix_dalton_conv_cards_embedding_hnsw;
                            {alter_col_ddl};
                            {create_idx_ddl};
                        END IF;
                    END IF;
                END IF;
            END
            $$;
            """
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        DO $$
        DECLARE
            col_type TEXT;
            has_locked_row BOOLEAN := FALSE;
            locked_provider TEXT;
            locked_model_label TEXT;
            locked_api_model TEXT;
            locked_dims INTEGER;
            locked_task_doc TEXT;
            locked_task_query TEXT;
            embedded_count BIGINT := 0;
        BEGIN
            IF to_regclass('public.dalton_embedding_metadata') IS NOT NULL THEN
                SELECT
                    provider,
                    model_label,
                    api_model_name,
                    dimensions,
                    task_type_doc,
                    task_type_query
                INTO
                    locked_provider,
                    locked_model_label,
                    locked_api_model,
                    locked_dims,
                    locked_task_doc,
                    locked_task_query
                FROM dalton_embedding_metadata
                WHERE scope = 'conversation_archive';
                has_locked_row := FOUND;
            END IF;

            IF to_regclass('public.dalton_conversation_cards') IS NOT NULL THEN
                SELECT format_type(a.atttypid, a.atttypmod) INTO col_type
                FROM pg_attribute a
                WHERE a.attrelid = 'public.dalton_conversation_cards'::regclass
                  AND a.attname = 'embedding'
                  AND NOT a.attisdropped;

                SELECT count(*) INTO embedded_count
                FROM dalton_conversation_cards
                WHERE embedding IS NOT NULL;

                IF embedded_count > 0 AND (
                    (col_type IS NOT NULL AND col_type <> 'vector(3072)')
                    OR NOT has_locked_row
                    OR locked_dims IS DISTINCT FROM 3072
                    OR locked_provider IS DISTINCT FROM 'gemini'
                    OR locked_model_label IS NULL
                    OR locked_model_label NOT IN ('gemini-embedding-2-preview', 'gemini-embedding-2')
                    OR locked_api_model IS DISTINCT FROM 'gemini-embedding-2'
                    OR locked_task_doc IS DISTINCT FROM 'RETRIEVAL_DOCUMENT'
                    OR locked_task_query IS DISTINCT FROM 'RETRIEVAL_QUERY'
                ) THEN
                    RAISE EXCEPTION
                        'Refusing in-place downgrade of d052: dalton_conversation_cards already contains % embedded rows whose locked embedding space is incompatible with legacy 3072-d Gemini retrieval (column_type=%, provider=%, model_label=%, api_model_name=%, locked_dims=%, task_type_doc=%, task_type_query=%). Restore the pre-upgrade database backup before rolling back.',
                        embedded_count,
                        COALESCE(col_type, 'unknown'),
                        COALESCE(locked_provider, 'none'),
                        COALESCE(locked_model_label, 'none'),
                        COALESCE(locked_api_model, 'none'),
                        COALESCE(locked_dims::text, 'none'),
                        COALESCE(locked_task_doc, 'none'),
                        COALESCE(locked_task_query, 'none');
                END IF;

                IF embedded_count = 0 AND col_type IS NOT NULL AND col_type <> 'vector(3072)' THEN
                    DROP INDEX IF EXISTS ix_dalton_conv_cards_embedding_hnsw;
                    ALTER TABLE dalton_conversation_cards
                        ALTER COLUMN embedding TYPE vector(3072);
                    CREATE INDEX IF NOT EXISTS ix_dalton_conv_cards_embedding_hnsw
                        ON dalton_conversation_cards
                        USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops);
                END IF;
            END IF;
        END
        $$;
        """
    )
    op.execute("DROP TABLE IF EXISTS dalton_embedding_metadata")
