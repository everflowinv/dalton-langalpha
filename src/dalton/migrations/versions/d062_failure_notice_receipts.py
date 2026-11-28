"""Dalton d062: durable failure notice receipt ledger, streak epochs, and leased targets.

Operates exclusively on Dalton-owned ``dalton_*`` tables (per standalone-core.md):
- ``dalton_failure_notices``: one row per terminal failure event across
  ``personal_automation``, ``company_automation``, ``data_task``, and ``wakeup``,
  guarded by partial unique index ``uq_dalton_failure_notices_dedup_key`` for atomic
  multi-worker anti-spam deduplication.
- ``dalton_failure_notice_streaks``: per-(subject_kind, source_key) ``recovery_epoch``
  bumped inside the terminal success transaction so post-recovery failures alert again.
- ``dalton_failure_notice_targets``: per-target delivery receipt with ``claim_token``
  and ``claim_expires_at`` lease fence to block late gateway claims.

Note on revision numbering:
- Anchored on ``down_revision = "d061"`` (the revision ID declared by
  ``d061_monitor_publish_verify.py``).

Revision ID: d062
Revises: d061
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d062"
down_revision: str | None = "d061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FAILURE_NOTICE_SUBJECT_KINDS: tuple[str, ...] = (
    "personal_automation",
    "company_automation",
    "data_task",
    "wakeup",
)

FAILURE_NOTICE_AGGREGATE_STATUSES: tuple[str, ...] = (
    "not_applicable",
    "unconfigured",
    "suppressed",
    "retrying",
    "delivered",
    "partial_failed",
    "failed",
)

FAILURE_NOTICE_TARGET_STATUSES: tuple[str, ...] = (
    "pending",
    "sending",
    "uncertain",
    "retryable_failed",
    "sent",
    "terminal_failed",
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # 1. dalton_failure_notices: one durable notice record per failed event
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_failure_notices (
            notice_id          UUID PRIMARY KEY,
            subject_kind       TEXT NOT NULL
                CHECK (subject_kind IN (
                    'personal_automation',
                    'company_automation',
                    'data_task',
                    'wakeup'
                )),
            subject_id         TEXT NOT NULL,
            source_key         TEXT NOT NULL,
            recipient_user_id  TEXT NOT NULL,
            failure_reason     TEXT NOT NULL,
            dedup_window_key   TEXT,
            aggregate_status   TEXT NOT NULL
                CHECK (aggregate_status IN (
                    'not_applicable',
                    'unconfigured',
                    'suppressed',
                    'retrying',
                    'delivered',
                    'partial_failed',
                    'failed'
                )),
            summary_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (subject_kind, subject_id)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_dalton_failure_notices_dedup_key
            ON dalton_failure_notices (subject_kind, source_key, dedup_window_key)
            WHERE dedup_window_key IS NOT NULL
              AND aggregate_status NOT IN ('failed', 'partial_failed')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_failure_notices_subject_lookup
            ON dalton_failure_notices (subject_id, subject_kind)
        """
    )

    # 2. dalton_failure_notice_streaks: monotonic recovery epoch per (subject_kind, source_key)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_failure_notice_streaks (
            subject_kind   TEXT NOT NULL
                CHECK (subject_kind IN (
                    'personal_automation',
                    'company_automation',
                    'data_task',
                    'wakeup'
                )),
            source_key     TEXT NOT NULL,
            recovery_epoch BIGINT NOT NULL DEFAULT 0
                CHECK (recovery_epoch >= 0),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (subject_kind, source_key)
        )
        """
    )

    # 3. dalton_failure_notice_targets: per-target leased delivery state machine
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_failure_notice_targets (
            target_receipt_id    UUID PRIMARY KEY,
            notice_id            UUID NOT NULL
                REFERENCES dalton_failure_notices(notice_id) ON DELETE CASCADE,
            channel              TEXT NOT NULL,
            target               TEXT NOT NULL,
            status               TEXT NOT NULL
                CHECK (status IN (
                    'pending',
                    'sending',
                    'uncertain',
                    'retryable_failed',
                    'sent',
                    'terminal_failed'
                )),
            attempt_count        INTEGER NOT NULL DEFAULT 0
                CHECK (attempt_count >= 0),
            max_attempts         INTEGER NOT NULL DEFAULT 3
                CHECK (max_attempts >= 1),
            claim_token          UUID,
            claim_expires_at     TIMESTAMPTZ,
            next_retry_at        TIMESTAMPTZ,
            uncertain_since      TIMESTAMPTZ,
            error_code           TEXT,
            error_message        TEXT,
            platform_message_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            payload_json         JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (notice_id, channel, target)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_failure_notice_targets_work
            ON dalton_failure_notice_targets (
                status,
                next_retry_at,
                claim_expires_at,
                uncertain_since
            )
            WHERE status IN ('pending', 'sending', 'uncertain', 'retryable_failed')
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS idx_dalton_failure_notice_targets_work")
    op.execute("DROP TABLE IF EXISTS dalton_failure_notice_targets")
    op.execute("DROP TABLE IF EXISTS dalton_failure_notice_streaks")
    op.execute("DROP INDEX IF EXISTS idx_dalton_failure_notices_subject_lookup")
    op.execute("DROP INDEX IF EXISTS uq_dalton_failure_notices_dedup_key")
    op.execute("DROP TABLE IF EXISTS dalton_failure_notices")
