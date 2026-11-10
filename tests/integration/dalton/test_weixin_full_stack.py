"""Self-contained Python + TypeScript full-stack integration test for CH-1 Personal Weixin iLink.

Starts `WeixinLiveBackendHarness` (validating the `*_test` database, applying Alembic `head` + `d032`,
seeding two independent admins + internal + external users with real `get_user_tier`, and running
live FastAPI on an ephemeral port), then executes `test/weixin-real-backend.integration.test.ts`
(`GatewayApp` + `HttpBackendClient` + `WeixinChannelAdapter`) over real loopback HTTP, and verifies
that teardown leaves zero synthetic rows in Postgres.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import pytest

from tests.integration.dalton.test_channel_gateway_db import (
    _cleanup_test_rows,
    _ensure_alembic_upgraded_to_head,
    _get_test_dsn,
)
from tests.integration.dalton.weixin_live_backend_harness import (
    DEFAULT_SYNTHETIC_ENCRYPTION_KEY,
    WeixinLiveBackendHarness,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
GATEWAY_DIR = REPO_ROOT / "integrations" / "dalton-channel-gateway"


@pytest.fixture(scope="session", autouse=True)
def dalton_schema():
    """Override directory conftest autouse fixture so unset DSN skips without opening test_db_pool."""
    yield


@pytest.fixture(autouse=True)
def cleanup_dalton_tables():
    """Override directory conftest autouse fixture so unset DSN skips without opening test_db_pool."""
    yield


async def _terminate_subprocess(proc: asyncio.subprocess.Process | None) -> None:
    """Ensure a spawned subprocess is terminated and reaped on timeout or failure."""
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except (TimeoutError, asyncio.TimeoutError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


@pytest.mark.asyncio
async def test_weixin_real_http_backend_gateway_and_postgres_full_stack() -> None:
    """Run the full 4-part Weixin real-HTTP + real-Postgres integration suite end-to-end and verify clean, scoped teardown."""
    dsn = _get_test_dsn()
    await _ensure_alembic_upgraded_to_head(dsn)

    ctrl_prefix = f"wxctrl_{uuid4().hex[:8]}"
    ctrl_bot = f"{ctrl_prefix}_bot@im.wechat"
    ctrl_user = f"{ctrl_prefix}_user@im.wechat"
    ctrl_msg_id = f"{ctrl_bot}:ctrl_msg_1"
    ctrl_sess_key = f"{ctrl_prefix}_sess"
    ctrl_req_key = str(uuid4())
    enc_key = (
        os.environ.get("BYOK_ENCRYPTION_KEY", "").strip()
        or DEFAULT_SYNTHETIC_ENCRYPTION_KEY
    )

    ctrl_pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=4,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        open=False,
    )
    await ctrl_pool.open()
    await ctrl_pool.wait()

    try:
        # Pre-save unowned/unlinked control rows to verify harness start/run/stop preserves unrelated rows
        async with ctrl_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_weixin_context_tokens (
                        ilink_bot_id, bot_generation, platform_user_id,
                        encrypted_context_token, seq, updated_at
                    )
                    VALUES (%s, 1, %s, pgp_sym_encrypt('ctrl-token', %s), 9, NOW())
                    """,
                    (ctrl_bot, ctrl_user, enc_key),
                )
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_inbound_events (
                        platform, message_id, request_key, dalton_user_id,
                        chat_type, chat_address, thread_scope_key, status,
                        fallback_pending, raw_payload
                    )
                    VALUES (
                        'weixin', %s, %s, NULL,
                        'dm', 'weixin:@me', %s, 'ignored',
                        FALSE, '{"deny_reason": "not_linked"}'::jsonb
                    )
                    """,
                    (ctrl_msg_id, ctrl_req_key, f"dm:{ctrl_user}"),
                )

        harness = WeixinLiveBackendHarness(dsn)
        prefix = harness.prefix
        user_ids = list(harness.cleanup_user_ids)
        proc: asyncio.subprocess.Process | None = None

        try:
            info = await harness.start()
            env = {
                **os.environ,
                "GATEWAY_REAL_BACKEND_URL": str(info["base_url"]),
                "INTERNAL_SERVICE_TOKEN": str(info["service_token"]),
                "GATEWAY_TEST_ADMIN_USER_ID": str(info["admin_1"]),
                "GATEWAY_TEST_ADMIN_2_USER_ID": str(info["admin_2"]),
                "GATEWAY_TEST_INTERNAL_USER_ID": str(info["internal_1"]),
                "GATEWAY_TEST_EXTERNAL_USER_ID": str(info["external_1"]),
                "GATEWAY_TEST_RUN_PREFIX": str(info["prefix"]),
            }
            proc = await asyncio.create_subprocess_exec(
                "node",
                str(GATEWAY_DIR / "node_modules" / "vitest" / "vitest.mjs"),
                "run",
                "test/weixin-real-backend.integration.test.ts",
                "--maxWorkers=2",
                cwd=str(GATEWAY_DIR),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=90.0)
            output = (stdout_bytes or b"").decode("utf-8", errors="replace")
            assert proc.returncode == 0, (
                f"weixin-real-backend.integration.test.ts failed (exit={proc.returncode}):\n{output}"
            )
        finally:
            await _terminate_subprocess(proc)
            await harness.stop()

        # Verify zero synthetic rows for this test remain in Postgres after harness.stop(),
        # while the pre-saved control rows from ctrl_prefix still exist.
        like_pat = f"%{prefix}%"
        async with ctrl_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT COUNT(*) AS cnt FROM dalton_channel_weixin_state
                    WHERE owner_user_id = ANY(%s)
                       OR login_owner_user_id = ANY(%s)
                       OR ilink_bot_id LIKE %s
                    """,
                    (user_ids, user_ids, like_pat),
                )
                assert (await cur.fetchone())["cnt"] == 0

                await cur.execute(
                    """
                    SELECT COUNT(*) AS cnt FROM dalton_channel_weixin_context_tokens
                    WHERE ilink_bot_id LIKE %s OR platform_user_id LIKE %s
                    """,
                    (like_pat, like_pat),
                )
                assert (await cur.fetchone())["cnt"] == 0

                await cur.execute(
                    "SELECT COUNT(*) AS cnt FROM users WHERE user_id = ANY(%s)",
                    (user_ids,),
                )
                assert (await cur.fetchone())["cnt"] == 0

                # Pre-saved control rows from ctrl_prefix must still exist
                await cur.execute(
                    "SELECT COUNT(*) AS cnt FROM dalton_channel_weixin_context_tokens WHERE ilink_bot_id = %s",
                    (ctrl_bot,),
                )
                assert (await cur.fetchone())["cnt"] == 1

                await cur.execute(
                    "SELECT COUNT(*) AS cnt FROM dalton_channel_inbound_events WHERE message_id = %s",
                    (ctrl_msg_id,),
                )
                assert (await cur.fetchone())["cnt"] == 1

                # Seed an unowned control row in dalton_channel_weixin_state and verify scoped cleanup preserves it
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_weixin_state (
                        singleton_id, ilink_bot_id, owner_user_id, encrypted_bot_token,
                        base_url, get_updates_buf, status, bot_generation,
                        login_session_key, login_owner_user_id, login_version, updated_at
                    )
                    VALUES (
                        1, NULL, NULL, NULL,
                        'https://ilinkai.weixin.qq.com', '', 'unconfigured', 1,
                        %s, NULL, 1, NOW()
                    )
                    """,
                    (ctrl_sess_key,),
                )

        await _cleanup_test_rows(
            ctrl_pool,
            prefix=prefix,
            user_ids=user_ids,
            workspace_ids=[],
            thread_ids=[],
        )

        async with ctrl_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COUNT(*) AS cnt FROM dalton_channel_weixin_state WHERE login_session_key = %s AND owner_user_id IS NULL",
                    (ctrl_sess_key,),
                )
                assert (await cur.fetchone())["cnt"] == 1
    finally:
        try:
            await _cleanup_test_rows(
                ctrl_pool,
                prefix=ctrl_prefix,
                user_ids=[],
                workspace_ids=[],
                thread_ids=[],
                session_keys=[ctrl_sess_key],
            )
        finally:
            await ctrl_pool.close()
