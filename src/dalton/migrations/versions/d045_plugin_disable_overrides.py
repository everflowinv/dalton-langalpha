"""Dalton d045: per-user bundled plugin re-enable overrides (`dalton_plugin_disable_overrides`).

Supports splitting ``dalton-sec`` out of the legacy ``dalton-data`` bundled
plugin without breaking existing users who disabled the legacy ``dalton-data``
bundle:

- Users with ``('bundle', 'dalton-data')`` in ``user_mcp_builtin_disables``
  and no matching row here inherit the disable for ``dalton-sec`` across the
  plugin listing, ``PATCH /api/v1/plugins/{name}/enabled``, and MCP tool
  resolution.
- Explicitly enabling ``dalton-sec`` inserts ``(user_id, 'dalton-sec', NOW())``
  here while leaving ``dalton-data`` disabled.
- Disabling ``dalton-sec`` deletes the override row and writes
  ``('bundle', 'dalton-sec')`` into ``user_mcp_builtin_disables``.

Backward-compatible with previous backend releases: only creates a new
Dalton-owned table and never alters existing upstream or Dalton tables.

Revision ID: d045
Revises: d044
Create Date: 2026-11-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d045"
down_revision: Union[str, None] = "d044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_plugin_disable_overrides (
            user_id      VARCHAR(255) NOT NULL,
            bundle_name  VARCHAR(255) NOT NULL,
            reenabled_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, bundle_name)
        )
        """
    )


def downgrade() -> None:
    ctx = op.get_context()
    if getattr(ctx, "as_sql", False):
        raise RuntimeError(
            "d045 downgrade cannot run in offline SQL mode because it must verify "
            "that no user_mcp_builtin_disables('bundle', 'dalton-sec') rows and no "
            "dalton_plugin_disable_overrides rows exist before dropping the table "
            "(rolling back to Batch 2a with those rows present would silently "
            "re-enable disabled dalton-sec plugins or lose explicit re-enable overrides)."
        )

    conn = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")

    has_disables = (
        conn.execute(
            sa.text("SELECT to_regclass('public.user_mcp_builtin_disables')")
        ).scalar()
        is not None
    )
    has_overrides = (
        conn.execute(
            sa.text("SELECT to_regclass('public.dalton_plugin_disable_overrides')")
        ).scalar()
        is not None
    )

    # Lock both tables in SHARE ROW EXCLUSIVE mode before counting rows so no
    # concurrent backend write can commit a new ('bundle', 'dalton-sec') disable
    # or override row between the count check and DROP TABLE in this transaction.
    # Deterministic order matches set_account_disable (disables -> overrides).
    if has_disables:
        op.execute("LOCK TABLE user_mcp_builtin_disables IN SHARE ROW EXCLUSIVE MODE")
    if has_overrides:
        op.execute(
            "LOCK TABLE dalton_plugin_disable_overrides IN SHARE ROW EXCLUSIVE MODE"
        )

    sec_disable_count = (
        int(
            conn.execute(
                sa.text(
                    """
                    SELECT COUNT(*)
                    FROM user_mcp_builtin_disables
                    WHERE kind = 'bundle' AND name = 'dalton-sec'
                    """
                )
            ).scalar()
            or 0
        )
        if has_disables
        else 0
    )

    override_count = (
        int(
            conn.execute(
                sa.text("SELECT COUNT(*) FROM dalton_plugin_disable_overrides")
            ).scalar()
            or 0
        )
        if has_overrides
        else 0
    )

    if sec_disable_count > 0 or override_count > 0:
        raise RuntimeError(
            "Refusing to downgrade d045 (dalton_plugin_disable_overrides): "
            f"found {sec_disable_count} ('bundle', 'dalton-sec') disable row(s) in "
            f"user_mcp_builtin_disables and {override_count} re-enable override row(s) "
            "in dalton_plugin_disable_overrides. Rolling back to Batch 2a while these "
            "rows exist would cause older backends (which map dalton_sec under "
            "'dalton-data') to silently re-enable SEC for accounts that disabled "
            "'dalton-sec' or lose explicit re-enable overrides. Migrate or clear those "
            "rows explicitly before downgrading."
        )

    op.execute("DROP TABLE IF EXISTS dalton_plugin_disable_overrides")
