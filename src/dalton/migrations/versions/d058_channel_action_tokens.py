"""Dalton d058: channel interactive action tokens, interrupt runtime config barrier,
interrupt resume contention table, and BT2 deliverable link recovery columns/index.

Creates:
1. ``dalton_channel_interrupt_runtime_config`` (singleton_id = 1) — cross-worker
   row-lock barrier (Level 7b-0) separating button toggle from three-switch drain.
2. ``dalton_channel_action_tokens`` — opaque action tokens, reconcilable card-send
   records (8-state ``bind_status``), and persistent late terminal card patch tasks
   (1 full composite index + 3 partial indexes).
3. ``dalton_channel_interrupt_resumes`` — unified three-terminal HITL resume
   contention & settlement ledger keyed by ``(thread_id, run_id, interrupt_id)``
   with per-claim ``claim_id``, ``requires_gateway_ack``, ``delivery_settled``,
   settlement ``CHECK`` constraint, ``NOT VALID`` Weixin account FK, and 5 partial
   indexes.
4. Purely additive columns on ``dalton_channel_deliveries`` (7 columns for BT2
   manifest, authorized target snapshot, enrollment flag, and per-attempt lease)
   plus 1 partial recovery index (``idx_dalton_channel_deliveries_links_recovery``).

Total: 3 new tables + 7 additive columns on ``dalton_channel_deliveries`` + 10 indexes
(1 composite + 9 partial).

Revision ID: d058
Revises: d055
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d058"
down_revision: str | None = "d055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # 1. Single-row runtime barrier table (Level 7b-0)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_interrupt_runtime_config (
            singleton_id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (singleton_id = 1),
            interactive_actions_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            interrupt_ledger_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            bt2_link_recovery_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            config_revision BIGINT NOT NULL DEFAULT 1,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (interrupt_ledger_enabled = TRUE OR interactive_actions_enabled = FALSE)
        )
        """
    )
    op.execute(
        """
        INSERT INTO dalton_channel_interrupt_runtime_config (singleton_id)
        VALUES (1)
        ON CONFLICT (singleton_id) DO NOTHING
        """
    )

    # 2. Channel interactive action tokens, reconcilable card sends, and late patch tasks
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_action_tokens (
            token_hash VARCHAR(64) PRIMARY KEY,
            token_role VARCHAR(32) NOT NULL DEFAULT 'direct' CHECK (
                token_role IN ('direct', 'gate', 'ephemeral_confirm')
            ),
            parent_token_hash VARCHAR(64) NULL,
            platform VARCHAR(32) NOT NULL CHECK (platform IN ('feishu', 'discord')),
            dalton_user_id VARCHAR(255) NOT NULL,
            initiator_platform_user_id VARCHAR(255) NOT NULL,
            origin_chat_type VARCHAR(16) NOT NULL CHECK (origin_chat_type IN ('dm', 'group')),
            origin_chat_address VARCHAR(255) NOT NULL,
            origin_scope_key VARCHAR(255) NOT NULL,
            session_generation INTEGER NOT NULL,
            card_chat_type VARCHAR(16) NOT NULL CHECK (card_chat_type IN ('dm', 'group')),
            card_delivery_address VARCHAR(255) NOT NULL,
            card_channel_id VARCHAR(255) NULL,
            card_message_id VARCHAR(255) NULL,
            bind_status VARCHAR(32) NOT NULL DEFAULT 'pending_bind' CHECK (
                bind_status IN (
                    'pending_bind',
                    'send_in_flight',
                    'bound',
                    'ephemeral',
                    'send_failed',
                    'bind_expired',
                    'send_unknown',
                    'skipped_platform_disabled'
                )
            ),
            send_attempt_started_at TIMESTAMPTZ NULL,
            send_gateway_epoch BIGINT NULL,
            send_reconcile_meta JSONB NULL,
            late_patch_status VARCHAR(32) NULL CHECK (
                late_patch_status IS NULL
                OR late_patch_status IN (
                    'pending',
                    'patched',
                    'deleted',
                    'uneditable_expired',
                    'failed_exhausted',
                    'skipped_platform_disabled'
                )
            ),
            late_patch_attempts INTEGER NOT NULL DEFAULT 0,
            late_patch_next_retry_at TIMESTAMPTZ NULL,
            late_patch_last_error TEXT NULL,
            gate_verified_at TIMESTAMPTZ NULL,
            thread_id UUID NOT NULL,
            run_id VARCHAR(255) NOT NULL,
            interrupt_id VARCHAR(255) NOT NULL,
            risk_tier VARCHAR(8) NOT NULL CHECK (risk_tier IN ('L0', 'L1', 'L2')),
            action_type VARCHAR(128) NOT NULL,
            args_sha256 VARCHAR(64) NOT NULL,
            server_confirm_token VARCHAR(128) NULL,
            options_snapshot JSONB NULL,
            prompt_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
            expires_at TIMESTAMPTZ NOT NULL,
            consumed_at TIMESTAMPTZ NULL,
            consumed_via VARCHAR(32) NULL CHECK (
                consumed_via IS NULL
                OR consumed_via IN ('channel', 'web', 'text_reply', 'superseded', 'expired')
            ),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (
                (token_role = 'ephemeral_confirm' AND bind_status = 'ephemeral' AND late_patch_status IS NULL)
                OR (token_role IN ('direct', 'gate') AND bind_status <> 'ephemeral')
            ),
            CHECK (
                bind_status <> 'send_in_flight'
                OR send_attempt_started_at IS NOT NULL
            ),
            CHECK (
                late_patch_status IS NULL
                OR (bind_status = 'bound' AND card_message_id IS NOT NULL)
            )
        )
        """
    )

    # Index 1 (full composite): group & lock tokens per pending interrupt
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_channel_action_tokens_interrupt
            ON dalton_channel_action_tokens (thread_id, run_id, interrupt_id)
        """
    )
    # Index 2 (partial 1): expire unconsumed tokens by expires_at
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_channel_action_tokens_expires
            ON dalton_channel_action_tokens (expires_at)
            WHERE consumed_at IS NULL
        """
    )
    # Index 3 (partial 2): track pending_bind (90s), send_in_flight (reconcile), send_unknown (drain)
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_channel_action_tokens_pending_bind
            ON dalton_channel_action_tokens (bind_status, created_at)
            WHERE token_role IN ('direct', 'gate')
              AND bind_status IN ('pending_bind', 'send_in_flight', 'send_unknown')
        """
    )
    # Index 4 (partial 3): batch scan late terminal card patch tasks
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_channel_action_tokens_late_patch
            ON dalton_channel_action_tokens (late_patch_next_retry_at, created_at)
            WHERE late_patch_status = 'pending'
        """
    )

    # 3. Unified interrupt resume contention & recovery table
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_interrupt_resumes (
            thread_id UUID NOT NULL,
            run_id VARCHAR(255) NOT NULL,
            interrupt_id VARCHAR(255) NOT NULL,
            claim_id UUID NOT NULL DEFAULT gen_random_uuid(),
            settled_via VARCHAR(32) NOT NULL CHECK (settled_via IN ('channel_button', 'channel_text', 'web')),
            settled_by_user_id VARCHAR(255) NOT NULL,
            platform VARCHAR(32) NULL CHECK (
                platform IS NULL OR platform IN ('feishu', 'discord', 'weixin')
            ),
            token_hash VARCHAR(64) NULL,
            inbound_message_id VARCHAR(255) NULL,
            legacy_direct BOOLEAN NOT NULL DEFAULT FALSE,
            origin_scope_key VARCHAR(255) NULL,
            session_generation INTEGER NULL,
            origin_chat_address VARCHAR(255) NULL,
            weixin_account_id UUID NULL,
            weixin_bot_id VARCHAR(255) NULL,
            weixin_bot_generation BIGINT NULL,
            weixin_recipient_id VARCHAR(255) NULL,
            request_key UUID NOT NULL,
            resume_payload JSONB NOT NULL,
            admitted_run_id VARCHAR(255) NULL,
            card_delivery_address VARCHAR(255) NULL,
            card_channel_id VARCHAR(255) NULL,
            card_message_id VARCHAR(255) NULL,
            superseded_token_hashes JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (
                jsonb_typeof(superseded_token_hashes) = 'array'
            ),
            card_targets JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (
                jsonb_typeof(card_targets) = 'array'
            ),
            resume_status VARCHAR(32) NOT NULL CHECK (
                resume_status IN ('pending_dispatch', 'admitted', 'terminal_failed')
            ),
            requires_gateway_ack BOOLEAN NOT NULL DEFAULT TRUE,
            delivery_settled BOOLEAN NOT NULL DEFAULT FALSE,
            lease_expires_at TIMESTAMPTZ NULL,
            run_consumed_at TIMESTAMPTZ NULL,
            channel_ack_at TIMESTAMPTZ NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at TIMESTAMPTZ NULL,
            last_error TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (thread_id, run_id, interrupt_id),
            CHECK (
                settled_via <> 'channel_text'
                OR legacy_direct = TRUE
                OR (platform IS NOT NULL AND inbound_message_id IS NOT NULL)
            ),
            CHECK (
                channel_ack_at IS NULL
                OR requires_gateway_ack = FALSE
                OR (
                    resume_status IN ('admitted', 'terminal_failed')
                    AND (resume_status = 'terminal_failed' OR run_consumed_at IS NOT NULL)
                    AND delivery_settled = TRUE
                    AND NOT (card_targets @> '[{"patch_status": "pending"}]'::jsonb)
                )
            )
        )
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_dalton_interrupt_resumes_wx_account'
            ) THEN
                ALTER TABLE dalton_channel_interrupt_resumes
                    ADD CONSTRAINT fk_dalton_interrupt_resumes_wx_account
                    FOREIGN KEY (weixin_account_id) REFERENCES dalton_channel_weixin_accounts(account_id)
                    ON DELETE SET NULL NOT VALID;
            END IF;
        END $$
        """
    )

    # Index 5 (partial 4): scan expired unadmitted claims
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_channel_interrupt_resumes_unadmitted
            ON dalton_channel_interrupt_resumes (lease_expires_at, created_at)
            WHERE resume_status = 'pending_dispatch'
        """
    )
    # Index 6 (partial 5): scan admitted runs whose checkpoint consumption is not yet stamped
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_channel_interrupt_resumes_unconsumed
            ON dalton_channel_interrupt_resumes (updated_at)
            WHERE resume_status = 'admitted' AND run_consumed_at IS NULL
        """
    )
    # Index 7 (partial 6): batch scan unacked rows by next_retry_at
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_channel_interrupt_resumes_unacked
            ON dalton_channel_interrupt_resumes (next_retry_at, updated_at)
            WHERE requires_gateway_ack = TRUE
              AND channel_ack_at IS NULL
        """
    )
    # Index 8 (partial 7): fast lookup by admitted_run_id when checkpoint advances
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_channel_interrupt_resumes_admitted_run
            ON dalton_channel_interrupt_resumes (admitted_run_id)
            WHERE admitted_run_id IS NOT NULL
        """
    )
    # Index 9 (partial 8): reverse lookup by originating inbound (platform, inbound_message_id)
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_channel_interrupt_resumes_inbound
            ON dalton_channel_interrupt_resumes (platform, inbound_message_id)
            WHERE inbound_message_id IS NOT NULL
        """
    )

    # 4. Additive columns on existing dalton_channel_deliveries table (BT2)
    op.execute(
        """
        ALTER TABLE dalton_channel_deliveries
            ADD COLUMN IF NOT EXISTS bt2_enrolled BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS source_delivery_key VARCHAR(512) NULL,
            ADD COLUMN IF NOT EXISTS deliverable_manifest JSONB NULL,
            ADD COLUMN IF NOT EXISTS authorized_target_meta JSONB NULL,
            ADD COLUMN IF NOT EXISTS claim_attempt_id UUID NULL,
            ADD COLUMN IF NOT EXISTS claim_gateway_epoch BIGINT NULL,
            ADD COLUMN IF NOT EXISTS claim_lease_expires_at TIMESTAMPTZ NULL
        """
    )

    # Index 10 (partial 9): shared unsettled BT2 link recovery predicate
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_channel_deliveries_links_recovery
            ON dalton_channel_deliveries (updated_at)
            WHERE (
                delivery_key LIKE 'deliverables-links:%'
                AND (
                    status IN ('in_flight', 'partial', 'unknown')
                    OR (
                        status = 'failed'
                        AND COALESCE(error_code, '') NOT IN (
                            'not_allowed',
                            'max_attempts_exceeded',
                            'skipped_platform_disabled'
                        )
                    )
                )
            )
            OR (
                bt2_enrolled = TRUE
                AND delivery_key NOT LIKE 'deliverables-links:%'
                AND deliverable_manifest IS NOT NULL
                AND jsonb_typeof(deliverable_manifest) = 'array'
                AND jsonb_array_length(deliverable_manifest) > 0
                AND (
                    status IN ('in_flight', 'sent', 'partial', 'unknown')
                    OR (
                        status = 'failed'
                        AND COALESCE(error_code, '') NOT IN (
                            'not_allowed',
                            'max_attempts_exceeded',
                            'skipped_platform_disabled'
                        )
                    )
                )
            )
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_deliveries_links_recovery")
    op.execute(
        """
        ALTER TABLE dalton_channel_deliveries
            DROP COLUMN IF EXISTS claim_lease_expires_at,
            DROP COLUMN IF EXISTS claim_gateway_epoch,
            DROP COLUMN IF EXISTS claim_attempt_id,
            DROP COLUMN IF EXISTS authorized_target_meta,
            DROP COLUMN IF EXISTS deliverable_manifest,
            DROP COLUMN IF EXISTS source_delivery_key,
            DROP COLUMN IF EXISTS bt2_enrolled
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_interrupt_resumes_inbound")
    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_interrupt_resumes_admitted_run")
    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_interrupt_resumes_unacked")
    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_interrupt_resumes_unconsumed")
    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_interrupt_resumes_unadmitted")
    op.execute("DROP TABLE IF EXISTS dalton_channel_interrupt_resumes")
    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_action_tokens_late_patch")
    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_action_tokens_pending_bind")
    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_action_tokens_expires")
    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_action_tokens_interrupt")
    op.execute("DROP TABLE IF EXISTS dalton_channel_action_tokens")
    op.execute("DROP TABLE IF EXISTS dalton_channel_interrupt_runtime_config")
