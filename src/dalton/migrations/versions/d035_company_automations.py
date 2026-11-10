"""Dalton d035: AU1 company automation schema, scope backfill, and company system identity.

Revision ID: d035
Revises: d031
Create Date: 2026-11-09
"""

from __future__ import annotations

import re
from typing import Any, Sequence, Union

from alembic import op

from src.dalton.autonomy.classification import build_research_tracking_sql_predicate
from src.dalton.company_identity import (
    COMPANY_SYSTEM_USER_ID,
    ensure_company_system_identity_sync,
)

revision: str = "d035"
down_revision: Union[str, None] = "d031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REQUIRED_UPSTREAM_TABLES: tuple[str, ...] = (
    "public.users",
    "public.workspaces",
    "public.automations",
)

_REQUIRED_DALTON_TABLES: tuple[str, ...] = (
    "public.dalton_budget_config",
    "public.dalton_coverage",
    "public.dalton_coverage_units",
    "public.dalton_data_task_defs",
    "public.dalton_channel_session_generations",
    "public.dalton_channel_inbound_events",
)


def _table_exists(bind: Any, qualified_name: str) -> bool:
    return bind.exec_driver_sql("SELECT to_regclass(%s)", (qualified_name,)).scalar() is not None


def _trigger_exists(bind: Any, table_regclass: str, trigger_name: str) -> bool:
    return (
        bind.exec_driver_sql(
            """
            SELECT 1
            FROM pg_trigger
            WHERE tgrelid = to_regclass(%s)
              AND tgname = %s
              AND NOT tgisinternal
            """,
            (table_regclass, trigger_name),
        ).scalar()
        is not None
    )


def _assert_prerequisite_tables(bind: Any) -> None:
    missing_upstream = [t for t in _REQUIRED_UPSTREAM_TABLES if not _table_exists(bind, t)]
    if missing_upstream:
        raise RuntimeError(
            "Upstream Alembic migrations must run before Dalton d035; "
            f"missing tables: {', '.join(missing_upstream)}"
        )
    missing_dalton = [t for t in _REQUIRED_DALTON_TABLES if not _table_exists(bind, t)]
    if missing_dalton:
        raise RuntimeError(
            "Prior Dalton migrations (d001..d031) must run before d035; "
            f"missing tables: {', '.join(missing_dalton)}"
        )


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
    _assert_prerequisite_tables(bind)

    ensure_company_system_identity_sync(bind)

    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS dalton_internal_feishu_groups (
            chat_id VARCHAR(255) PRIMARY KEY CHECK (BTRIM(chat_id) <> ''),
            name TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            is_alert_default BOOLEAN NOT NULL DEFAULT FALSE,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            added_by VARCHAR(255) NOT NULL,
            updated_by VARCHAR(255) NOT NULL,
            added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            audit_log JSONB NOT NULL DEFAULT '[]'::jsonb
                CONSTRAINT dalton_internal_feishu_groups_audit_log_array_check
                CHECK (jsonb_typeof(audit_log) = 'array')
        );
        """
    )

    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS dalton_notify_storage_state (
            state_id INTEGER PRIMARY KEY DEFAULT 1 CHECK (state_id = 1),
            storage_mode VARCHAR(20) NOT NULL DEFAULT 'legacy'
                CHECK (storage_mode IN ('legacy', 'task_row')),
            vault_source VARCHAR(20) NOT NULL DEFAULT 'legacy'
                CHECK (vault_source IN ('legacy', 'company')),
            vault_source_batch_id VARCHAR(128),
            updated_by VARCHAR(255),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    bind.exec_driver_sql(
        """
        INSERT INTO dalton_notify_storage_state (
            state_id, storage_mode, vault_source, vault_source_batch_id
        )
        VALUES (1, 'legacy', 'legacy', NULL)
        ON CONFLICT (state_id) DO NOTHING;
        """
    )

    bind.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS dalton_company_migration_batches (
            batch_id VARCHAR(128) PRIMARY KEY CHECK (BTRIM(batch_id) <> ''),
            stage VARCHAR(32) NOT NULL DEFAULT 'running'
                CHECK (stage IN (
                    'running',
                    'frozen',
                    'switching',
                    'done',
                    'rollback_frozen',
                    'rolled_back'
                )),
            vault_source_user_id VARCHAR(255),
            created_secrets JSONB NOT NULL DEFAULT '[]'::jsonb
                CONSTRAINT dalton_company_migration_batches_created_secrets_array_check
                CHECK (jsonb_typeof(created_secrets) = 'array'),
            preexisting_verified_secrets JSONB NOT NULL DEFAULT '[]'::jsonb
                CONSTRAINT dalton_company_migration_batches_preexisting_secrets_array_check
                CHECK (jsonb_typeof(preexisting_verified_secrets) = 'array'),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    bind.exec_driver_sql(
        """
        ALTER TABLE automations
            ADD COLUMN IF NOT EXISTS scope VARCHAR(32) NOT NULL DEFAULT 'personal',
            ADD COLUMN IF NOT EXISTS notify_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            ADD COLUMN IF NOT EXISTS notify_routes JSONB,
            ADD COLUMN IF NOT EXISTS notify_storage_mode VARCHAR(20) NOT NULL DEFAULT 'legacy',
            ADD COLUMN IF NOT EXISTS config_version INTEGER NOT NULL DEFAULT 1,
            ADD COLUMN IF NOT EXISTS active_version INTEGER NOT NULL DEFAULT 1,
            ADD COLUMN IF NOT EXISTS created_by VARCHAR(255),
            ADD COLUMN IF NOT EXISTS updated_by VARCHAR(255),
            ADD COLUMN IF NOT EXISTS audit_history JSONB NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS deleted_by VARCHAR(255),
            ADD COLUMN IF NOT EXISTS pre_delete_desired_status VARCHAR(20);
        """
    )

    bind.exec_driver_sql("ALTER TABLE automations DROP CONSTRAINT IF EXISTS automations_scope_check")
    bind.exec_driver_sql(
        """
        ALTER TABLE automations ADD CONSTRAINT automations_scope_check
            CHECK (scope IN ('personal', 'research_tracking', 'company'))
        """
    )

    bind.exec_driver_sql(
        "ALTER TABLE automations DROP CONSTRAINT IF EXISTS automations_notify_storage_mode_check"
    )
    bind.exec_driver_sql(
        """
        ALTER TABLE automations ADD CONSTRAINT automations_notify_storage_mode_check
            CHECK (notify_storage_mode IN ('legacy', 'task_row'))
        """
    )

    bind.exec_driver_sql(
        "ALTER TABLE automations DROP CONSTRAINT IF EXISTS automations_company_notify_mode_check"
    )
    bind.exec_driver_sql(
        """
        ALTER TABLE automations ADD CONSTRAINT automations_company_notify_mode_check
            CHECK (scope <> 'company' OR notify_storage_mode = 'task_row')
        """
    )

    bind.exec_driver_sql(
        "ALTER TABLE automations DROP CONSTRAINT IF EXISTS automations_company_owner_check"
    )
    bind.exec_driver_sql(
        f"""
        ALTER TABLE automations ADD CONSTRAINT automations_company_owner_check
            CHECK (
                (scope = 'company' AND user_id = '{COMPANY_SYSTEM_USER_ID}')
                OR (scope <> 'company' AND user_id <> '{COMPANY_SYSTEM_USER_ID}')
            )
        """
    )

    bind.exec_driver_sql(
        "ALTER TABLE automations DROP CONSTRAINT IF EXISTS automations_notify_routes_type_check"
    )
    bind.exec_driver_sql(
        """
        ALTER TABLE automations ADD CONSTRAINT automations_notify_routes_type_check
            CHECK (notify_routes IS NULL OR jsonb_typeof(notify_routes) = 'array')
        """
    )

    bind.exec_driver_sql(
        "ALTER TABLE automations DROP CONSTRAINT IF EXISTS automations_audit_history_type_check"
    )
    bind.exec_driver_sql(
        """
        ALTER TABLE automations ADD CONSTRAINT automations_audit_history_type_check
            CHECK (jsonb_typeof(audit_history) = 'array')
        """
    )

    bind.exec_driver_sql(
        "ALTER TABLE automations DROP CONSTRAINT IF EXISTS automations_version_positive_check"
    )
    bind.exec_driver_sql(
        """
        ALTER TABLE automations ADD CONSTRAINT automations_version_positive_check
            CHECK (config_version >= 1 AND active_version >= 1)
        """
    )

    bind.exec_driver_sql(
        "ALTER TABLE automations DROP CONSTRAINT IF EXISTS automations_pre_delete_status_check"
    )
    bind.exec_driver_sql(
        """
        ALTER TABLE automations ADD CONSTRAINT automations_pre_delete_status_check
            CHECK (
                pre_delete_desired_status IS NULL
                OR pre_delete_desired_status IN ('active', 'paused')
            )
        """
    )

    # Preserve existing updated_at timestamps while backfilling scope.
    has_updated_trigger = _trigger_exists(bind, "public.automations", "trg_automations_updated_at")
    if has_updated_trigger:
        bind.exec_driver_sql("ALTER TABLE automations DISABLE TRIGGER trg_automations_updated_at")
    try:
        rt_predicate = build_research_tracking_sql_predicate("a")
        bind.exec_driver_sql(
            f"""
            UPDATE automations a
            SET scope = 'research_tracking'
            WHERE a.scope = 'personal'
              AND {rt_predicate}
            """
        )
    finally:
        if has_updated_trigger:
            bind.exec_driver_sql("ALTER TABLE automations ENABLE TRIGGER trg_automations_updated_at")

    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_automations_user_file_name")
    bind.exec_driver_sql(
        """
        CREATE UNIQUE INDEX idx_automations_user_file_name
            ON automations (user_id, file_name)
            WHERE deleted_at IS NULL
        """
    )
    bind.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS idx_automations_scope_deleted
            ON automations (scope, deleted_at, created_at DESC)
        """
    )

    bind.exec_driver_sql(
        """
        ALTER TABLE dalton_data_task_defs
            ADD COLUMN IF NOT EXISTS notify_routes JSONB,
            ADD COLUMN IF NOT EXISTS notify_storage_mode VARCHAR(20) NOT NULL DEFAULT 'legacy',
            ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS deleted_by VARCHAR(255),
            ADD COLUMN IF NOT EXISTS pre_delete_enabled BOOLEAN,
            ADD COLUMN IF NOT EXISTS migration_state JSONB NOT NULL DEFAULT '{}'::jsonb;
        """
    )

    bind.exec_driver_sql(
        "ALTER TABLE dalton_data_task_defs DROP CONSTRAINT IF EXISTS dalton_data_task_defs_notify_storage_mode_check"
    )
    bind.exec_driver_sql(
        """
        ALTER TABLE dalton_data_task_defs ADD CONSTRAINT dalton_data_task_defs_notify_storage_mode_check
            CHECK (notify_storage_mode IN ('legacy', 'task_row'))
        """
    )

    bind.exec_driver_sql(
        "ALTER TABLE dalton_data_task_defs DROP CONSTRAINT IF EXISTS dalton_data_task_defs_notify_routes_type_check"
    )
    bind.exec_driver_sql(
        """
        ALTER TABLE dalton_data_task_defs ADD CONSTRAINT dalton_data_task_defs_notify_routes_type_check
            CHECK (notify_routes IS NULL OR jsonb_typeof(notify_routes) = 'array')
        """
    )

    bind.exec_driver_sql(
        "ALTER TABLE dalton_data_task_defs DROP CONSTRAINT IF EXISTS dalton_data_task_defs_migration_state_type_check"
    )
    bind.exec_driver_sql(
        """
        ALTER TABLE dalton_data_task_defs ADD CONSTRAINT dalton_data_task_defs_migration_state_type_check
            CHECK (jsonb_typeof(migration_state) = 'object')
        """
    )


def _assert_safe_downgrade(bind: Any) -> None:
    blockers: list[str] = []

    company_autos = int(
        bind.exec_driver_sql(
            """
            SELECT COUNT(*)
            FROM automations
            WHERE scope = 'company' OR user_id = %s
            """,
            (COMPANY_SYSTEM_USER_ID,),
        ).scalar()
        or 0
    )
    if company_autos > 0:
        blockers.append(f"automations has {company_autos} company-scoped/company-owned row(s)")

    deleted_autos = int(
        bind.exec_driver_sql(
            """
            SELECT COUNT(*)
            FROM automations
            WHERE deleted_at IS NOT NULL
               OR deleted_by IS NOT NULL
               OR pre_delete_desired_status IS NOT NULL
            """
        ).scalar()
        or 0
    )
    if deleted_autos > 0:
        blockers.append(f"automations has {deleted_autos} soft-deleted row(s)")

    deleted_data_tasks = int(
        bind.exec_driver_sql(
            """
            SELECT COUNT(*)
            FROM dalton_data_task_defs
            WHERE deleted_at IS NOT NULL
               OR deleted_by IS NOT NULL
               OR pre_delete_enabled IS NOT NULL
            """
        ).scalar()
        or 0
    )
    if deleted_data_tasks > 0:
        blockers.append(f"dalton_data_task_defs has {deleted_data_tasks} soft-deleted row(s)")

    auto_notify_rows = int(
        bind.exec_driver_sql(
            """
            SELECT COUNT(*)
            FROM automations
            WHERE notify_storage_mode <> 'legacy'
               OR notify_routes IS NOT NULL
               OR notify_enabled IS FALSE
            """
        ).scalar()
        or 0
    )
    if auto_notify_rows > 0:
        blockers.append(f"automations has {auto_notify_rows} row-level notify state row(s)")

    data_notify_rows = int(
        bind.exec_driver_sql(
            """
            SELECT COUNT(*)
            FROM dalton_data_task_defs
            WHERE notify_storage_mode <> 'legacy'
               OR notify_routes IS NOT NULL
            """
        ).scalar()
        or 0
    )
    if data_notify_rows > 0:
        blockers.append(
            f"dalton_data_task_defs has {data_notify_rows} row-level notify state row(s)"
        )

    if _table_exists(bind, "public.dalton_notify_storage_state"):
        non_legacy_state = int(
            bind.exec_driver_sql(
                """
                SELECT COUNT(*)
                FROM dalton_notify_storage_state
                WHERE storage_mode <> 'legacy'
                   OR vault_source <> 'legacy'
                   OR vault_source_batch_id IS NOT NULL
                """
            ).scalar()
            or 0
        )
        if non_legacy_state > 0:
            blockers.append("dalton_notify_storage_state is not in initial legacy mode")

    if _table_exists(bind, "public.dalton_company_migration_batches"):
        batches_cnt = int(
            bind.exec_driver_sql(
                "SELECT COUNT(*) FROM dalton_company_migration_batches"
            ).scalar()
            or 0
        )
        if batches_cnt > 0:
            blockers.append(
                f"dalton_company_migration_batches retains {batches_cnt} batch record(s)"
            )

    data_migration_rows = int(
        bind.exec_driver_sql(
            """
            SELECT COUNT(*)
            FROM dalton_data_task_defs
            WHERE migration_state <> '{}'::jsonb
            """
        ).scalar()
        or 0
    )
    if data_migration_rows > 0:
        blockers.append(
            f"dalton_data_task_defs retains saved migration_state on {data_migration_rows} row(s)"
        )

    auto_migration_meta = int(
        bind.exec_driver_sql(
            """
            SELECT COUNT(*)
            FROM automations
            WHERE COALESCE(metadata, '{}'::jsonb) ?| ARRAY[
                'pre_company_migration',
                'company_migration',
                'company_migration_batches'
            ]
            """
        ).scalar()
        or 0
    )
    if auto_migration_meta > 0:
        blockers.append(
            f"automations.metadata retains migration source identity on {auto_migration_meta} row(s)"
        )

    auto_history_rows = int(
        bind.exec_driver_sql(
            """
            SELECT COUNT(*)
            FROM automations
            WHERE audit_history <> '[]'::jsonb
               OR config_version > 1
               OR active_version > 1
            """
        ).scalar()
        or 0
    )
    if auto_history_rows > 0:
        blockers.append(
            f"automations retains modification audit_history on {auto_history_rows} row(s)"
        )

    if _table_exists(bind, "public.dalton_internal_feishu_groups"):
        feishu_groups_cnt = int(
            bind.exec_driver_sql(
                "SELECT COUNT(*) FROM dalton_internal_feishu_groups"
            ).scalar()
            or 0
        )
        if feishu_groups_cnt > 0:
            blockers.append(
                f"dalton_internal_feishu_groups retains {feishu_groups_cnt} group(s)"
            )

    dup_file_names = int(
        bind.exec_driver_sql(
            """
            SELECT COUNT(*)
            FROM (
                SELECT 1
                FROM automations
                GROUP BY user_id, file_name
                HAVING COUNT(*) > 1
            ) dup
            """
        ).scalar()
        or 0
    )
    if dup_file_names > 0:
        blockers.append(
            f"automations has {dup_file_names} duplicate (user_id, file_name) pair(s) "
            "preventing unique index restoration"
        )

    _safe_ident_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    user_ref_tables: list[tuple[str, str, str]] = [
        ("public.computers", "computers", "user_id"),
        ("public.workspaces", "workspaces", "user_id"),
        ("public.watchlists", "watchlists", "user_id"),
        ("public.watchlist_items", "watchlist_items", "user_id"),
        ("public.user_portfolios", "user_portfolios", "user_id"),
        ("public.user_preferences", "user_preferences", "user_id"),
        ("public.user_api_keys", "user_api_keys", "user_id"),
        ("public.user_oauth_tokens", "user_oauth_tokens", "user_id"),
        ("public.user_vault_secrets", "user_vault_secrets", "user_id"),
        ("public.user_mcp_servers", "user_mcp_servers", "user_id"),
        ("public.user_mcp_builtin_disables", "user_mcp_builtin_disables", "user_id"),
        ("public.user_skills", "user_skills", "user_id"),
        ("public.user_plugins", "user_plugins", "user_id"),
        ("public.dalton_budget_reservations", "dalton_budget_reservations", "user_id"),
        ("public.dalton_budget_task_caps", "dalton_budget_task_caps", "user_id"),
        ("public.dalton_notify_deliveries", "dalton_notify_deliveries", "user_id"),
        ("public.dalton_channel_automation_runs", "dalton_channel_automation_runs", "user_id"),
    ]
    checked_pairs: set[tuple[str, str]] = {
        ("automations", "user_id"),
        ("dalton_budget_config", "user_id"),
        *{(tbl, col) for _, tbl, col in user_ref_tables},
    }
    fk_rows = bind.exec_driver_sql(
        """
        SELECT c.relname, a.attname
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = con.conkey[1]
        WHERE con.contype = 'f'
          AND con.confrelid = to_regclass('public.users')
          AND array_length(con.conkey, 1) = 1
          AND n.nspname = 'public'
        ORDER BY c.relname, a.attname
        """
    ).fetchall()
    for fk_tbl, fk_col in fk_rows:
        tbl_s, col_s = str(fk_tbl), str(fk_col)
        if (tbl_s, col_s) not in checked_pairs and _safe_ident_re.match(tbl_s) and _safe_ident_re.match(col_s):
            checked_pairs.add((tbl_s, col_s))
            user_ref_tables.append((f"public.{tbl_s}", tbl_s, col_s))

    for reg_name, table_name, col_name in user_ref_tables:
        if not _table_exists(bind, reg_name):
            continue
        if not (_safe_ident_re.match(table_name) and _safe_ident_re.match(col_name)):
            continue
        ref_cnt = int(
            bind.exec_driver_sql(
                f'SELECT COUNT(*) FROM "{table_name}" WHERE "{col_name}" = %s',
                (COMPANY_SYSTEM_USER_ID,),
            ).scalar()
            or 0
        )
        if ref_cnt > 0:
            blockers.append(
                f"{table_name} still references {COMPANY_SYSTEM_USER_ID} ({ref_cnt} row(s))"
            )

    if blockers:
        raise RuntimeError(
            "Refusing destructive downgrade of Dalton d035 because dependent state exists: "
            + "; ".join(blockers)
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
    _assert_prerequisite_tables(bind)
    _assert_safe_downgrade(bind)

    bind.exec_driver_sql(
        "ALTER TABLE dalton_data_task_defs DROP CONSTRAINT IF EXISTS dalton_data_task_defs_migration_state_type_check"
    )
    bind.exec_driver_sql(
        "ALTER TABLE dalton_data_task_defs DROP CONSTRAINT IF EXISTS dalton_data_task_defs_notify_routes_type_check"
    )
    bind.exec_driver_sql(
        "ALTER TABLE dalton_data_task_defs DROP CONSTRAINT IF EXISTS dalton_data_task_defs_notify_storage_mode_check"
    )
    bind.exec_driver_sql(
        """
        ALTER TABLE dalton_data_task_defs
            DROP COLUMN IF EXISTS migration_state,
            DROP COLUMN IF EXISTS pre_delete_enabled,
            DROP COLUMN IF EXISTS deleted_by,
            DROP COLUMN IF EXISTS deleted_at,
            DROP COLUMN IF EXISTS notify_storage_mode,
            DROP COLUMN IF EXISTS notify_routes;
        """
    )

    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_automations_scope_deleted")
    bind.exec_driver_sql("DROP INDEX IF EXISTS idx_automations_user_file_name")
    bind.exec_driver_sql(
        """
        CREATE UNIQUE INDEX idx_automations_user_file_name
            ON automations (user_id, file_name)
        """
    )

    bind.exec_driver_sql(
        "ALTER TABLE automations DROP CONSTRAINT IF EXISTS automations_pre_delete_status_check"
    )
    bind.exec_driver_sql(
        "ALTER TABLE automations DROP CONSTRAINT IF EXISTS automations_version_positive_check"
    )
    bind.exec_driver_sql(
        "ALTER TABLE automations DROP CONSTRAINT IF EXISTS automations_audit_history_type_check"
    )
    bind.exec_driver_sql(
        "ALTER TABLE automations DROP CONSTRAINT IF EXISTS automations_notify_routes_type_check"
    )
    bind.exec_driver_sql(
        "ALTER TABLE automations DROP CONSTRAINT IF EXISTS automations_company_owner_check"
    )
    bind.exec_driver_sql(
        "ALTER TABLE automations DROP CONSTRAINT IF EXISTS automations_company_notify_mode_check"
    )
    bind.exec_driver_sql(
        "ALTER TABLE automations DROP CONSTRAINT IF EXISTS automations_notify_storage_mode_check"
    )
    bind.exec_driver_sql(
        "ALTER TABLE automations DROP CONSTRAINT IF EXISTS automations_scope_check"
    )
    bind.exec_driver_sql(
        """
        ALTER TABLE automations
            DROP COLUMN IF EXISTS pre_delete_desired_status,
            DROP COLUMN IF EXISTS deleted_by,
            DROP COLUMN IF EXISTS deleted_at,
            DROP COLUMN IF EXISTS audit_history,
            DROP COLUMN IF EXISTS updated_by,
            DROP COLUMN IF EXISTS created_by,
            DROP COLUMN IF EXISTS active_version,
            DROP COLUMN IF EXISTS config_version,
            DROP COLUMN IF EXISTS notify_storage_mode,
            DROP COLUMN IF EXISTS notify_routes,
            DROP COLUMN IF EXISTS notify_enabled,
            DROP COLUMN IF EXISTS scope;
        """
    )

    bind.exec_driver_sql("DROP TABLE IF EXISTS dalton_company_migration_batches")
    bind.exec_driver_sql("DROP TABLE IF EXISTS dalton_notify_storage_state")
    bind.exec_driver_sql("DROP TABLE IF EXISTS dalton_internal_feishu_groups")

    bind.exec_driver_sql(
        "DELETE FROM dalton_budget_config WHERE user_id = %s",
        (COMPANY_SYSTEM_USER_ID,),
    )
    bind.exec_driver_sql(
        "DELETE FROM users WHERE user_id = %s",
        (COMPANY_SYSTEM_USER_ID,),
    )
