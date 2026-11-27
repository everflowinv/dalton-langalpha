"""Dalton d06X: personal calendar & mail integrations foundation tables, sequence, and thread purge columns.

Creates:
- Sequence ``dalton_integration_auth_epoch_seq``
- ``dalton_user_integrations``
- ``dalton_user_integration_oauth_states``
- ``dalton_user_integration_pending_confirmations`` (5-minute Supabase post-callback confirmation staging)
- ``dalton_integration_action_approvals``
- ``dalton_thread_integration_touches``
- ``dalton_integration_purge_ledger``
- ``dalton_integration_webhook_events``
- ``conversation_threads`` columns:
  ``has_personal_integration_data``, ``personal_purge_in_progress``,
  ``personal_purge_generation``, ``personal_purged_turn_ranges``

Revision ID: d06X
Revises: d06W
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d06X"
down_revision: str | None = "d06W"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # Monotonic global authorization epoch sequence
    op.execute(
        """
        CREATE SEQUENCE IF NOT EXISTS dalton_integration_auth_epoch_seq
            AS BIGINT
            START WITH 1
            INCREMENT BY 1
            NO CYCLE
        """
    )

    # 1. Personal external integration connections
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_user_integrations (
            user_id             TEXT NOT NULL,
            provider            TEXT NOT NULL CHECK (provider IN ('google', 'feishu')),
            service             TEXT NOT NULL CHECK (service IN ('calendar', 'mail')),
            oauth_client_id     TEXT NOT NULL,
            provider_account_id TEXT NOT NULL,
            provider_union_id   TEXT NULL,
            provider_email      TEXT NULL,
            granted_scopes      TEXT NOT NULL,
            access_token        BYTEA NULL,
            refresh_token       BYTEA NULL,
            auth_epoch          BIGINT NOT NULL DEFAULT nextval('dalton_integration_auth_epoch_seq'),
            authorized_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            token_generation    BIGINT NOT NULL DEFAULT 1,
            status              TEXT NOT NULL DEFAULT 'connected' CHECK (
                status IN (
                    'connected',
                    'needs_reauth',
                    'refresh_ambiguous',
                    'disconnect_pending',
                    'revoked_purging'
                )
            ),
            refresh_inflight_gen BIGINT NULL,
            refresh_started_at  TIMESTAMPTZ NULL,
            expires_at          TIMESTAMPTZ NULL,
            last_refresh_at     TIMESTAMPTZ NULL,
            needs_reauth_at     TIMESTAMPTZ NULL,
            last_error_code     TEXT NULL,
            reauth_notified_at  TIMESTAMPTZ NULL,
            preferences         JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, provider, service)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_user_integrations_account
            ON dalton_user_integrations (user_id, provider, oauth_client_id, provider_account_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_user_integrations_provider_account
            ON dalton_user_integrations (provider, oauth_client_id, provider_account_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_user_integrations_status
            ON dalton_user_integrations (status)
        """
    )

    # 2. One-time server-side OAuth state table (10-minute TTL)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_user_integration_oauth_states (
            state_hash       TEXT PRIMARY KEY,
            user_id          TEXT NOT NULL,
            auth_mode        TEXT NOT NULL CHECK (auth_mode IN ('local', 'supabase', 'oss')),
            session_id       TEXT NULL,
            user_email       TEXT NULL,
            provider         TEXT NOT NULL CHECK (provider IN ('google', 'feishu')),
            service          TEXT NOT NULL CHECK (service IN ('calendar', 'mail')),
            oauth_client_id  TEXT NOT NULL,
            intent           TEXT NOT NULL DEFAULT 'connect' CHECK (
                intent IN ('connect', 'switch_account', 'reauth')
            ),
            code_verifier    BYTEA NOT NULL,
            requested_scopes TEXT NOT NULL,
            redirect_uri     TEXT NOT NULL,
            return_to        TEXT NULL,
            browser_binding_hash TEXT NULL,
            expires_at       TIMESTAMPTZ NOT NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_user_integration_oauth_states_expires
            ON dalton_user_integration_oauth_states (expires_at)
        """
    )

    # 2b. Supabase 5-minute post-callback confirmation staging table
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_user_integration_pending_confirmations (
            confirmation_hash   TEXT PRIMARY KEY,
            user_id             TEXT NOT NULL,
            provider            TEXT NOT NULL CHECK (provider IN ('google', 'feishu')),
            service             TEXT NOT NULL CHECK (service IN ('calendar', 'mail')),
            oauth_client_id     TEXT NOT NULL,
            provider_account_id TEXT NOT NULL,
            provider_union_id   TEXT NULL,
            provider_email      TEXT NULL,
            granted_scopes      TEXT NOT NULL,
            access_token        BYTEA NULL,
            refresh_token       BYTEA NULL,
            expires_at          TIMESTAMPTZ NULL,
            intent              TEXT NOT NULL DEFAULT 'connect' CHECK (
                intent IN ('connect', 'switch_account', 'reauth')
            ),
            expected_old_oauth_client_id     TEXT NULL,
            expected_old_provider_account_id TEXT NULL,
            expected_old_auth_epoch          BIGINT NULL,
            preferences         JSONB NOT NULL DEFAULT '{}'::jsonb,
            confirm_expires_at  TIMESTAMPTZ NOT NULL,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_user_integration_pending_confirmations_user_expires
            ON dalton_user_integration_pending_confirmations (user_id, confirm_expires_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_user_integration_pending_confirmations_expires
            ON dalton_user_integration_pending_confirmations (confirm_expires_at)
        """
    )

    # 3. Write action & Google MCP elicitation one-time approvals
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_integration_action_approvals (
            approval_id                 UUID PRIMARY KEY,
            caller_user_id              TEXT NOT NULL,
            thread_id                   UUID NOT NULL,
            run_id                      UUID NULL,
            interrupt_id                TEXT NULL,
            action_type                 TEXT NOT NULL,
            provider                    TEXT NOT NULL CHECK (provider IN ('google', 'feishu')),
            service                     TEXT NOT NULL CHECK (service IN ('calendar', 'mail')),
            provider_account_id         TEXT NOT NULL,
            auth_epoch                  BIGINT NOT NULL,
            token_generation            BIGINT NOT NULL DEFAULT 1,
            payload_sha256              TEXT NOT NULL,
            input_requests_map          JSONB NULL,
            encrypted_elicitation_state BYTEA NULL,
            idempotent_message_id       TEXT NULL,
            status                      TEXT NOT NULL DEFAULT 'pending' CHECK (
                status IN (
                    'pending',
                    'executing',
                    'succeeded',
                    'rejected',
                    'send_ambiguous',
                    'failed',
                    'expired'
                )
            ),
            expires_at                  TIMESTAMPTZ NOT NULL,
            consumed_at                 TIMESTAMPTZ NULL,
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_integration_approvals_user_service
            ON dalton_integration_action_approvals (caller_user_id, provider, service)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_integration_approvals_thread_run
            ON dalton_integration_action_approvals (thread_id, run_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_integration_approvals_expires
            ON dalton_integration_action_approvals (expires_at)
        """
    )

    # 4. Private thread touch tracking
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_thread_integration_touches (
            thread_id           UUID NOT NULL REFERENCES conversation_threads(conversation_thread_id) ON DELETE CASCADE,
            response_id         UUID NOT NULL,
            provider            TEXT NOT NULL CHECK (provider IN ('google', 'feishu')),
            service             TEXT NOT NULL CHECK (service IN ('calendar', 'mail')),
            auth_epoch          BIGINT NOT NULL,
            turn_index          INTEGER NOT NULL,
            user_id             TEXT NOT NULL,
            provider_account_id TEXT NOT NULL,
            touched_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            purged_at           TIMESTAMPTZ NULL,
            PRIMARY KEY (thread_id, response_id, provider, service, auth_epoch)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_thread_integration_touches_active_user
            ON dalton_thread_integration_touches (user_id, provider, service)
            WHERE purged_at IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_thread_integration_touches_active_thread
            ON dalton_thread_integration_touches (thread_id)
            WHERE purged_at IS NULL
        """
    )

    # 5. Dual-track local 2A purge & remote revocation ledger
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_integration_purge_ledger (
            purge_id                  UUID PRIMARY KEY,
            user_id                   TEXT NOT NULL,
            provider                  TEXT NOT NULL CHECK (provider IN ('google', 'feishu')),
            service                   TEXT NULL CHECK (service IS NULL OR service IN ('calendar', 'mail')),
            oauth_client_id           TEXT NOT NULL,
            provider_account_id       TEXT NOT NULL,
            auth_epoch_upto           BIGINT NOT NULL,
            trigger_reason            TEXT NOT NULL,
            local_purge_status        TEXT NOT NULL CHECK (
                local_purge_status IN ('running', 'completed', 'not_required')
            ),
            purged_max_turn_by_thread JSONB NOT NULL DEFAULT '{}'::jsonb,
            tracker_reconcile_needed  BOOLEAN NOT NULL DEFAULT FALSE,
            local_completed_at        TIMESTAMPTZ NULL,
            remote_revoke_status      TEXT NOT NULL CHECK (
                remote_revoke_status IN (
                    'pending',
                    'succeeded',
                    'failed_retryable',
                    'superseded',
                    'not_applicable'
                )
            ),
            encrypted_revoke_token    BYTEA NULL,
            retry_count               INTEGER NOT NULL DEFAULT 0,
            next_retry_at             TIMESTAMPTZ NULL,
            last_remote_error         TEXT NULL,
            remote_completed_at       TIMESTAMPTZ NULL,
            disconnected_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at              TIMESTAMPTZ NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_integration_purge_ledger_local_running
            ON dalton_integration_purge_ledger (local_purge_status)
            WHERE local_purge_status = 'running'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_integration_purge_ledger_remote_retry
            ON dalton_integration_purge_ledger (remote_revoke_status, next_retry_at)
            WHERE remote_revoke_status IN ('pending', 'failed_retryable')
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_integration_purge_ledger_user_account
            ON dalton_integration_purge_ledger (user_id, provider, provider_account_id)
        """
    )

    # 6. External webhook event deduplication
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_integration_webhook_events (
            provider    TEXT NOT NULL CHECK (provider IN ('google', 'feishu')),
            event_id    TEXT NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (provider, event_id)
        )
        """
    )

    # 7. conversation_threads personal integration & purge columns
    op.execute(
        """
        ALTER TABLE conversation_threads
            ADD COLUMN IF NOT EXISTS has_personal_integration_data BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS personal_purge_in_progress    BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS personal_purge_generation     BIGINT  NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS personal_purged_turn_ranges   JSONB   NOT NULL DEFAULT '[]'::jsonb
        """
    )

    # 8. Singleton multi-worker 12-hour health sweep coordination row
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_integration_health_sweep_state (
            singleton         BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
            last_started_at   TIMESTAMPTZ NULL,
            last_completed_at TIMESTAMPTZ NULL,
            last_worker_id    TEXT NULL,
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        INSERT INTO dalton_integration_health_sweep_state (singleton)
        VALUES (TRUE)
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.execute("DROP TABLE IF EXISTS dalton_integration_health_sweep_state")

    op.execute(
        """
        ALTER TABLE conversation_threads
            DROP COLUMN IF EXISTS personal_purged_turn_ranges,
            DROP COLUMN IF EXISTS personal_purge_generation,
            DROP COLUMN IF EXISTS personal_purge_in_progress,
            DROP COLUMN IF EXISTS has_personal_integration_data
        """
    )

    op.execute("DROP TABLE IF EXISTS dalton_integration_webhook_events")

    op.execute("DROP INDEX IF EXISTS ix_dalton_integration_purge_ledger_user_account")
    op.execute("DROP INDEX IF EXISTS ix_dalton_integration_purge_ledger_remote_retry")
    op.execute("DROP INDEX IF EXISTS ix_dalton_integration_purge_ledger_local_running")
    op.execute("DROP TABLE IF EXISTS dalton_integration_purge_ledger")

    op.execute("DROP INDEX IF EXISTS ix_dalton_thread_integration_touches_active_thread")
    op.execute("DROP INDEX IF EXISTS ix_dalton_thread_integration_touches_active_user")
    op.execute("DROP TABLE IF EXISTS dalton_thread_integration_touches")

    op.execute("DROP INDEX IF EXISTS ix_dalton_integration_approvals_expires")
    op.execute("DROP INDEX IF EXISTS ix_dalton_integration_approvals_thread_run")
    op.execute("DROP INDEX IF EXISTS ix_dalton_integration_approvals_user_service")
    op.execute("DROP TABLE IF EXISTS dalton_integration_action_approvals")

    op.execute("DROP INDEX IF EXISTS ix_dalton_user_integration_pending_confirmations_expires")
    op.execute("DROP INDEX IF EXISTS ix_dalton_user_integration_pending_confirmations_user_expires")
    op.execute("DROP TABLE IF EXISTS dalton_user_integration_pending_confirmations")

    op.execute("DROP INDEX IF EXISTS ix_dalton_user_integration_oauth_states_expires")
    op.execute("DROP TABLE IF EXISTS dalton_user_integration_oauth_states")

    op.execute("DROP INDEX IF EXISTS ix_dalton_user_integrations_status")
    op.execute("DROP INDEX IF EXISTS ix_dalton_user_integrations_provider_account")
    op.execute("DROP INDEX IF EXISTS ix_dalton_user_integrations_account")
    op.execute("DROP TABLE IF EXISTS dalton_user_integrations")

    op.execute("DROP SEQUENCE IF EXISTS dalton_integration_auth_epoch_seq")
