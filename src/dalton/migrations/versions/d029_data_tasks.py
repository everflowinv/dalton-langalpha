"""Dalton d029: OC-1 deterministic data task definitions, single-slot lock, and run ledger.

Revision ID: d029
Revises: d028
Create Date: 2026-11-07
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d029"
down_revision: Union[str, None] = "d028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DATA_TASK_RUNNERS: tuple[str, ...] = ("container", "host")
DATA_TASK_COS_MODES: tuple[str, ...] = ("skip", "prod")
DATA_TASK_SLOT_STATES: tuple[str, ...] = ("idle", "running", "reclaiming")
DATA_TASK_TRIGGER_SOURCES: tuple[str, ...] = ("cron", "manual")
DATA_TASK_RUN_STATUSES: tuple[str, ...] = (
    "queued",
    "starting",
    "running",
    "succeeded",
    "failed",
    "timed_out",
    "aborted",
    "skipped_low_memory",
    "skipped_superseded",
    "cancelled",
)
DATA_TASK_NOTIFY_STATUSES: tuple[str, ...] = (
    "preview_only",
    "pending_dispatch",
    "dispatched",
    "skipped",
    "failed",
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_data_task_defs (
            job_key                     TEXT PRIMARY KEY,
            task_code                   TEXT NOT NULL,
            title                       TEXT NOT NULL,
            batch_no                    INTEGER NOT NULL CHECK (batch_no BETWEEN 1 AND 4),
            runner                      TEXT NOT NULL DEFAULT 'container'
                CHECK (runner IN ('container', 'host')),
            cron_expr                   TEXT NOT NULL,
            timezone                    TEXT NOT NULL,
            timeout_seconds             INTEGER,
            no_output_timeout_seconds   INTEGER,
            command_argv                JSONB NOT NULL,
            workdir                     TEXT NOT NULL DEFAULT '/Users/everflow/.openclaw/workspace',
            required_secrets            JSONB NOT NULL DEFAULT '[]'::jsonb,
            cos_mode                    TEXT NOT NULL DEFAULT 'skip'
                CHECK (cos_mode IN ('skip', 'prod')),
            notify_enabled              BOOLEAN NOT NULL DEFAULT FALSE,
            enabled                     BOOLEAN NOT NULL DEFAULT FALSE,
            paused_by_user              BOOLEAN NOT NULL DEFAULT FALSE,
            consecutive_failures        INTEGER NOT NULL DEFAULT 0,
            config_version              INTEGER NOT NULL DEFAULT 1,
            active_version              INTEGER NOT NULL DEFAULT 1,
            updated_by                  TEXT,
            audit_history               JSONB NOT NULL DEFAULT '[]'::jsonb,
            updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_data_task_slot (
            slot_id                     SMALLINT PRIMARY KEY DEFAULT 1 CHECK (slot_id = 1),
            generation                  BIGINT NOT NULL DEFAULT 0,
            slot_state                  TEXT NOT NULL DEFAULT 'idle'
                CHECK (slot_state IN ('idle', 'running', 'reclaiming')),
            active_run_id               UUID,
            active_job_key              TEXT,
            started_at                  TIMESTAMPTZ,
            updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute(
        """
        INSERT INTO dalton_data_task_slot (slot_id, generation, slot_state)
        VALUES (1, 0, 'idle')
        ON CONFLICT DO NOTHING;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_data_task_runs (
            run_id                      UUID PRIMARY KEY,
            job_key                     TEXT NOT NULL REFERENCES dalton_data_task_defs(job_key),
            trigger_source              TEXT NOT NULL
                CHECK (trigger_source IN ('cron', 'manual')),
            triggered_by_user_id        TEXT,
            scheduled_slot_utc          TIMESTAMPTZ NOT NULL,
            deadline_utc                TIMESTAMPTZ NOT NULL,
            low_mem_since_utc           TIMESTAMPTZ,
            status                      TEXT NOT NULL
                CHECK (status IN (
                    'queued',
                    'starting',
                    'running',
                    'succeeded',
                    'failed',
                    'timed_out',
                    'aborted',
                    'skipped_low_memory',
                    'skipped_superseded',
                    'cancelled'
                )),
            generation                  BIGINT,
            container_id                TEXT,
            container_name              TEXT,
            started_at                  TIMESTAMPTZ,
            completed_at                TIMESTAMPTZ,
            exit_code                   INTEGER,
            oom_killed                  BOOLEAN,
            failure_reason              TEXT,
            log_tail                    TEXT,
            outbox_payload              JSONB,
            notify_preview              JSONB,
            notify_status               TEXT
                CHECK (notify_status IS NULL OR notify_status IN (
                    'preview_only',
                    'pending_dispatch',
                    'dispatched',
                    'skipped',
                    'failed'
                )),
            frozen_snapshot_dir         TEXT,
            frozen_manifest             JSONB,
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_dalton_data_task_runs_cron_slot
            ON dalton_data_task_runs (job_key, scheduled_slot_utc)
            WHERE trigger_source = 'cron';
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_dalton_data_task_runs_one_queued_per_job
            ON dalton_data_task_runs (job_key)
            WHERE status = 'queued';
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS uq_dalton_data_task_runs_one_queued_per_job;")
    op.execute("DROP INDEX IF EXISTS uq_dalton_data_task_runs_cron_slot;")
    op.execute("DROP TABLE IF EXISTS dalton_data_task_runs;")
    op.execute("DROP TABLE IF EXISTS dalton_data_task_slot;")
    op.execute("DROP TABLE IF EXISTS dalton_data_task_defs;")
