"""Real PostgreSQL integration tests for deployment credentials migration d06X, audit trigger, and concurrency.

Run outside the sandbox against a temporary *_test PostgreSQL database (serially, no -n):
  env TEST_DB_HOST=127.0.0.1 TEST_DB_PORT=55432 TEST_DB_NAME=lane84_test \
      TEST_DB_USER=postgres TEST_DB_PASSWORD=pgtest \
      DALTON_TEST_PG_DSN=postgresql://postgres:pgtest@127.0.0.1:55432/lane84_test \
      .venv/bin/python -m pytest -q -o addopts="" -p no:cacheprovider \
      -m integration tests/integration/dalton/test_deployment_credentials_db.py
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from unittest.mock import create_autospec

import psycopg
import pytest
from psycopg import errors as pg_errors

from src.dalton.deployment_credentials import sandbox_scrub_store as dc_scrub_store
from src.dalton.deployment_credentials import snapshot as dc_snapshot
from src.dalton.deployment_credentials import store as dc_store
from src.dalton.deployment_credentials.store import (
    DeploymentSettingConflictError,
    read_settings_snapshot,
    write_settings,
)
from src.server.services.setup_settings import apply as setup_apply
from src.server.services.setup_settings import environment as setup_env
from src.server.services.setup_settings import follow as setup_follow
from tests.integration.dalton._db_guard import extract_effective_test_db_name

# Loaded before patched_get_db_connection sweeps sys.modules so store uses test_db_pool.
_POOL_HOLDERS = (dc_store, dc_scrub_store)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

_TEST_BYOK_KEY = "test-byok-db-key-not-real-0123456789abcdef"


async def _reset_deployment_tables(test_db_uri: str) -> None:
    """Clean deployment_settings and reset meta revision=0 (audit table is append-only)."""
    extract_effective_test_db_name(test_db_uri, source="test_db_uri")
    async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True) as conn:
        await conn.execute("DELETE FROM deployment_settings")
        await conn.execute(
            """
            INSERT INTO dalton_deployment_settings_meta (singleton, revision, updated_by)
            VALUES (TRUE, 0, NULL)
            ON CONFLICT (singleton) DO UPDATE
            SET revision = 0, updated_at = NOW(), updated_by = NULL
            """
        )


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BYOK_ENCRYPTION_KEY", _TEST_BYOK_KEY)
    monkeypatch.setenv("DALTON_ENABLED", "1")
    for key in (
        "DALTON_GOOGLE_OAUTH_CLIENT_ID",
        "DALTON_GOOGLE_OAUTH_CLIENT_SECRET",
        "DALTON_FEISHU_APP_ID",
        "DALTON_FEISHU_APP_SECRET",
        "DALTON_FEISHU_ENCRYPT_KEY",
        "DALTON_FEISHU_VERIFICATION_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(setup_env, "_at_start", None)
    dc_snapshot.reset_active_snapshot_for_test()
    monkeypatch.setattr(
        dc_store,
        "query_feishu_vault_pair_strict",
        create_autospec(
            dc_snapshot.query_feishu_vault_pair_strict,
            return_value=(None, False, None),
        ),
    )
    monkeypatch.setattr(
        setup_apply,
        "refresh",
        create_autospec(setup_apply.refresh, return_value={}),
    )
    monkeypatch.setattr(
        setup_follow,
        "publish_changed",
        create_autospec(setup_follow.publish_changed, return_value=None),
    )
    yield
    dc_snapshot.reset_active_snapshot_for_test()


async def test_migration_d06x_tables_initial_revision_and_append_only_audit_trigger(
    dalton_schema,
    test_db_uri: str,
) -> None:
    """Verify d06X created all 3 tables, initialized meta revision=0, and blocks UPDATE/DELETE/TRUNCATE."""
    extract_effective_test_db_name(test_db_uri, source="test_db_uri")
    async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = 'public'
                  AND tablename IN (
                      'dalton_deployment_settings_meta',
                      'dalton_deployment_settings_audit',
                      'dalton_sandbox_feishu_vault_scrub'
                  )
                ORDER BY tablename
                """
            )
            tables = [row[0] for row in await cur.fetchall()]
            assert tables == [
                "dalton_deployment_settings_audit",
                "dalton_deployment_settings_meta",
                "dalton_sandbox_feishu_vault_scrub",
            ]

            await cur.execute(
                "SELECT revision FROM dalton_deployment_settings_meta WHERE singleton = TRUE"
            )
            meta_row = await cur.fetchone()
            assert meta_row is not None and int(meta_row[0]) >= 0

            # Seed one audit row and verify UPDATE, DELETE, and TRUNCATE all raise under table owner.
            await cur.execute(
                """
                INSERT INTO dalton_deployment_settings_audit (
                    revision, actor_user_id, setting_name, action, previous_source, value_fingerprint
                )
                VALUES (0, 'db-owner-test', 'DALTON_FEISHU_ENCRYPT_KEY', 'set', 'unset', 'abcdef012345')
                RETURNING audit_id
                """
            )
            inserted = await cur.fetchone()
            assert inserted is not None
            audit_id = inserted[0]

            with pytest.raises(pg_errors.RaiseException, match="append-only"):
                await cur.execute(
                    "UPDATE dalton_deployment_settings_audit SET action = 'clear' WHERE audit_id = %s",
                    (audit_id,),
                )
            with pytest.raises(pg_errors.RaiseException, match="append-only"):
                await cur.execute(
                    "DELETE FROM dalton_deployment_settings_audit WHERE audit_id = %s",
                    (audit_id,),
                )
            with pytest.raises(pg_errors.RaiseException, match="append-only"):
                await cur.execute("TRUNCATE TABLE dalton_deployment_settings_audit")


async def test_group_write_increments_revision_appends_hmac_audit_and_supports_clear(
    dalton_schema,
    patched_get_db_connection,
    test_db_uri: str,
) -> None:
    """Group write increments revision and writes audit rows in the same transaction."""
    await _reset_deployment_tables(test_db_uri)
    fake_secret = "test-secret-not-real-db-audit-0001"
    expected_fp = hmac.new(
        _TEST_BYOK_KEY.encode("utf-8"),
        fake_secret.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:12]

    res1 = await write_settings(
        {
            "DALTON_GOOGLE_OAUTH_CLIENT_ID": "db-g-client-1",
            "DALTON_GOOGLE_OAUTH_CLIENT_SECRET": fake_secret,
        },
        expected_revision=0,
        actor_user_id="admin-db-1",
        client_ip="127.0.0.1",
    )
    assert res1.revision == 1
    assert set(res1.changed_names) == {
        "DALTON_GOOGLE_OAUTH_CLIENT_ID",
        "DALTON_GOOGLE_OAUTH_CLIENT_SECRET",
    }

    rev, rows = await read_settings_snapshot()
    assert rev == 1
    assert rows["DALTON_GOOGLE_OAUTH_CLIENT_ID"] == "db-g-client-1"
    assert rows["DALTON_GOOGLE_OAUTH_CLIENT_SECRET"] == fake_secret

    async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT setting_name, action, previous_source, value_fingerprint, actor_user_id, client_ip
                FROM dalton_deployment_settings_audit
                WHERE revision = 1 AND actor_user_id = 'admin-db-1'
                ORDER BY setting_name
                """
            )
            audits = {r[0]: r[1:] for r in await cur.fetchall()}
            assert audits["DALTON_GOOGLE_OAUTH_CLIENT_ID"] == (
                "set",
                "unset",
                "db-g-client-1",
                "admin-db-1",
                "127.0.0.1",
            )
            assert audits["DALTON_GOOGLE_OAUTH_CLIENT_SECRET"] == (
                "set",
                "unset",
                expected_fp,
                "admin-db-1",
                "127.0.0.1",
            )
            assert fake_secret not in str(audits)

    # Clear entire group -> revision 2 with NULL value_fingerprint
    res2 = await write_settings(
        {
            "DALTON_GOOGLE_OAUTH_CLIENT_ID": None,
            "DALTON_GOOGLE_OAUTH_CLIENT_SECRET": None,
        },
        expected_revision=1,
        actor_user_id="admin-db-1",
        client_ip="127.0.0.1",
    )
    assert res2.revision == 2
    rev2, rows2 = await read_settings_snapshot()
    assert rev2 == 2
    assert rows2 == {}

    async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT setting_name, action, previous_source, value_fingerprint
                FROM dalton_deployment_settings_audit
                WHERE revision = 2 AND actor_user_id = 'admin-db-1'
                ORDER BY setting_name
                """
            )
            clear_audits = await cur.fetchall()
            assert len(clear_audits) == 2
            for _name, action, prev_src, fp in clear_audits:
                assert action == "clear"
                assert prev_src == "saved"
                assert fp is None


async def test_concurrent_writers_same_expected_revision_exactly_one_wins(
    dalton_schema,
    patched_get_db_connection,
    test_db_uri: str,
) -> None:
    """Two concurrent writers targeting expected_revision=0 serialize: one commits, one gets 409."""
    await _reset_deployment_tables(test_db_uri)

    async def _attempt(tag: str):
        return await write_settings(
            {
                "DALTON_GOOGLE_OAUTH_CLIENT_ID": f"cid-{tag}",
                "DALTON_GOOGLE_OAUTH_CLIENT_SECRET": f"test-secret-not-real-race-{tag}",
            },
            expected_revision=0,
            actor_user_id=f"admin-{tag}",
        )

    outcomes = await asyncio.gather(_attempt("A"), _attempt("B"), return_exceptions=True)
    successes = [o for o in outcomes if not isinstance(o, BaseException)]
    conflicts = [o for o in outcomes if isinstance(o, DeploymentSettingConflictError)]

    assert len(successes) == 1
    assert len(conflicts) == 1
    assert successes[0].revision == 1
    assert conflicts[0].code == "revision_conflict"
    assert conflicts[0].current_revision == 1


async def test_concurrent_rotation_repeatable_read_snapshot_never_tears_group(
    dalton_schema,
    patched_get_db_connection,
    test_db_uri: str,
) -> None:
    """Concurrent readers during pair rotations always observe consistent (id, secret) pairs."""
    await _reset_deployment_tables(test_db_uri)
    await write_settings(
        {
            "DALTON_GOOGLE_OAUTH_CLIENT_ID": "cid-v1",
            "DALTON_GOOGLE_OAUTH_CLIENT_SECRET": "test-secret-not-real-v1",
        },
        expected_revision=0,
        actor_user_id="rotator",
    )

    stop_reading = asyncio.Event()
    observed: list[tuple[int, str | None, str | None]] = []

    async def _writer() -> None:
        for rev in range(1, 10):
            next_v = rev + 1
            await write_settings(
                {
                    "DALTON_GOOGLE_OAUTH_CLIENT_ID": f"cid-v{next_v}",
                    "DALTON_GOOGLE_OAUTH_CLIENT_SECRET": f"test-secret-not-real-v{next_v}",
                },
                expected_revision=rev,
                actor_user_id="rotator",
            )
            await asyncio.sleep(0.005)
        stop_reading.set()

    async def _reader() -> None:
        while not stop_reading.is_set():
            rev, vals = await read_settings_snapshot()
            observed.append(
                (
                    rev,
                    vals.get("DALTON_GOOGLE_OAUTH_CLIENT_ID"),
                    vals.get("DALTON_GOOGLE_OAUTH_CLIENT_SECRET"),
                )
            )
            await asyncio.sleep(0.001)

    await asyncio.gather(_writer(), _reader(), _reader())
    assert observed
    for rev, cid, secret in observed:
        assert cid == f"cid-v{rev}"
        assert secret == f"test-secret-not-real-v{rev}"


async def test_corrupt_ciphertext_savepoint_isolation_and_incomplete_pair_drop(
    dalton_schema,
    patched_get_db_connection,
    test_db_uri: str,
) -> None:
    """Corrupt ciphertext row rolls back only its SAVEPOINT and drops its pair_required group only."""
    await _reset_deployment_tables(test_db_uri)
    await write_settings(
        {
            "DALTON_GOOGLE_OAUTH_CLIENT_ID": "g-good-id",
            "DALTON_GOOGLE_OAUTH_CLIENT_SECRET": "test-secret-not-real-good-g",
            "DALTON_FEISHU_APP_ID": "f-good-id",
            "DALTON_FEISHU_APP_SECRET": "test-secret-not-real-good-f",
            "DALTON_FEISHU_VERIFICATION_TOKEN": "test-secret-not-real-good-vt",
        },
        expected_revision=0,
        actor_user_id="admin-corrupt-test",
    )

    # Corrupt DALTON_GOOGLE_OAUTH_CLIENT_SECRET bytes directly in Postgres.
    async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True) as conn:
        await conn.execute(
            "UPDATE deployment_settings SET value = %s WHERE name = %s",
            (b"not-valid-pgp-ciphertext", "DALTON_GOOGLE_OAUTH_CLIENT_SECRET"),
        )

    rev, rows = await read_settings_snapshot()
    assert rev == 1
    # Corrupt pair_required=True group (google_oauth) is dropped as a whole.
    assert "DALTON_GOOGLE_OAUTH_CLIENT_ID" not in rows
    assert "DALTON_GOOGLE_OAUTH_CLIENT_SECRET" not in rows
    # Unrelated intact groups remain readable from the same snapshot.
    assert rows["DALTON_FEISHU_APP_ID"] == "f-good-id"
    assert rows["DALTON_FEISHU_APP_SECRET"] == "test-secret-not-real-good-f"
    assert rows["DALTON_FEISHU_VERIFICATION_TOKEN"] == "test-secret-not-real-good-vt"


async def test_sandbox_feishu_vault_scrub_store_transitions_concurrency_and_ref_change(
    dalton_schema,
    patched_get_db_connection,
    test_db_uri: str,
) -> None:
    """Verify dalton_sandbox_feishu_vault_scrub transitions, concurrent attempt_count, and provider_ref re-list."""
    import uuid

    extract_effective_test_db_name(test_db_uri, source="test_db_uri")
    user_id = f"scrub-db-user-{uuid.uuid4().hex[:8]}"
    cid_running = str(uuid.uuid4())
    cid_stopped = str(uuid.uuid4())
    ref_running_v1 = f"sb-scrub-run-{uuid.uuid4().hex[:8]}"
    ref_running_v2 = f"sb-scrub-run-v2-{uuid.uuid4().hex[:8]}"
    ref_stopped = f"sb-scrub-stop-{uuid.uuid4().hex[:8]}"

    try:
        # Explicit distinct computer names satisfy migration 097's idx_computers_user_name unique index.
        async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True) as conn:
            await conn.execute(
                "INSERT INTO users (user_id, name) VALUES (%s, %s)",
                (user_id, user_id),
            )
            await conn.execute(
                "INSERT INTO computers (computer_id, user_id, name, kind, status, provider_ref, root_dir) "
                "VALUES (%s::uuid, %s, %s, 'docker', 'running', %s, '/home/workspace')",
                (cid_running, user_id, f"scrub-running-{cid_running[:8]}", ref_running_v1),
            )
            await conn.execute(
                "INSERT INTO computers (computer_id, user_id, name, kind, status, provider_ref, root_dir) "
                "VALUES (%s::uuid, %s, %s, 'docker', 'stopped', %s, '/home/workspace')",
                (cid_stopped, user_id, f"scrub-stopped-{cid_stopped[:8]}", ref_stopped),
            )

        # 1. Non-running computer is listed and marked deferred_stopped.
        non_running = await dc_scrub_store.list_non_running_uncleaned_computer_ids()
        assert cid_stopped in non_running
        assert cid_running not in non_running

        marked = await dc_scrub_store.mark_deferred_stopped([cid_stopped])
        assert marked == 1
        st_stopped = await dc_scrub_store.get_scrub_state(cid_stopped)
        assert st_stopped is not None
        assert st_stopped["status"] == "deferred_stopped"
        assert st_stopped["attempt_count"] == 0

        # 2. Running uncleaned computer is returned by list_running_uncleaned.
        running_rows = await dc_scrub_store.list_running_uncleaned(limit=50)
        assert any(r["computer_id"] == cid_running for r in running_rows)
        assert all(r["computer_id"] != cid_stopped for r in running_rows)

        # 3. Two concurrent mark_failed calls increment attempt_count atomically in SQL.
        await asyncio.gather(
            dc_scrub_store.mark_failed(cid_running, ref_running_v1, "TimeoutError"),
            dc_scrub_store.mark_failed(cid_running, ref_running_v1, "ConnectionError"),
        )
        st_failed = await dc_scrub_store.get_scrub_state(cid_running)
        assert st_failed is not None
        assert st_failed["status"] == "failed"
        assert st_failed["attempt_count"] == 2
        assert st_failed["last_error"] in ("TimeoutError", "ConnectionError")
        assert st_failed["cleaned_at"] is None

        # Still listed while status='failed'.
        running_after_fail = await dc_scrub_store.list_running_uncleaned(limit=50)
        assert any(r["computer_id"] == cid_running for r in running_after_fail)

        # 4. Transition failed -> cleaned excludes the computer for matching provider_ref.
        await dc_scrub_store.mark_cleaned(cid_running, ref_running_v1)
        st_clean = await dc_scrub_store.get_scrub_state(cid_running)
        assert st_clean is not None
        assert st_clean["status"] == "cleaned"
        assert st_clean["attempt_count"] == 3
        assert st_clean["last_error"] is None
        assert st_clean["cleaned_at"] is not None

        running_after_clean = await dc_scrub_store.list_running_uncleaned(limit=50)
        assert all(r["computer_id"] != cid_running for r in running_after_clean)

        # 5. Changing computers.provider_ref causes list_running_uncleaned to re-list it.
        async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True) as conn:
            await conn.execute(
                "UPDATE computers SET provider_ref = %s WHERE computer_id = %s::uuid",
                (ref_running_v2, cid_running),
            )
        running_after_recreate = await dc_scrub_store.list_running_uncleaned(limit=50)
        matched = [r for r in running_after_recreate if r["computer_id"] == cid_running]
        assert len(matched) == 1
        assert matched[0]["provider_ref"] == ref_running_v2

        # Transition deferred_stopped -> failed -> cleaned on cid_stopped.
        await dc_scrub_store.mark_failed(cid_stopped, ref_stopped, "VaultScrubIOError")
        await dc_scrub_store.mark_cleaned(cid_stopped, ref_stopped)
        st_stopped_clean = await dc_scrub_store.get_scrub_state(cid_stopped)
        assert st_stopped_clean is not None
        assert st_stopped_clean["status"] == "cleaned"
        assert st_stopped_clean["attempt_count"] == 2
        assert st_stopped_clean["last_error"] is None
    finally:
        async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True) as conn:
            await conn.execute(
                "DELETE FROM dalton_sandbox_feishu_vault_scrub WHERE computer_id = ANY(%s::uuid[])",
                ([cid_running, cid_stopped],),
            )
            await conn.execute(
                "DELETE FROM computers WHERE computer_id = ANY(%s::uuid[])",
                ([cid_running, cid_stopped],),
            )
            await conn.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
