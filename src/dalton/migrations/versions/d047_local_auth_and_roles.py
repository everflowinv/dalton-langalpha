"""Dalton d047: local authentication, sessions, bootstrap state, user roles, and credential holder.

Adds Dalton-owned tables for standalone core local authentication and role management:
- ``dalton_user_roles``: per-user access tier (``admin``, ``internal``, ``external``) keyed by ``users.user_id``
- ``dalton_credential_holder``: singleton team credential holder pointing to ``users.user_id``
- ``dalton_local_credentials``: local password hashes (scrypt) and lockout state
- ``dalton_local_sessions``: stable-``session_id`` sessions with current refresh hash & password version
- ``dalton_local_refresh_tokens``: rotated-out refresh token history for bounded once-only replay detection
- ``dalton_bootstrap_state``: permanent singleton lock once first admin bootstrap completes
- ``dalton_bootstrap_tokens``: singleton active bootstrap setup token hash, expiry, and failure counter
- ``dalton_auth_attempts``: dual-dimension (IP + account) login/password-change rate-limit & backoff counters

Also adds ``tier`` column to ``dalton_invites`` (Dalton-owned table from ``d016``).
Touches no upstream tables.

Downgrade refuses to run if ``dalton_bootstrap_state`` already has a completed bootstrap row,
so rolling back and re-applying ``d047`` can never erase the permanent bootstrap lock.

Revision ID: d047
Revises: d045
Create Date: 2026-11-23
"""

from __future__ import annotations

import hashlib
from typing import Sequence, Union

from alembic import op

revision: str = "d047"
down_revision: Union[str, None] = "d045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same advisory lock key used by ``src/dalton/auth/bootstrap.py`` (computed inline with
# stdlib hashlib so this migration never imports application code).
_BOOTSTRAP_ADVISORY_LOCK_KEY: int = int.from_bytes(
    hashlib.sha256(b"dalton:bootstrap:admin:v1").digest()[:8],
    byteorder="big",
    signed=True,
)


def upgrade() -> None:
    # 1. Per-user access roles keyed by users.user_id
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_user_roles (
            user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            tier TEXT NOT NULL CHECK (tier IN ('admin', 'internal', 'external')),
            granted_by TEXT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_dalton_user_roles_tier
            ON dalton_user_roles (tier);
        """
    )

    # 2. Singleton team model credential holder
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_credential_holder (
            singleton_id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (singleton_id = 1),
            holder_user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
            updated_by TEXT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    # 3. Local account password credentials and per-account lockout tracking
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_local_credentials (
            user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            email TEXT NOT NULL UNIQUE CHECK (email = LOWER(email)),
            username TEXT NULL UNIQUE CHECK (username IS NULL OR username = LOWER(username)),
            password_hash TEXT NOT NULL,
            password_changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            failed_attempts INTEGER NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
            locked_until TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    # 4. Stable-sid local sessions (refresh_token_hash rotates in place; only SHA-256 hash stored)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_local_sessions (
            session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            refresh_token_hash TEXT NOT NULL UNIQUE,
            password_changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ NULL,
            revoked_reason TEXT NULL,
            client_ip TEXT NULL,
            user_agent TEXT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_dalton_local_sessions_user_active
            ON dalton_local_sessions (user_id, expires_at DESC)
            WHERE revoked_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_dalton_local_sessions_expires_at
            ON dalton_local_sessions (expires_at);
        """
    )

    # 4b. Rotated-out refresh token history for bounded once-only replay detection
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_local_refresh_tokens (
            token_hash TEXT PRIMARY KEY,
            session_id UUID NOT NULL REFERENCES dalton_local_sessions(session_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            rotated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            reuse_detected_at TIMESTAMPTZ NULL
        );
        CREATE INDEX IF NOT EXISTS idx_dalton_local_refresh_tokens_expires_at
            ON dalton_local_refresh_tokens (expires_at);
        """
    )

    # 5. Permanent singleton bootstrap completion record (non-empty => 409 forever)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_bootstrap_state (
            singleton_id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (singleton_id = 1),
            completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_by_user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT
        );
        """
    )

    # 6. Singleton active bootstrap token challenge state (24h expiry, 5-attempt lockout)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_bootstrap_tokens (
            singleton_id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (singleton_id = 1),
            token_sha256 TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('env', 'cli')),
            issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            failed_attempts INTEGER NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
            locked_at TIMESTAMPTZ NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    # 7. Dual-dimension (IP and account) login rate-limiting and backoff table
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_auth_attempts (
            dimension TEXT NOT NULL CHECK (dimension IN ('ip', 'account')),
            key_hash TEXT NOT NULL,
            failed_attempts INTEGER NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
            window_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_failed_at TIMESTAMPTZ NULL,
            locked_until TIMESTAMPTZ NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (dimension, key_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_dalton_auth_attempts_locked_until
            ON dalton_auth_attempts (locked_until)
            WHERE locked_until IS NOT NULL;
        """
    )

    # 8. Add target tier column to existing Dalton-owned dalton_invites table (d016)
    op.execute(
        """
        ALTER TABLE dalton_invites
            ADD COLUMN IF NOT EXISTS tier TEXT NOT NULL DEFAULT 'external';
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_dalton_invites_tier'
            ) THEN
                ALTER TABLE dalton_invites
                    ADD CONSTRAINT ck_dalton_invites_tier
                    CHECK (tier IN ('internal', 'external'));
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # Acquire the same PostgreSQL transaction advisory lock used by bootstrap so a
    # concurrent ``POST /bootstrap/admin`` cannot race with downgrade, then refuse
    # downgrade if initial admin bootstrap has already completed.
    op.execute(
        f"""
        DO $$
        BEGIN
            PERFORM pg_advisory_xact_lock({_BOOTSTRAP_ADVISORY_LOCK_KEY});
            IF to_regclass('public.dalton_bootstrap_state') IS NOT NULL
               AND EXISTS (SELECT 1 FROM dalton_bootstrap_state WHERE singleton_id = 1) THEN
                RAISE EXCEPTION
                    'Refusing to downgrade d047: initial admin bootstrap has already completed '
                    '(dalton_bootstrap_state is non-empty). Dropping d047 tables would erase '
                    'the permanent bootstrap lock and allow re-bootstrapping.';
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        ALTER TABLE dalton_invites DROP CONSTRAINT IF EXISTS ck_dalton_invites_tier;
        ALTER TABLE dalton_invites DROP COLUMN IF EXISTS tier;
        """
    )
    op.execute("DROP TABLE IF EXISTS dalton_auth_attempts;")
    op.execute("DROP TABLE IF EXISTS dalton_bootstrap_tokens;")
    op.execute("DROP TABLE IF EXISTS dalton_bootstrap_state;")
    op.execute("DROP TABLE IF EXISTS dalton_local_refresh_tokens;")
    op.execute("DROP TABLE IF EXISTS dalton_local_sessions;")
    op.execute("DROP TABLE IF EXISTS dalton_local_credentials;")
    op.execute("DROP TABLE IF EXISTS dalton_credential_holder;")
    op.execute("DROP TABLE IF EXISTS dalton_user_roles;")
