"""Add dalton_report_issues: the controlled server-side bug report inbox (E3).

The upstream self-improve submission path and the sandbox's GitHub
credentials are gone (upstream #595), so a user-approved bug report now goes
to an internal table the team triages — never to GitHub, and the sandbox
never sees a `gh` login. ``d020`` belongs to the C line; this migration
chains behind it so the Dalton line stays single-headed.
"""

from __future__ import annotations

from alembic import op

revision = "d021"
down_revision = "d020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_report_issues (
            issue_id BIGSERIAL PRIMARY KEY,
            ref TEXT NOT NULL UNIQUE,
            user_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL DEFAULT '',
            thread_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'normal',
            status TEXT NOT NULL DEFAULT 'open',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_dalton_report_issues_created
            ON dalton_report_issues (created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dalton_report_issues;")
