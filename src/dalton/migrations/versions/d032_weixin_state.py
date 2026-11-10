"""d032 weixin login session, bot generation, and encrypted context tokens.

Revision ID: d032
Revises: d031
Create Date: 2026-04-06

Follows d031 channel session selection. A database already stamped d032
does not re-run this revision, so the context-order columns are also added
with ADD COLUMN IF NOT EXISTS. Protocol seq and platform time each keep a
high-water on the context-token row for that bot, generation, and sender.
"""

from alembic import op

revision = "d032"
down_revision = "d031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE dalton_channel_weixin_state
            ALTER COLUMN ilink_bot_id DROP NOT NULL,
            ALTER COLUMN owner_user_id DROP NOT NULL,
            ALTER COLUMN encrypted_bot_token DROP NOT NULL;

        ALTER TABLE dalton_channel_weixin_state
            ADD COLUMN IF NOT EXISTS bot_generation BIGINT NOT NULL DEFAULT 1,
            ADD COLUMN IF NOT EXISTS ilink_user_id VARCHAR(255),
            ADD COLUMN IF NOT EXISTS ilink_username VARCHAR(255),
            ADD COLUMN IF NOT EXISTS login_session_key VARCHAR(255),
            ADD COLUMN IF NOT EXISTS login_owner_user_id VARCHAR(255),
            ADD COLUMN IF NOT EXISTS login_version BIGINT NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS login_expected_bot_generation BIGINT,
            ADD COLUMN IF NOT EXISTS login_status VARCHAR(64),
            ADD COLUMN IF NOT EXISTS login_qrcode TEXT,
            ADD COLUMN IF NOT EXISTS login_qrcode_img_content TEXT,
            ADD COLUMN IF NOT EXISTS login_redirect_base_url VARCHAR(512),
            ADD COLUMN IF NOT EXISTS login_expires_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS login_confirmed_result JSONB,
            ADD COLUMN IF NOT EXISTS login_updated_at TIMESTAMPTZ;

        ALTER TABLE dalton_channel_identities
            ADD COLUMN IF NOT EXISTS encrypted_weixin_context_token BYTEA,
            ADD COLUMN IF NOT EXISTS weixin_context_bot_id VARCHAR(255),
            ADD COLUMN IF NOT EXISTS weixin_context_bot_generation BIGINT,
            ADD COLUMN IF NOT EXISTS weixin_context_seq BIGINT NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS weixin_context_seq_source VARCHAR(32) NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS weixin_context_updated_at TIMESTAMPTZ;

        UPDATE dalton_channel_identities
        SET weixin_context_token = NULL
        WHERE weixin_context_token IS NOT NULL;

        ALTER TABLE dalton_channel_inbound_events
            ADD COLUMN IF NOT EXISTS encrypted_weixin_context_token BYTEA,
            ADD COLUMN IF NOT EXISTS weixin_context_bot_id VARCHAR(255),
            ADD COLUMN IF NOT EXISTS weixin_context_bot_generation BIGINT,
            ADD COLUMN IF NOT EXISTS weixin_context_seq BIGINT,
            ADD COLUMN IF NOT EXISTS consumed_bind_result JSONB;

        CREATE TABLE IF NOT EXISTS dalton_channel_weixin_context_tokens (
            ilink_bot_id VARCHAR(255) NOT NULL,
            bot_generation BIGINT NOT NULL,
            platform_user_id VARCHAR(255) NOT NULL,
            encrypted_context_token BYTEA NOT NULL,
            seq BIGINT NOT NULL DEFAULT 0,
            seq_source VARCHAR(32) NOT NULL DEFAULT 'protocol',
            protocol_seq_high BIGINT NOT NULL DEFAULT 0,
            platform_time_high BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (ilink_bot_id, bot_generation, platform_user_id)
        );

        ALTER TABLE dalton_channel_weixin_context_tokens
            ADD COLUMN IF NOT EXISTS seq_source VARCHAR(32) NOT NULL DEFAULT 'protocol',
            ADD COLUMN IF NOT EXISTS protocol_seq_high BIGINT NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS platform_time_high BIGINT NOT NULL DEFAULT 0;

        UPDATE dalton_channel_weixin_context_tokens
        SET protocol_seq_high = GREATEST(protocol_seq_high, seq)
        WHERE seq_source = 'protocol';

        UPDATE dalton_channel_weixin_context_tokens
        SET platform_time_high = GREATEST(platform_time_high, seq)
        WHERE seq_source = 'platform_time';

        CREATE INDEX IF NOT EXISTS idx_dalton_channel_wx_ctx_tokens_lookup
            ON dalton_channel_weixin_context_tokens (platform_user_id, ilink_bot_id, bot_generation);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS dalton_channel_weixin_context_tokens;

        ALTER TABLE dalton_channel_inbound_events
            DROP COLUMN IF EXISTS consumed_bind_result,
            DROP COLUMN IF EXISTS weixin_context_seq,
            DROP COLUMN IF EXISTS weixin_context_bot_generation,
            DROP COLUMN IF EXISTS weixin_context_bot_id,
            DROP COLUMN IF EXISTS encrypted_weixin_context_token;

        ALTER TABLE dalton_channel_identities
            DROP COLUMN IF EXISTS weixin_context_updated_at,
            DROP COLUMN IF EXISTS weixin_context_seq_source,
            DROP COLUMN IF EXISTS weixin_context_seq,
            DROP COLUMN IF EXISTS weixin_context_bot_generation,
            DROP COLUMN IF EXISTS weixin_context_bot_id,
            DROP COLUMN IF EXISTS encrypted_weixin_context_token;

        ALTER TABLE dalton_channel_weixin_state
            DROP COLUMN IF EXISTS login_updated_at,
            DROP COLUMN IF EXISTS login_confirmed_result,
            DROP COLUMN IF EXISTS login_expires_at,
            DROP COLUMN IF EXISTS login_redirect_base_url,
            DROP COLUMN IF EXISTS login_qrcode_img_content,
            DROP COLUMN IF EXISTS login_qrcode,
            DROP COLUMN IF EXISTS login_status,
            DROP COLUMN IF EXISTS login_expected_bot_generation,
            DROP COLUMN IF EXISTS login_version,
            DROP COLUMN IF EXISTS login_owner_user_id,
            DROP COLUMN IF EXISTS login_session_key,
            DROP COLUMN IF EXISTS ilink_username,
            DROP COLUMN IF EXISTS ilink_user_id,
            DROP COLUMN IF EXISTS bot_generation;
        """
    )
