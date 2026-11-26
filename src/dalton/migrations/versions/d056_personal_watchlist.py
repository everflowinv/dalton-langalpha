"""Dalton d056: personal watchlist three-tier schema, tier policies, reconcile outbox, views, and custom columns.

Extends ``dalton_coverage`` and ``dalton_coverage_groups`` to support the three
personal tiers (``core`` / ``watch`` / ``radar``), adds first-class watchlist
columns (``tags``, ``sort_order``, ``is_pinned``, ``is_private``,
``task_overrides``) with backfills for unlisted companies and legacy ``extra``
tags, and creates ``dalton_watchlist_tier_policies``,
``dalton_watchlist_reconcile_jobs``, ``dalton_watchlist_views``, and
``dalton_watchlist_custom_columns``.

Existing ``core`` and ``watch`` rows are preserved untouched. ``downgrade()``
preserves ``radar`` rows in ``extra`` (``downgraded_from_tier='radar'``) before
reverting them to ``watch`` and restoring the two-tier ``CHECK`` constraints.

Revision ID: d056
Revises: d055
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d056"
down_revision: str | None = "d055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # 1. Extend dalton_coverage and dalton_coverage_groups CHECK constraints to 3 tiers
    op.execute(
        """
        ALTER TABLE dalton_coverage
            DROP CONSTRAINT IF EXISTS dalton_coverage_tier_check,
            ADD CONSTRAINT dalton_coverage_tier_check
                CHECK (tier IN ('core', 'watch', 'radar'))
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_coverage_groups
            DROP CONSTRAINT IF EXISTS dalton_coverage_groups_default_tier_check,
            ADD CONSTRAINT dalton_coverage_groups_default_tier_check
                CHECK (default_tier IN ('core', 'watch', 'radar'))
        """
    )

    # 2. Add dedicated personal watchlist columns to dalton_coverage
    op.execute(
        """
        ALTER TABLE dalton_coverage
            ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS is_pinned BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS is_private BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS task_overrides JSONB
        """
    )

    # Backfill unlisted companies (e.g. SPCX, OPENAI, ANTHROPIC) and legacy extra.tags
    op.execute(
        """
        UPDATE dalton_coverage
        SET is_private = TRUE
        WHERE ticker IN ('SPCX', 'OPENAI', 'ANTHROPIC')
           OR UPPER(COALESCE(market, '')) = 'PRIVATE'
           OR COALESCE(extra->>'is_private', '') = 'true'
        """
    )
    op.execute(
        """
        UPDATE dalton_coverage
        SET tags = extra->'tags'
        WHERE jsonb_typeof(extra->'tags') = 'array' AND tags = '[]'::jsonb
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_coverage_user_tier_order
            ON dalton_coverage (user_id, is_pinned DESC, tier, sort_order, ticker)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_coverage_tags_gin
            ON dalton_coverage USING GIN (tags)
        """
    )

    # 3. Per-user tier monitoring policies
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_watchlist_tier_policies (
            user_id TEXT NOT NULL,
            tier TEXT NOT NULL CHECK (tier IN ('core', 'watch', 'radar')),
            watch_enabled BOOLEAN NOT NULL,
            watch_cron VARCHAR(64) NOT NULL,
            delta_enabled BOOLEAN NOT NULL,
            delta_cron VARCHAR(64) NOT NULL,
            delta_timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
            move_enabled BOOLEAN NOT NULL,
            move_threshold_pct NUMERIC(5, 2) NOT NULL DEFAULT 4.00
                CHECK (move_threshold_pct >= 1.00 AND move_threshold_pct <= 30.00),
            earnings_stages JSONB NOT NULL DEFAULT '[]'::jsonb,
            policy_version INTEGER NOT NULL DEFAULT 1,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, tier)
        )
        """
    )

    # 4. Transactional outbox for watchlist automation reconciliation
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_watchlist_reconcile_jobs (
            job_id UUID PRIMARY KEY,
            user_id TEXT NOT NULL,
            trigger_action VARCHAR(32) NOT NULL,
            target_policy_versions JSONB NOT NULL DEFAULT '{}'::jsonb,
            pending_tickers JSONB NOT NULL DEFAULT '[]'::jsonb,
            completed_tickers JSONB NOT NULL DEFAULT '[]'::jsonb,
            failed_items JSONB NOT NULL DEFAULT '[]'::jsonb,
            override_user_pause BOOLEAN NOT NULL DEFAULT FALSE,
            yaml_synced BOOLEAN NOT NULL DEFAULT FALSE,
            status VARCHAR(24) NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'completed', 'partial')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_watchlist_reconcile_pending
            ON dalton_watchlist_reconcile_jobs (user_id, created_at ASC)
            WHERE status IN ('pending', 'running', 'partial') OR yaml_synced = FALSE
        """
    )

    # 5. Per-user column view presets
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_watchlist_views (
            view_id UUID PRIMARY KEY,
            user_id TEXT NOT NULL,
            name VARCHAR(80) NOT NULL DEFAULT '默认视图',
            is_default BOOLEAN NOT NULL DEFAULT TRUE,
            columns JSONB NOT NULL,
            view_state JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, name)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_watchlist_views_user
            ON dalton_watchlist_views (user_id, is_default DESC, created_at ASC)
        """
    )

    # 6. Per-user strongly-typed AST custom metric columns (activated in Batch 3)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_watchlist_custom_columns (
            column_id UUID PRIMARY KEY,
            user_id TEXT NOT NULL,
            slug VARCHAR(64) NOT NULL,
            label VARCHAR(64) NOT NULL,
            description TEXT,
            format_type VARCHAR(24) NOT NULL DEFAULT 'number',
            decimals SMALLINT NOT NULL DEFAULT 2,
            formula_spec JSONB NOT NULL,
            spec_hash VARCHAR(64) NOT NULL,
            is_pure_market_spec BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, slug)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_watchlist_custom_cols_user
            ON dalton_watchlist_custom_columns (user_id, created_at ASC)
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # Preserve radar rows in extra before reverting CHECK constraints to 2 tiers
    op.execute(
        """
        UPDATE dalton_coverage
        SET extra = COALESCE(extra, '{}'::jsonb)
                    || jsonb_build_object(
                        'downgraded_from_tier', 'radar',
                        'downgraded_silent', true
                    ),
            tier = 'watch',
            updated_at = NOW()
        WHERE tier = 'radar'
        """
    )
    op.execute(
        """
        UPDATE dalton_coverage_groups
        SET default_tier = 'watch'
        WHERE default_tier = 'radar'
        """
    )

    # Drop new tables & indexes
    op.execute("DROP INDEX IF EXISTS idx_dalton_watchlist_custom_cols_user")
    op.execute("DROP TABLE IF EXISTS dalton_watchlist_custom_columns")
    op.execute("DROP INDEX IF EXISTS idx_dalton_watchlist_views_user")
    op.execute("DROP TABLE IF EXISTS dalton_watchlist_views")
    op.execute("DROP INDEX IF EXISTS idx_dalton_watchlist_reconcile_pending")
    op.execute("DROP TABLE IF EXISTS dalton_watchlist_reconcile_jobs")
    op.execute("DROP TABLE IF EXISTS dalton_watchlist_tier_policies")

    # Drop new indexes and columns from dalton_coverage
    op.execute("DROP INDEX IF EXISTS idx_dalton_coverage_tags_gin")
    op.execute("DROP INDEX IF EXISTS idx_dalton_coverage_user_tier_order")
    op.execute(
        """
        ALTER TABLE dalton_coverage
            DROP COLUMN IF EXISTS task_overrides,
            DROP COLUMN IF EXISTS is_private,
            DROP COLUMN IF EXISTS is_pinned,
            DROP COLUMN IF EXISTS sort_order,
            DROP COLUMN IF EXISTS tags
        """
    )

    # Restore 2-tier CHECK constraints
    op.execute(
        """
        ALTER TABLE dalton_coverage_groups
            DROP CONSTRAINT IF EXISTS dalton_coverage_groups_default_tier_check,
            ADD CONSTRAINT dalton_coverage_groups_default_tier_check
                CHECK (default_tier IN ('core', 'watch'))
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_coverage
            DROP CONSTRAINT IF EXISTS dalton_coverage_tier_check,
            ADD CONSTRAINT dalton_coverage_tier_check
                CHECK (tier IN ('core', 'watch'))
        """
    )
