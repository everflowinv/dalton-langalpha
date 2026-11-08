"""Dalton d030: CH-1 channel gateway persistence tables (Feishu, Discord, Weixin).

Revision ID: d030
Revises: d029
Create Date: 2026-11-08
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "d030"
down_revision: Union[str, None] = "d029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

    # 表 1: 平台账号与 Dalton 用户绑定表
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_identities (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id VARCHAR(255) NOT NULL,
            platform VARCHAR(32) NOT NULL,               -- 'feishu' | 'discord' | 'weixin'
            platform_user_id VARCHAR(255) NOT NULL,      -- 飞书 open_id / Discord user.id / 微信 ilink_user_id
            platform_username VARCHAR(255),
            platform_tenant_key VARCHAR(255),
            weixin_context_token TEXT,                   -- 微信最近一次入站 context_token 持久化兜底
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (platform, platform_user_id),
            UNIQUE (user_id, platform)
        );
        """
    )

    # 表 2: 一次性绑定码表（显式持久化 platform，防跨平台错投或误作废）
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_bind_codes (
            code_hash VARCHAR(64) PRIMARY KEY,           -- SHA-256(大写绑定码)
            user_id VARCHAR(255) NOT NULL,
            platform VARCHAR(32) NOT NULL,               -- 'feishu' | 'discord' | 'weixin'
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ,
            revoked_reason VARCHAR(64),                  -- 如 'sent_in_group' | 'superseded'
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_dalton_channel_bind_codes_active_user_platform
            ON dalton_channel_bind_codes (user_id, platform)
            WHERE used_at IS NULL AND revoked_reason IS NULL;
        """
    )

    # 表 3: 用户频道设置表（对应 .agents/user/channels.json，version 必为字符串如 'v1'）
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_user_settings (
            user_id VARCHAR(255) PRIMARY KEY,
            version TEXT NOT NULL DEFAULT 'v1',          -- 对齐 src/server/services/channel_settings.py:353
            apps JSONB NOT NULL DEFAULT '{}'::jsonb,     -- 结构对齐 channel_settings.py:60-79 (_App / _Binding / _AgentMessages)
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    # 表 4: 群聊/频道与工作区绑定表（仅保存群级工作区路由，不存单线程监听）
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_chat_bindings (
            platform VARCHAR(32) NOT NULL,               -- 'feishu' | 'discord'
            chat_address VARCHAR(255) NOT NULL,          -- 如 'feishu:oc_xxx' | 'discord:1234567890'
            chat_name VARCHAR(255),
            workspace_id UUID REFERENCES workspaces(workspace_id) ON DELETE SET NULL,
            bound_by_user_id VARCHAR(255) NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (platform, chat_address)
        );
        """
    )

    # 表 5: 会话代数（generation）、私聊工作区覆盖与待处理 HITL 中断表（代数推进规则见 §1.4 I-2 / I-3）
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_session_generations (
            platform VARCHAR(32) NOT NULL,               -- 'feishu' | 'discord' | 'weixin'
            scope_key VARCHAR(255) NOT NULL,             -- 私聊为 'dm:<platform_user_id>'；群聊为 'chat:<chat_id>' 或 'chat:<chat_id>:thread:<thread_id>'
            dalton_user_id VARCHAR(255) NOT NULL,
            generation INTEGER NOT NULL DEFAULT 1,       -- 推进规则唯一定义于 §1.4 I-2 / I-3
            override_workspace_id UUID REFERENCES workspaces(workspace_id) ON DELETE SET NULL, -- 私聊通过 /workspace 切换的工作区
            pending_interrupt JSONB,                     -- 当前会话待处理的 HITL 中断: {interrupt_id, thread_id, run_id, category, action_requests}
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (platform, scope_key, dalton_user_id)
        );
        """
    )

    # 表 6: 线程级后台 /watch 监听表（以真实 Dalton thread_id 为主键，同群多人或 /new 后旧线程互不覆盖）
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_thread_watches (
            thread_id UUID PRIMARY KEY,                  -- 真实的 Dalton conversation_thread_id
            dalton_user_id VARCHAR(255) NOT NULL,        -- 订阅 /watch 及状态对账使用的 X-User-Id（线程主人）
            platform VARCHAR(32) NOT NULL,               -- 'feishu' | 'discord' | 'weixin'
            target_address VARCHAR(255) NOT NULL,        -- 回报投递目标地址，如 'feishu:oc_xxx' 或 'feishu:@me'
            scope_key VARCHAR(255) NOT NULL,
            generation INTEGER NOT NULL,
            last_seen_turn_index INTEGER NOT NULL DEFAULT 0, -- 已扫描入库的最新 turn_index，供离线 >15 分钟补齐通知/唤醒回合
            watch_active BOOLEAN NOT NULL DEFAULT TRUE,  -- 仅当满足 §1.3.4 全部 7 项关闭条件时置为 FALSE
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_channel_thread_watches_active
            ON dalton_channel_thread_watches (watch_active)
            WHERE watch_active = TRUE;
        """
    )

    # 表 7: 入站消息幂等与全生命周期恢复账本（支撑 pending / streaming / steered / pending_resubmit / retry / fallback 恢复）
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_inbound_events (
            platform VARCHAR(32) NOT NULL,
            message_id VARCHAR(255) NOT NULL,
            request_key UUID NOT NULL UNIQUE,            -- 首回合或 :turn: 改发请求编号
            retry_request_key UUID UNIQUE,               -- 409 cancelled && retryable 时生成的 :retry: 请求编号
            resubmitted_request_key UUID UNIQUE,         -- 退回插话在原回合 completed 时生成的 :returned: 请求编号
            steered_into_run_id VARCHAR(255),            -- 当作为插话被接纳时记录目标 run_id
            dalton_user_id VARCHAR(255),
            chat_type VARCHAR(16) NOT NULL,              -- 'dm' | 'group'
            chat_address VARCHAR(255) NOT NULL,          -- 如 'feishu:oc_xxx' | 'feishu:@me'
            thread_scope_key VARCHAR(255) NOT NULL,
            external_thread_id VARCHAR(512),
            workspace_id UUID,
            thread_id UUID,
            run_id VARCHAR(255),
            turn_index INTEGER,                          -- SSE metadata 给出的 turn_index，供 /messages/replay 直接定位
            status VARCHAR(32) NOT NULL DEFAULT 'pending',
            fallback_pending BOOLEAN NOT NULL DEFAULT FALSE,
            sent_to_current_count INTEGER NOT NULL DEFAULT 0, -- 当前 run_id 已成功向本会话发送非空文字的次数
            raw_payload JSONB NOT NULL,                  -- 原始入站事件载荷（不含冗余日志）
            uploaded_attachments JSONB,                  -- 已上传至目标工作区的附件引用 [{file_name, content_type, size, workspace_path, image_data_url}]
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (platform, message_id)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_channel_inbound_events_recovery
            ON dalton_channel_inbound_events (status, fallback_pending)
            WHERE status IN ('pending', 'accepted', 'streaming', 'steered', 'pending_resubmit')
               OR fallback_pending = TRUE;
        """
    )

    # 表 8: 后台唤醒回合（Subagent / Background Command / Wakeup / 委派分析师回报）追踪表
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_background_runs (
            run_id VARCHAR(255) PRIMARY KEY,
            thread_id UUID NOT NULL,
            dalton_user_id VARCHAR(255) NOT NULL,
            platform VARCHAR(32) NOT NULL,
            target_address VARCHAR(255) NOT NULL,
            turn_index INTEGER,
            status VARCHAR(32) NOT NULL DEFAULT 'streaming', -- 'streaming' | 'completed' | 'failed' | 'cancelled'
            fallback_pending BOOLEAN NOT NULL DEFAULT TRUE,
            sent_to_current_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_channel_bg_runs_recovery
            ON dalton_channel_background_runs (fallback_pending)
            WHERE fallback_pending = TRUE;
        """
    )

    # 表 9: 统一出站投递幂等与分段进度账本（覆盖 /agent/send、回放兜底、HITL 中断问句、自动化投递、系统提示）
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_deliveries (
            delivery_key VARCHAR(512) PRIMARY KEY,       -- 'tool:<run_id>:<tool_call_id>' | 'fallback:<run_id>' | 'interrupt:<run_id>:<interrupt_id>' | 'auto:<execution_id>:<entry>:<address>' | 'notice:<id>'
            kind VARCHAR(32) NOT NULL,                   -- 'agent_send' | 'fallback' | 'interrupt' | 'automation' | 'notice'
            user_id VARCHAR(255) NOT NULL,
            thread_id UUID,
            run_id VARCHAR(255),
            execution_id UUID,
            is_current BOOLEAN NOT NULL DEFAULT FALSE,   -- 是否发往当前回合来源会话
            has_non_empty_text BOOLEAN NOT NULL DEFAULT FALSE, -- 是否携带非空正文文本
            platform VARCHAR(32) NOT NULL,
            target_address VARCHAR(255) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'in_flight', -- 'in_flight' | 'sent' | 'partial' | 'failed' | 'unknown'
            total_chunks INTEGER NOT NULL DEFAULT 0,
            sent_chunks INTEGER NOT NULL DEFAULT 0,
            platform_message_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            files_status JSONB NOT NULL DEFAULT '[]'::jsonb,
            first_response_json JSONB,                   -- 持久化首次完成（sent/partial）的完整响应 JSON，供 duplicate: true 原样返回
            error_code VARCHAR(64),
            error_message TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dalton_channel_deliveries_run_id
            ON dalton_channel_deliveries (run_id)
            WHERE run_id IS NOT NULL;
        """
    )

    # 表 10: 自动化投递运行登记与目标结算表（字段严格对齐 src/server/services/automation_delivery.py:202-209, 591-605）
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_automation_runs (
            execution_id UUID PRIMARY KEY,
            user_id VARCHAR(255) NOT NULL,
            workspace_id UUID,
            thread_id UUID,                                   -- 启动或 finish 时可选传入 (automation_delivery.py:208, 592)
            automation_name TEXT NOT NULL DEFAULT '',         -- 对应请求字段 automation_name (automation_delivery.py:205)
            entries JSONB NOT NULL DEFAULT '[]'::jsonb,       -- 对应请求字段 entries (automation_delivery.py:206)
            status VARCHAR(32) NOT NULL DEFAULT 'registered', -- 'registered' | 'finishing' | 'finished'
            finish_status VARCHAR(32),                        -- finish 请求的 status: 'completed' | 'failed' | 'stopped'
            final_text TEXT,                                  -- finish 请求的 final_text (最长 20000 字符)
            targets JSONB NOT NULL DEFAULT '[]'::jsonb,       -- [{entry, address, name, ok, message, reached, via, error, delivery_key}]
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    # 表 11: 个人微信单实例 Bot 加密凭据与长轮询游标真值表（数据库为唯一真值来源）
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_channel_weixin_state (
            singleton_id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (singleton_id = 1),
            ilink_bot_id VARCHAR(255) NOT NULL,
            owner_user_id VARCHAR(255) NOT NULL,         -- 发起扫码的 admin 用户 user_id
            encrypted_bot_token BYTEA NOT NULL,          -- pgp_sym_encrypt(bot_token, get_encryption_key())，见 src/server/database/encryption.py:10
            base_url VARCHAR(512) NOT NULL DEFAULT 'https://ilinkai.weixin.qq.com',
            get_updates_buf TEXT NOT NULL DEFAULT '',    -- iLink getupdates 同步游标
            status VARCHAR(32) NOT NULL DEFAULT 'active', -- 'active' | 'session_expired'
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS dalton_channel_weixin_state;")
    op.execute("DROP TABLE IF EXISTS dalton_channel_automation_runs;")
    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_deliveries_run_id;")
    op.execute("DROP TABLE IF EXISTS dalton_channel_deliveries;")
    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_bg_runs_recovery;")
    op.execute("DROP TABLE IF EXISTS dalton_channel_background_runs;")
    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_inbound_events_recovery;")
    op.execute("DROP TABLE IF EXISTS dalton_channel_inbound_events;")
    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_thread_watches_active;")
    op.execute("DROP TABLE IF EXISTS dalton_channel_thread_watches;")
    op.execute("DROP TABLE IF EXISTS dalton_channel_session_generations;")
    op.execute("DROP TABLE IF EXISTS dalton_channel_chat_bindings;")
    op.execute("DROP TABLE IF EXISTS dalton_channel_user_settings;")
    op.execute("DROP INDEX IF EXISTS idx_dalton_channel_bind_codes_active_user_platform;")
    op.execute("DROP TABLE IF EXISTS dalton_channel_bind_codes;")
    op.execute("DROP TABLE IF EXISTS dalton_channel_identities;")
