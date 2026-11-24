"""Dalton d049: Multi-account personal Weixin channel tables and copy-only backfill.

Revision ID: d049
Revises: d046
Create Date: 2026-11-24

Design reference: ``docs/dalton/features/weixin-multi-account.md`` §2.1–§2.3.

Zero-mutation / lossless downgrade contract (D2, D10, D18):
- Creates 8 new ``dalton_channel_*`` tables, 13 nullable columns across 4 hot
  shared channel tables, and 5 ``NOT VALID`` foreign keys (validated post-deploy
  via ``src/dalton/database/weixin_fk_validate.py`` outside this transaction).
- Reads legacy ``dalton_channel_weixin_state``, ``dalton_channel_identities``,
  ``dalton_channel_weixin_context_tokens``, and existing rows/columns of shared
  tables **strictly read-only** — performs zero writes or deletes on any legacy
  table or column.
- ``downgrade()`` drops only the objects created by ``d049`` so rolling back
  before new business writes restores a row-for-row identical ``d046`` database.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d049"
down_revision: str | None = "d046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

    # (1) Deployment-scoped single gateway instance lease table
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_gateway_lease (
            singleton_id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (singleton_id = 1),
            instance_id  VARCHAR(128) NOT NULL,
            epoch        BIGINT NOT NULL DEFAULT 1,
            acquired_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at   TIMESTAMPTZ NOT NULL,
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    # (2) Team Weixin runtime configuration table
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_weixin_config (
            singleton_id      SMALLINT PRIMARY KEY DEFAULT 1 CHECK (singleton_id = 1),
            enabled           BOOLEAN NOT NULL DEFAULT TRUE,
            allow_external    BOOLEAN,
            accounts_revision BIGINT NOT NULL DEFAULT 1,
            updated_by        VARCHAR(255),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    # (3) Personal Weixin multi-account credentials, identity & cursor table
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_weixin_accounts (
            account_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id                   VARCHAR(255) NOT NULL UNIQUE
                                      CHECK (user_id <> 'system:dalton-company'),
            ilink_bot_id              VARCHAR(255) NOT NULL UNIQUE,
            ilink_user_id             VARCHAR(255),
            prev_ilink_user_id        VARCHAR(255),
            ilink_username            VARCHAR(255),
            encrypted_bot_token       BYTEA NOT NULL,
            base_url                  VARCHAR(512) NOT NULL DEFAULT 'https://ilinkai.weixin.qq.com',
            cdn_base_url              VARCHAR(512) NOT NULL DEFAULT 'https://novac2c.cdn.weixin.qq.com/c2c',
            get_updates_buf           TEXT NOT NULL DEFAULT '',
            status                    VARCHAR(32) NOT NULL DEFAULT 'active'
                                      CHECK (status IN ('active', 'pending_verification', 'expired', 'disabled')),
            prev_status               VARCHAR(32),
            disabled_reason           VARCHAR(64),
            bot_generation            BIGINT NOT NULL DEFAULT 1,
            prev_bot_generation       BIGINT,
            bot_generation_updated_at TIMESTAMPTZ,
            last_inbound_at           TIMESTAMPTZ,
            last_error_code           VARCHAR(64),
            last_error_at             TIMESTAMPTZ,
            created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (prev_ilink_user_id IS NULL OR ilink_user_id IS NULL),
            CHECK (
                (status = 'disabled'
                 AND disabled_reason IN ('member_not_in_roster', 'external_access_disabled')
                 AND prev_status IN ('active', 'pending_verification', 'expired'))
                OR (status <> 'disabled' AND disabled_reason IS NULL AND prev_status IS NULL)
            )
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_dalton_wx_accounts_ilink_user_occupied
            ON dalton_channel_weixin_accounts (COALESCE(ilink_user_id, prev_ilink_user_id))
            WHERE COALESCE(ilink_user_id, prev_ilink_user_id) IS NOT NULL
              AND (status IN ('active', 'pending_verification', 'expired') OR disabled_reason = 'external_access_disabled');
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_wx_accounts_status
            ON dalton_channel_weixin_accounts (status, updated_at DESC);
        """
    )

    # (4) Multi-account encrypted context tokens & QR login sessions
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_weixin_account_tokens (
            account_id              UUID NOT NULL REFERENCES dalton_channel_weixin_accounts(account_id) ON DELETE CASCADE,
            ilink_bot_id            VARCHAR(255) NOT NULL,
            bot_generation          BIGINT NOT NULL,
            platform_user_id        VARCHAR(255) NOT NULL,
            encrypted_context_token BYTEA NOT NULL,
            seq                     BIGINT NOT NULL DEFAULT 0,
            seq_source              VARCHAR(32) NOT NULL DEFAULT 'protocol',
            protocol_seq_high       BIGINT NOT NULL DEFAULT 0,
            platform_time_high      BIGINT NOT NULL DEFAULT 0,
            token_version           BIGINT NOT NULL DEFAULT 1,
            stale_at                TIMESTAMPTZ,
            stale_reason            VARCHAR(64),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (account_id, bot_generation, platform_user_id)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_weixin_login_sessions (
            session_key             VARCHAR(255) PRIMARY KEY,
            user_id                 VARCHAR(255) NOT NULL CHECK (user_id <> 'system:dalton-company'),
            target_account_id       UUID REFERENCES dalton_channel_weixin_accounts(account_id) ON DELETE SET NULL,
            login_version           BIGINT NOT NULL DEFAULT 1,
            expected_bot_generation BIGINT,
            status                  VARCHAR(64) NOT NULL DEFAULT 'starting'
                                    CHECK (status IN ('starting', 'wait', 'scaned', 'verify_code', 'confirmed', 'expired', 'superseded')),
            encrypted_qrcode        BYTEA,
            qrcode_img_content      TEXT,
            redirect_base_url       VARCHAR(512) NOT NULL DEFAULT 'https://ilinkai.weixin.qq.com',
            expires_at              TIMESTAMPTZ NOT NULL,
            confirmed_result        JSONB,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (status IN ('starting', 'expired', 'superseded') OR encrypted_qrcode IS NOT NULL)
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_dalton_wx_login_sessions_user_pending
            ON dalton_channel_weixin_login_sessions (user_id)
            WHERE status IN ('starting', 'wait', 'scaned', 'verify_code');
        """
    )

    # (5) Audit log table
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_weixin_audit_logs (
            audit_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            action               VARCHAR(64) NOT NULL,
            operator_user_id     VARCHAR(255) NOT NULL,
            target_user_id       VARCHAR(255),
            weixin_account_id    UUID,
            ilink_bot_id         VARCHAR(255),
            masked_ilink_user_id VARCHAR(32),
            reason               TEXT NOT NULL,
            metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_wx_audit_logs_target_time
            ON dalton_channel_weixin_audit_logs (target_user_id, created_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_wx_audit_logs_time
            ON dalton_channel_weixin_audit_logs (created_at DESC);
        """
    )

    # (6) Explicit message collection buffers & items
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_weixin_collect_buffers (
            buffer_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            account_id          UUID REFERENCES dalton_channel_weixin_accounts(account_id) ON DELETE SET NULL,
            bot_generation      BIGINT NOT NULL,
            dalton_user_id      VARCHAR(255) NOT NULL,
            scope_key           VARCHAR(255) NOT NULL,
            session_generation  INTEGER NOT NULL,
            workspace_id        UUID NOT NULL,
            initial_prompt      TEXT NOT NULL DEFAULT '',
            flush_prompt        TEXT,
            status              VARCHAR(32) NOT NULL DEFAULT 'collecting'
                                CHECK (status IN ('collecting', 'flushing', 'done', 'cancelled', 'expired')),
            item_count          INTEGER NOT NULL DEFAULT 0,
            total_text_chars    INTEGER NOT NULL DEFAULT 0,
            attachment_count    INTEGER NOT NULL DEFAULT 0,
            expires_at          TIMESTAMPTZ NOT NULL,
            max_expires_at      TIMESTAMPTZ NOT NULL,
            flushed_message_id  VARCHAR(255),
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_dalton_wx_collect_active
            ON dalton_channel_weixin_collect_buffers (account_id, bot_generation, scope_key, session_generation)
            WHERE status IN ('collecting', 'flushing');
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_weixin_collect_items (
            buffer_id                 UUID NOT NULL REFERENCES dalton_channel_weixin_collect_buffers(buffer_id) ON DELETE CASCADE,
            message_id                VARCHAR(255) NOT NULL,
            protocol_seq              BIGINT,
            platform_time_ms          BIGINT,
            receive_order             INTEGER NOT NULL,
            text_content              TEXT NOT NULL DEFAULT '',
            expected_attachment_count INTEGER NOT NULL DEFAULT 0,
            upload_status             VARCHAR(32) NOT NULL DEFAULT 'ready'
                                      CHECK (upload_status IN ('pending_upload', 'ready', 'failed')),
            uploaded_attachments      JSONB NOT NULL DEFAULT '[]'::jsonb,
            upload_error              TEXT,
            created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (buffer_id, message_id)
        );
        """
    )

    # (7) Hot shared tables: 13 nullable columns + 5 NOT VALID foreign keys
    op.execute(
        """
        ALTER TABLE dalton_channel_inbound_events
            ADD COLUMN IF NOT EXISTS weixin_account_id UUID,
            ADD COLUMN IF NOT EXISTS encrypted_weixin_media_meta BYTEA,
            ADD COLUMN IF NOT EXISTS collect_buffer_id UUID;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_dalton_inbound_wx_account'
            ) THEN
                ALTER TABLE dalton_channel_inbound_events
                    ADD CONSTRAINT fk_dalton_inbound_wx_account
                    FOREIGN KEY (weixin_account_id) REFERENCES dalton_channel_weixin_accounts(account_id)
                    ON DELETE SET NULL NOT VALID;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_dalton_inbound_wx_collect_buf'
            ) THEN
                ALTER TABLE dalton_channel_inbound_events
                    ADD CONSTRAINT fk_dalton_inbound_wx_collect_buf
                    FOREIGN KEY (collect_buffer_id) REFERENCES dalton_channel_weixin_collect_buffers(buffer_id)
                    ON DELETE SET NULL NOT VALID;
            END IF;
        END $$;
        """
    )

    op.execute(
        """
        ALTER TABLE dalton_channel_thread_watches
            ADD COLUMN IF NOT EXISTS weixin_account_id UUID,
            ADD COLUMN IF NOT EXISTS weixin_recipient_id VARCHAR(255),
            ADD COLUMN IF NOT EXISTS weixin_bot_generation BIGINT;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_dalton_watches_wx_account'
            ) THEN
                ALTER TABLE dalton_channel_thread_watches
                    ADD CONSTRAINT fk_dalton_watches_wx_account
                    FOREIGN KEY (weixin_account_id) REFERENCES dalton_channel_weixin_accounts(account_id)
                    ON DELETE SET NULL NOT VALID;
            END IF;
        END $$;
        """
    )

    op.execute(
        """
        ALTER TABLE dalton_channel_background_runs
            ADD COLUMN IF NOT EXISTS weixin_account_id UUID,
            ADD COLUMN IF NOT EXISTS weixin_recipient_id VARCHAR(255),
            ADD COLUMN IF NOT EXISTS weixin_bot_generation BIGINT;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_dalton_bg_runs_wx_account'
            ) THEN
                ALTER TABLE dalton_channel_background_runs
                    ADD CONSTRAINT fk_dalton_bg_runs_wx_account
                    FOREIGN KEY (weixin_account_id) REFERENCES dalton_channel_weixin_accounts(account_id)
                    ON DELETE SET NULL NOT VALID;
            END IF;
        END $$;
        """
    )

    op.execute(
        """
        ALTER TABLE dalton_channel_deliveries
            ADD COLUMN IF NOT EXISTS weixin_account_id UUID,
            ADD COLUMN IF NOT EXISTS weixin_bot_id VARCHAR(255),
            ADD COLUMN IF NOT EXISTS weixin_bot_generation BIGINT,
            ADD COLUMN IF NOT EXISTS weixin_recipient_id VARCHAR(255);
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_dalton_deliveries_wx_account'
            ) THEN
                ALTER TABLE dalton_channel_deliveries
                    ADD CONSTRAINT fk_dalton_deliveries_wx_account
                    FOREIGN KEY (weixin_account_id) REFERENCES dalton_channel_weixin_accounts(account_id)
                    ON DELETE SET NULL NOT VALID;
            END IF;
        END $$;
        """
    )

    # §2.2 Copy-only backfill (zero mutations to legacy tables or legacy columns)
    bind = op.get_bind()

    # 1. Initialize runtime config singleton row
    bind.execute(
        sa.text(
            """
            INSERT INTO dalton_channel_weixin_config (singleton_id, enabled, accounts_revision)
            VALUES (1, TRUE, 1)
            ON CONFLICT (singleton_id) DO NOTHING
            """
        )
    )

    # 2. Read-only snapshot of legacy singleton dalton_channel_weixin_state.
    # Verified across d030/d032: legacy dalton_channel_weixin_state has no cdn_base_url
    # column, so migrated accounts intentionally inherit the new table's default
    # cdn_base_url ('https://novac2c.cdn.weixin.qq.com/c2c').
    legacy_state = bind.execute(
        sa.text(
            """
            SELECT ilink_bot_id,
                   owner_user_id,
                   encrypted_bot_token,
                   base_url,
                   get_updates_buf,
                   status,
                   bot_generation,
                   ilink_user_id,
                   ilink_username,
                   updated_at
            FROM dalton_channel_weixin_state
            WHERE singleton_id = 1
            """
        )
    ).mappings().first()

    if (
        legacy_state is None
        or not legacy_state.get("ilink_bot_id")
        or legacy_state.get("encrypted_bot_token") is None
        or not legacy_state.get("owner_user_id")
        or str(legacy_state.get("owner_user_id")).strip() == "system:dalton-company"
    ):
        return

    owner_user_id = str(legacy_state["owner_user_id"]).strip()
    ilink_bot_id = str(legacy_state["ilink_bot_id"]).strip()
    bot_generation = int(legacy_state.get("bot_generation") or 1)
    base_url = str(legacy_state.get("base_url") or "https://ilinkai.weixin.qq.com").strip()
    get_updates_buf = str(legacy_state.get("get_updates_buf") or "")
    ilink_username = legacy_state.get("ilink_username")
    raw_status = str(legacy_state.get("status") or "active").strip().lower()

    # 3. Determine effective ilink_user_id (read-only)
    eff_ilink_user_id = (
        str(legacy_state["ilink_user_id"]).strip()
        if legacy_state.get("ilink_user_id")
        and str(legacy_state.get("ilink_user_id")).strip()
        else None
    )
    if eff_ilink_user_id is None:
        id_row = bind.execute(
            sa.text(
                """
                SELECT platform_user_id
                FROM dalton_channel_identities
                WHERE user_id = :owner_user_id AND platform = 'weixin'
                LIMIT 1
                """
            ),
            {"owner_user_id": owner_user_id},
        ).mappings().first()
        if id_row and id_row.get("platform_user_id"):
            candidate_wx_uid = str(id_row["platform_user_id"]).strip()
            has_matching_tok = bind.execute(
                sa.text(
                    """
                    SELECT 1
                    FROM dalton_channel_weixin_context_tokens
                    WHERE ilink_bot_id = :ilink_bot_id
                      AND platform_user_id = :platform_user_id
                    LIMIT 1
                    """
                ),
                {
                    "ilink_bot_id": ilink_bot_id,
                    "platform_user_id": candidate_wx_uid,
                },
            ).first()
            if has_matching_tok is not None:
                eff_ilink_user_id = candidate_wx_uid

    if raw_status in ("session_expired", "expired"):
        new_status = "expired"
    elif eff_ilink_user_id is None:
        new_status = "pending_verification"
    else:
        new_status = "active"

    inserted_acc = bind.execute(
        sa.text(
            """
            INSERT INTO dalton_channel_weixin_accounts (
                user_id,
                ilink_bot_id,
                ilink_user_id,
                prev_ilink_user_id,
                ilink_username,
                encrypted_bot_token,
                base_url,
                get_updates_buf,
                status,
                bot_generation,
                created_at,
                updated_at
            )
            VALUES (
                :user_id,
                :ilink_bot_id,
                :ilink_user_id,
                NULL,
                :ilink_username,
                :encrypted_bot_token,
                :base_url,
                :get_updates_buf,
                :status,
                :bot_generation,
                COALESCE(CAST(:updated_at AS TIMESTAMPTZ), NOW()),
                COALESCE(CAST(:updated_at AS TIMESTAMPTZ), NOW())
            )
            ON CONFLICT (user_id) DO NOTHING
            RETURNING account_id
            """
        ),
        {
            "user_id": owner_user_id,
            "ilink_bot_id": ilink_bot_id,
            "ilink_user_id": eff_ilink_user_id,
            "ilink_username": ilink_username,
            "encrypted_bot_token": legacy_state["encrypted_bot_token"],
            "base_url": base_url,
            "get_updates_buf": get_updates_buf,
            "status": new_status,
            "bot_generation": bot_generation,
            "updated_at": legacy_state.get("updated_at"),
        },
    ).mappings().first()

    if inserted_acc is None:
        existing_acc = bind.execute(
            sa.text(
                "SELECT account_id FROM dalton_channel_weixin_accounts WHERE user_id = :user_id"
            ),
            {"user_id": owner_user_id},
        ).mappings().first()
        if existing_acc is None:
            return
        new_account_id = existing_acc["account_id"]
    else:
        new_account_id = inserted_acc["account_id"]

    # Copy matching context tokens into dalton_channel_weixin_account_tokens
    bind.execute(
        sa.text(
            """
            INSERT INTO dalton_channel_weixin_account_tokens (
                account_id,
                ilink_bot_id,
                bot_generation,
                platform_user_id,
                encrypted_context_token,
                seq,
                seq_source,
                protocol_seq_high,
                platform_time_high,
                token_version,
                updated_at
            )
            SELECT
                :account_id,
                t.ilink_bot_id,
                t.bot_generation,
                t.platform_user_id,
                t.encrypted_context_token,
                t.seq,
                t.seq_source,
                t.protocol_seq_high,
                t.platform_time_high,
                1,
                t.updated_at
            FROM dalton_channel_weixin_context_tokens t
            WHERE t.ilink_bot_id = :ilink_bot_id
              AND (CAST(:eff_ilink_user_id AS VARCHAR) IS NULL OR t.platform_user_id = CAST(:eff_ilink_user_id AS VARCHAR))
            ON CONFLICT (account_id, bot_generation, platform_user_id) DO NOTHING
            """
        ),
        {
            "account_id": new_account_id,
            "ilink_bot_id": ilink_bot_id,
            "eff_ilink_user_id": eff_ilink_user_id,
        },
    )

    bind.execute(
        sa.text(
            """
            UPDATE dalton_channel_weixin_accounts a
            SET last_inbound_at = (
                SELECT MAX(t.updated_at)
                FROM dalton_channel_weixin_account_tokens t
                WHERE t.account_id = a.account_id
            )
            WHERE a.account_id = :account_id
            """
        ),
        {"account_id": new_account_id},
    )

    # 4② Backfill NEW columns on dalton_channel_inbound_events
    bind.execute(
        sa.text(
            """
            UPDATE dalton_channel_inbound_events
            SET weixin_account_id = :account_id
            WHERE platform = 'weixin'
              AND weixin_account_id IS NULL
              AND COALESCE(weixin_context_bot_id, raw_payload->>'ilink_bot_id') = :ilink_bot_id
              AND (CAST(:eff_ilink_user_id AS VARCHAR) IS NULL OR raw_payload->>'platform_user_id' = CAST(:eff_ilink_user_id AS VARCHAR))
            """
        ),
        {
            "account_id": new_account_id,
            "ilink_bot_id": ilink_bot_id,
            "eff_ilink_user_id": eff_ilink_user_id,
        },
    )

    unfinished_media_cnt = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM dalton_channel_inbound_events
            WHERE platform = 'weixin'
              AND weixin_account_id = :account_id
              AND encrypted_weixin_media_meta IS NULL
              AND (
                  status IN ('pending', 'accepted', 'streaming', 'steered', 'pending_resubmit')
                  OR fallback_pending = TRUE
              )
              AND raw_payload ? 'weixin_attachments'
            """
        ),
        {"account_id": new_account_id},
    ).scalar_one()

    if int(unfinished_media_cnt or 0) > 0:
        from src.server.database.encryption import get_encryption_key

        enc_key = get_encryption_key()
        bind.execute(
            sa.text(
                """
                UPDATE dalton_channel_inbound_events
                SET encrypted_weixin_media_meta = pgp_sym_encrypt(
                    (raw_payload->'weixin_attachments')::text,
                    :enc_key
                )
                WHERE platform = 'weixin'
                  AND weixin_account_id = :account_id
                  AND encrypted_weixin_media_meta IS NULL
                  AND (
                      status IN ('pending', 'accepted', 'streaming', 'steered', 'pending_resubmit')
                      OR fallback_pending = TRUE
                  )
                  AND raw_payload ? 'weixin_attachments'
                """
            ),
            {"account_id": new_account_id, "enc_key": enc_key},
        )

    # 4③ Backfill NEW columns on dalton_channel_thread_watches
    if eff_ilink_user_id is not None:
        bind.execute(
            sa.text(
                """
                WITH unique_thread_gen AS (
                    SELECT thread_id,
                           MIN(COALESCE(weixin_context_bot_generation, :default_gen)) AS bot_gen
                    FROM dalton_channel_inbound_events
                    WHERE platform = 'weixin'
                      AND thread_id IS NOT NULL
                    GROUP BY thread_id
                    HAVING COUNT(DISTINCT COALESCE(weixin_context_bot_id, raw_payload->>'ilink_bot_id', '')) = 1
                       AND MIN(COALESCE(weixin_context_bot_id, raw_payload->>'ilink_bot_id', '')) = :ilink_bot_id
                       AND COUNT(DISTINCT COALESCE(weixin_context_bot_generation, :default_gen)) = 1
                )
                UPDATE dalton_channel_thread_watches w
                SET weixin_account_id = :account_id,
                    weixin_recipient_id = :eff_ilink_user_id,
                    weixin_bot_generation = u.bot_gen
                FROM unique_thread_gen u
                WHERE w.thread_id = u.thread_id
                  AND w.platform = 'weixin'
                  AND w.dalton_user_id = :owner_user_id
                  AND w.scope_key = 'dm:' || :eff_ilink_user_id
                  AND w.weixin_account_id IS NULL
                """
            ),
            {
                "account_id": new_account_id,
                "eff_ilink_user_id": eff_ilink_user_id,
                "owner_user_id": owner_user_id,
                "ilink_bot_id": ilink_bot_id,
                "default_gen": bot_generation,
            },
        )
    else:
        bind.execute(
            sa.text(
                """
                WITH latest_thread_in AS (
                    SELECT DISTINCT ON (thread_id)
                           thread_id,
                           COALESCE(weixin_context_bot_id, raw_payload->>'ilink_bot_id') AS bot_id,
                           weixin_context_bot_generation AS bot_gen
                    FROM dalton_channel_inbound_events
                    WHERE platform = 'weixin'
                      AND thread_id IS NOT NULL
                      AND dalton_user_id = :owner_user_id
                    ORDER BY thread_id, created_at DESC
                )
                UPDATE dalton_channel_thread_watches w
                SET weixin_account_id = :account_id,
                    weixin_bot_generation = COALESCE(l.bot_gen, :default_gen)
                FROM latest_thread_in l
                WHERE w.thread_id = l.thread_id
                  AND w.platform = 'weixin'
                  AND w.dalton_user_id = :owner_user_id
                  AND l.bot_id = :ilink_bot_id
                  AND w.weixin_account_id IS NULL
                """
            ),
            {
                "account_id": new_account_id,
                "owner_user_id": owner_user_id,
                "ilink_bot_id": ilink_bot_id,
                "default_gen": bot_generation,
            },
        )

    # 4④ Backfill NEW columns on dalton_channel_background_runs
    # Priority 1: inherit from already-backfilled thread_watches(thread_id)
    bind.execute(
        sa.text(
            """
            UPDATE dalton_channel_background_runs bg
            SET weixin_account_id = w.weixin_account_id,
                weixin_recipient_id = w.weixin_recipient_id,
                weixin_bot_generation = w.weixin_bot_generation
            FROM dalton_channel_thread_watches w
            WHERE bg.thread_id = w.thread_id
              AND bg.platform = 'weixin'
              AND w.platform = 'weixin'
              AND bg.dalton_user_id = w.dalton_user_id
              AND w.weixin_account_id IS NOT NULL
              AND w.weixin_bot_generation IS NOT NULL
              AND bg.weixin_account_id IS NULL
            """
        )
    )
    # Priority 2: unique inbound source on same thread_id with created_at <= bg.created_at
    bind.execute(
        sa.text(
            """
            WITH unique_bg_src AS (
                SELECT bg.run_id,
                       MIN(ie.weixin_account_id::text)::uuid AS wx_account_id,
                       MIN(NULLIF(btrim(COALESCE(ie.raw_payload->>'platform_user_id', '')), '')) AS wx_recipient_id,
                       MIN(ie.weixin_context_bot_generation) AS wx_bot_gen
                FROM dalton_channel_background_runs bg
                JOIN dalton_channel_inbound_events ie
                  ON ie.platform = 'weixin'
                 AND ie.thread_id = bg.thread_id
                 AND ie.created_at <= bg.created_at
                 AND ie.weixin_account_id = :account_id
                 AND ie.weixin_context_bot_generation IS NOT NULL
                WHERE bg.platform = 'weixin'
                  AND bg.dalton_user_id = :owner_user_id
                  AND bg.weixin_account_id IS NULL
                GROUP BY bg.run_id
                HAVING COUNT(DISTINCT ie.weixin_account_id) = 1
                   AND COUNT(DISTINCT ie.weixin_context_bot_generation) = 1
                   AND COUNT(DISTINCT COALESCE(ie.raw_payload->>'platform_user_id', '')) <= 1
            )
            UPDATE dalton_channel_background_runs bg
            SET weixin_account_id = s.wx_account_id,
                weixin_recipient_id = COALESCE(s.wx_recipient_id, :eff_ilink_user_id),
                weixin_bot_generation = s.wx_bot_gen
            FROM unique_bg_src s
            WHERE bg.run_id = s.run_id
              AND bg.weixin_account_id IS NULL
            """
        ),
        {
            "account_id": new_account_id,
            "owner_user_id": owner_user_id,
            "eff_ilink_user_id": eff_ilink_user_id,
        },
    )

    # 4⑤ Backfill NEW columns on dalton_channel_deliveries
    # Priority 1a: match by run_id to inbound_events
    bind.execute(
        sa.text(
            """
            WITH unique_in_run AS (
                SELECT run_id,
                       MIN(weixin_account_id::text)::uuid AS wx_account_id,
                       MIN(COALESCE(weixin_context_bot_id, raw_payload->>'ilink_bot_id')) AS wx_bot_id,
                       MIN(weixin_context_bot_generation) AS wx_bot_gen,
                       MIN(NULLIF(btrim(COALESCE(raw_payload->>'platform_user_id', '')), '')) AS wx_recipient_id
                FROM dalton_channel_inbound_events
                WHERE platform = 'weixin'
                  AND run_id IS NOT NULL
                  AND weixin_account_id IS NOT NULL
                  AND weixin_context_bot_generation IS NOT NULL
                GROUP BY run_id
                HAVING COUNT(DISTINCT weixin_account_id) = 1
                   AND COUNT(DISTINCT weixin_context_bot_generation) = 1
                   AND COUNT(DISTINCT COALESCE(raw_payload->>'platform_user_id', '')) <= 1
            )
            UPDATE dalton_channel_deliveries d
            SET weixin_account_id = s.wx_account_id,
                weixin_bot_id = s.wx_bot_id,
                weixin_bot_generation = s.wx_bot_gen,
                weixin_recipient_id = COALESCE(s.wx_recipient_id, :eff_ilink_user_id)
            FROM unique_in_run s
            WHERE d.platform = 'weixin'
              AND d.run_id IS NOT NULL
              AND d.run_id = s.run_id
              AND d.weixin_account_id IS NULL
            """
        ),
        {"eff_ilink_user_id": eff_ilink_user_id},
    )
    # Priority 1b: match by run_id to background_runs
    bind.execute(
        sa.text(
            """
            UPDATE dalton_channel_deliveries d
            SET weixin_account_id = bg.weixin_account_id,
                weixin_bot_id = :ilink_bot_id,
                weixin_bot_generation = bg.weixin_bot_generation,
                weixin_recipient_id = bg.weixin_recipient_id
            FROM dalton_channel_background_runs bg
            WHERE d.platform = 'weixin'
              AND d.run_id IS NOT NULL
              AND d.run_id = bg.run_id
              AND bg.weixin_account_id IS NOT NULL
              AND bg.weixin_bot_generation IS NOT NULL
              AND d.weixin_account_id IS NULL
            """
        ),
        {"ilink_bot_id": ilink_bot_id},
    )
    # Priority 2: run_id IS NULL -> unique inbound_events on thread_id with created_at <= d.created_at
    bind.execute(
        sa.text(
            """
            WITH unique_del_src AS (
                SELECT d.delivery_key,
                       MIN(ie.weixin_account_id::text)::uuid AS wx_account_id,
                       MIN(COALESCE(ie.weixin_context_bot_id, ie.raw_payload->>'ilink_bot_id')) AS wx_bot_id,
                       MIN(ie.weixin_context_bot_generation) AS wx_bot_gen,
                       MIN(NULLIF(btrim(COALESCE(ie.raw_payload->>'platform_user_id', '')), '')) AS wx_recipient_id
                FROM dalton_channel_deliveries d
                JOIN dalton_channel_inbound_events ie
                  ON ie.platform = 'weixin'
                 AND ie.thread_id = d.thread_id
                 AND ie.created_at <= d.created_at
                 AND ie.weixin_account_id IS NOT NULL
                 AND ie.weixin_context_bot_generation IS NOT NULL
                WHERE d.platform = 'weixin'
                  AND d.run_id IS NULL
                  AND d.thread_id IS NOT NULL
                  AND d.weixin_account_id IS NULL
                GROUP BY d.delivery_key
                HAVING COUNT(DISTINCT ie.weixin_account_id) = 1
                   AND COUNT(DISTINCT COALESCE(ie.weixin_context_bot_id, ie.raw_payload->>'ilink_bot_id', '')) = 1
                   AND COUNT(DISTINCT ie.weixin_context_bot_generation) = 1
                   AND COUNT(DISTINCT COALESCE(ie.raw_payload->>'platform_user_id', '')) <= 1
            )
            UPDATE dalton_channel_deliveries d
            SET weixin_account_id = s.wx_account_id,
                weixin_bot_id = s.wx_bot_id,
                weixin_bot_generation = s.wx_bot_gen,
                weixin_recipient_id = COALESCE(s.wx_recipient_id, :eff_ilink_user_id)
            FROM unique_del_src s
            WHERE d.delivery_key = s.delivery_key
              AND d.weixin_account_id IS NULL
            """
        ),
        {"eff_ilink_user_id": eff_ilink_user_id},
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # Drop 5 foreign key constraints added by d049
    op.execute(
        "ALTER TABLE dalton_channel_deliveries DROP CONSTRAINT IF EXISTS fk_dalton_deliveries_wx_account;"
    )
    op.execute(
        "ALTER TABLE dalton_channel_background_runs DROP CONSTRAINT IF EXISTS fk_dalton_bg_runs_wx_account;"
    )
    op.execute(
        "ALTER TABLE dalton_channel_thread_watches DROP CONSTRAINT IF EXISTS fk_dalton_watches_wx_account;"
    )
    op.execute(
        "ALTER TABLE dalton_channel_inbound_events DROP CONSTRAINT IF EXISTS fk_dalton_inbound_wx_collect_buf;"
    )
    op.execute(
        "ALTER TABLE dalton_channel_inbound_events DROP CONSTRAINT IF EXISTS fk_dalton_inbound_wx_account;"
    )

    # Drop 13 new columns from 4 hot tables
    op.execute(
        """
        ALTER TABLE dalton_channel_deliveries
            DROP COLUMN IF EXISTS weixin_recipient_id,
            DROP COLUMN IF EXISTS weixin_bot_generation,
            DROP COLUMN IF EXISTS weixin_bot_id,
            DROP COLUMN IF EXISTS weixin_account_id;
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_channel_background_runs
            DROP COLUMN IF EXISTS weixin_bot_generation,
            DROP COLUMN IF EXISTS weixin_recipient_id,
            DROP COLUMN IF EXISTS weixin_account_id;
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_channel_thread_watches
            DROP COLUMN IF EXISTS weixin_bot_generation,
            DROP COLUMN IF EXISTS weixin_recipient_id,
            DROP COLUMN IF EXISTS weixin_account_id;
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_channel_inbound_events
            DROP COLUMN IF EXISTS collect_buffer_id,
            DROP COLUMN IF EXISTS encrypted_weixin_media_meta,
            DROP COLUMN IF EXISTS weixin_account_id;
        """
    )

    # Drop 8 new tables in reverse dependency order
    op.execute("DROP TABLE IF EXISTS dalton_channel_weixin_collect_items;")
    op.execute("DROP TABLE IF EXISTS dalton_channel_weixin_collect_buffers;")
    op.execute("DROP TABLE IF EXISTS dalton_channel_weixin_login_sessions;")
    op.execute("DROP TABLE IF EXISTS dalton_channel_weixin_account_tokens;")
    op.execute("DROP TABLE IF EXISTS dalton_channel_weixin_audit_logs;")
    op.execute("DROP TABLE IF EXISTS dalton_channel_weixin_accounts;")
    op.execute("DROP TABLE IF EXISTS dalton_channel_weixin_config;")
    op.execute("DROP TABLE IF EXISTS dalton_channel_gateway_lease;")
