"""Real PostgreSQL upgrade -> downgrade -> re-upgrade integration test for migration d048.

Skipped automatically when ``DALTON_TEST_PG_DSN`` is not set.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PG_DSN = os.getenv("DALTON_TEST_PG_DSN", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_DSN,
        reason="DALTON_TEST_PG_DSN is not set; skipping real PostgreSQL d048 migration test",
    ),
]


def _run_alembic(args: list[str], *, env: dict[str, str], dalton: bool = True) -> None:
    cmd = [sys.executable, "-m", "alembic"]
    if dalton:
        cmd.extend(["-c", "src/dalton/alembic.ini"])
    cmd.extend(args)
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Alembic command failed: {' '.join(cmd)}\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


from tests.integration.dalton._db_guard import extract_effective_test_db_name


def _alembic_env_for_dsn(dsn: str) -> dict[str, str]:
    from psycopg.conninfo import conninfo_to_dict

    db_name = extract_effective_test_db_name(dsn, source="DALTON_TEST_PG_DSN")
    params = conninfo_to_dict(dsn)
    env = os.environ.copy()
    env["DATABASE_URL"] = dsn
    if params.get("host"):
        env["DB_HOST"] = str(params["host"])
    if params.get("port"):
        env["DB_PORT"] = str(params["port"])
    if params.get("user"):
        env["DB_USER"] = str(params["user"])
    if params.get("password") is not None:
        env["DB_PASSWORD"] = str(params["password"])
    env["DB_NAME"] = db_name
    return env


def test_d048_upgrade_downgrade_reupgrade_real_postgres() -> None:
    """Verify d047 -> d048 -> downgrade d047 -> re-upgrade d048 preserves audit & revokes pending admin invites."""
    try:
        extract_effective_test_db_name(PG_DSN, source="DALTON_TEST_PG_DSN")
    except ValueError as exc:
        pytest.fail(str(exc))
    env = _alembic_env_for_dsn(PG_DSN)

    # Ensure upstream schema + Dalton up to d047 exist, then upgrade to d048.
    _run_alembic(["upgrade", "head"], env=env, dalton=False)
    _run_alembic(["upgrade", "d047"], env=env, dalton=True)
    _run_alembic(["upgrade", "d048"], env=env, dalton=True)

    pending_admin_id = str(uuid.uuid4())
    redeemed_admin_id = str(uuid.uuid4())
    pending_internal_id = str(uuid.uuid4())
    pending_external_id = str(uuid.uuid4())
    creator_uid = f"d048-test-creator-{uuid.uuid4().hex[:8]}"

    with psycopg.connect(PG_DSN, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dalton_invites (invite_id, token_sha256, created_by_user_id, expires_at, tier)
                VALUES (%s, %s, %s, NOW() + INTERVAL '7 days', 'admin')
                """,
                (pending_admin_id, f"sha-pending-admin-{uuid.uuid4().hex}", creator_uid),
            )
            cur.execute(
                """
                INSERT INTO dalton_invites (
                    invite_id, token_sha256, created_by_user_id, expires_at, used_at, used_by_email, tier
                )
                VALUES (%s, %s, %s, NOW() + INTERVAL '7 days', NOW(), 'redeemed-admin@example.com', 'admin')
                """,
                (redeemed_admin_id, f"sha-redeemed-admin-{uuid.uuid4().hex}", creator_uid),
            )
            cur.execute(
                """
                INSERT INTO dalton_invites (invite_id, token_sha256, created_by_user_id, expires_at, tier)
                VALUES (%s, %s, %s, NOW() + INTERVAL '7 days', 'internal')
                """,
                (pending_internal_id, f"sha-pending-internal-{uuid.uuid4().hex}", creator_uid),
            )
            cur.execute(
                """
                INSERT INTO dalton_invites (invite_id, token_sha256, created_by_user_id, expires_at, tier)
                VALUES (%s, %s, %s, NOW() + INTERVAL '7 days', 'external')
                """,
                (pending_external_id, f"sha-pending-external-{uuid.uuid4().hex}", creator_uid),
            )
        conn.commit()

    try:
        # Downgrade d048 -> d047
        _run_alembic(["downgrade", "d047"], env=env, dalton=True)

        with psycopg.connect(PG_DSN, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tier, used_at, revoked_at FROM dalton_invites WHERE invite_id = %s",
                    (pending_admin_id,),
                )
                p_tier, p_used, p_revoked = cur.fetchone()
                assert p_tier == "admin"
                assert p_used is None
                assert p_revoked is not None, "Pending admin invite must be revoked on d048 downgrade"

                cur.execute(
                    "SELECT tier, used_at, revoked_at FROM dalton_invites WHERE invite_id = %s",
                    (redeemed_admin_id,),
                )
                r_tier, r_used, r_revoked = cur.fetchone()
                assert r_tier == "admin", "Redeemed admin invite must keep tier='admin' for audit"
                assert r_used is not None
                assert r_revoked is None

                cur.execute(
                    "SELECT tier, revoked_at FROM dalton_invites WHERE invite_id = %s",
                    (pending_internal_id,),
                )
                assert cur.fetchone() == ("internal", None)

                # New pending admin invite must be rejected by the downgraded check constraint
                with pytest.raises(psycopg.errors.CheckViolation):
                    cur.execute(
                        """
                        INSERT INTO dalton_invites (invite_id, token_sha256, created_by_user_id, expires_at, tier)
                        VALUES (%s, %s, %s, NOW() + INTERVAL '7 days', 'admin')
                        """,
                        (str(uuid.uuid4()), f"sha-new-admin-blocked-{uuid.uuid4().hex}", creator_uid),
                    )
                conn.rollback()

        # Re-upgrade d047 -> d048
        _run_alembic(["upgrade", "d048"], env=env, dalton=True)

        reupgraded_admin_id = str(uuid.uuid4())
        with psycopg.connect(PG_DSN, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO dalton_invites (invite_id, token_sha256, created_by_user_id, expires_at, tier)
                    VALUES (%s, %s, %s, NOW() + INTERVAL '7 days', 'admin')
                    """,
                    (reupgraded_admin_id, f"sha-reupgraded-admin-{uuid.uuid4().hex}", creator_uid),
                )
                cur.execute(
                    "SELECT tier, revoked_at FROM dalton_invites WHERE invite_id = %s",
                    (reupgraded_admin_id,),
                )
                assert cur.fetchone() == ("admin", None)
                # Clean up test rows
                cur.execute(
                    "DELETE FROM dalton_invites WHERE created_by_user_id = %s",
                    (creator_uid,),
                )
            conn.commit()
    finally:
        # Leave DB at head and clean up test rows even on failure
        try:
            _run_alembic(["upgrade", "head"], env=env, dalton=True)
        except AssertionError:
            pass
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM dalton_invites WHERE created_by_user_id = %s",
                    (creator_uid,),
                )
