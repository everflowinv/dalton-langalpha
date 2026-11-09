"""Self-contained live FastAPI + isolated *_test Postgres harness for CH-1 Weixin integration tests.

Responsibilities:
- Validates that the target DSN points to an isolated `*_test` database (never production);
- Applies real Alembic migrations to upstream `head` and Dalton `d032`;
- Sets synthetic `BYOK_ENCRYPTION_KEY` and `INTERNAL_SERVICE_TOKEN`;
- Seeds two independent admin users (`admin_1`, `admin_2`), an internal user (`internal_1`),
  and an external user (`external_1`) in `users` and `workspaces`, configuring
  `DALTON_PM_USER_IDS` and `DALTON_ALLOWED_EMAILS` so the real `get_user_tier()` and
  permission checks run without monkeypatching `get_user_tier`;
- Launches a real FastAPI server on an ephemeral `127.0.0.1` port mounting `dalton_router`
  (`channels_router`), synthetic SSE turn endpoints that obey `trg_responses_lifecycle_guard`
  (`in_progress` -> `completed`), and test control endpoints;
- Cleans up all synthetic rows on teardown so zero synthetic data remains in the test database.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
import signal
import socket
import sys
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import uvicorn

from src.dalton import sharing as dalton_sharing
from src.dalton.app.router import router as dalton_router
from src.dalton.database import channel_gateway as cg_db
from src.server.database import pool as db_pool
from src.server.database import user as user_db
from src.server.database.home_workspace import get_home_workspace_id
from src.server.utils.api import get_current_user_id
from tests.integration.dalton.test_channel_gateway_db import (
    _cleanup_test_rows,
    _ensure_alembic_upgraded_to_head,
)

DEFAULT_SYNTHETIC_SERVICE_TOKEN = "test-weixin-internal-service-token-32b!!"
DEFAULT_SYNTHETIC_ENCRYPTION_KEY = "test-weixin-channel-encryption-key-32b!"

# Same constraint names as conversation.threads_write / 001_initial_schema.
_THREAD_INDEX_CONSTRAINT = "unique_thread_index_per_workspace"
_EXTERNAL_ID_INDEX = "idx_conversation_threads_external"
_THREAD_INDEX_ATTEMPTS = 3


def _unique_constraint_name(exc: BaseException) -> str | None:
    diag = getattr(exc, "diag", None)
    name = getattr(diag, "constraint_name", None) if diag is not None else None
    return str(name) if name else None

_DSN_ENV_VARS = (
    "DALTON_CHANNEL_GATEWAY_TEST_DSN",
    "DALTON_TEST_DB_DSN",
    "TEST_DATABASE_URL",
)


def resolve_validated_test_dsn(explicit_dsn: str | None = None) -> str:
    """Resolve and validate that the Postgres DSN targets an isolated `*_test` database."""
    raw = (explicit_dsn or "").strip()
    if not raw:
        for var in _DSN_ENV_VARS:
            candidate = os.environ.get(var, "").strip()
            if candidate:
                raw = candidate
                break
    if not raw:
        raise RuntimeError(
            "No test Postgres DSN configured (set DALTON_CHANNEL_GATEWAY_TEST_DSN, "
            "DALTON_TEST_DB_DSN, or TEST_DATABASE_URL)."
        )
    parsed = urlsplit(raw)
    db_name = (parsed.path or "").lstrip("/")
    if not db_name.endswith("_test"):
        raise RuntimeError(
            f"Refusing to run Weixin integration harness against non-test database {db_name!r} "
            "(database name must end with '_test')."
        )
    return raw


def _pick_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class WeixinLiveBackendHarness:
    """Manages an isolated *_test Postgres pool, seeded users, and a live uvicorn FastAPI server."""

    def __init__(self, dsn: str, *, prefix: str | None = None) -> None:
        self.dsn = resolve_validated_test_dsn(dsn)
        self.prefix = prefix or f"wxfull_{uuid4().hex[:8]}"
        self.service_token = (
            os.environ.get("INTERNAL_SERVICE_TOKEN", "").strip()
            or DEFAULT_SYNTHETIC_SERVICE_TOKEN
        )
        self.encryption_key = (
            os.environ.get("BYOK_ENCRYPTION_KEY", "").strip()
            or DEFAULT_SYNTHETIC_ENCRYPTION_KEY
        )

        self.admin_1 = f"{self.prefix}_admin_1"
        self.admin_2 = f"{self.prefix}_admin_2"
        self.internal_1 = f"{self.prefix}_internal_1"
        self.external_1 = f"{self.prefix}_external_1"

        self.cleanup_user_ids: list[str] = [
            self.admin_1,
            self.admin_2,
            self.internal_1,
            self.external_1,
        ]
        self.cleanup_workspace_ids: list[str] = [
            get_home_workspace_id(uid) for uid in self.cleanup_user_ids
        ]
        self.cleanup_thread_ids: list[str] = []
        self.cleanup_session_keys: list[str] = []

        self.pool: AsyncConnectionPool | None = None
        self.port: int = 0
        self.base_url: str = ""
        self._server: uvicorn.Server | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._orig_env: dict[str, str | None] = {}
        self._orig_get_db_conns: dict[str, Any] = {}

    async def start(self) -> dict[str, Any]:
        try:
            for key, val in {
                "BYOK_ENCRYPTION_KEY": self.encryption_key,
                "INTERNAL_SERVICE_TOKEN": self.service_token,
                "DALTON_PM_USER_IDS": f"{self.admin_1},{self.admin_2}",
                "DALTON_ALLOWED_EMAILS": (
                    f"{self.admin_1}@example.com,"
                    f"{self.admin_2}@example.com,"
                    f"{self.internal_1}@example.com"
                ),
            }.items():
                self._orig_env[key] = os.environ.get(key)
                os.environ[key] = val

            await _ensure_alembic_upgraded_to_head(self.dsn)

            self.pool = AsyncConnectionPool(
                conninfo=self.dsn,
                min_size=2,
                max_size=12,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                },
                open=False,
            )
            await self.pool.open()
            await self.pool.wait()

            pool_ref = self.pool

            @asynccontextmanager
            async def _pooled_get_db_connection(existing_conn: Any = None):
                if existing_conn is not None:
                    yield existing_conn
                    return
                async with pool_ref.connection() as conn:
                    yield conn

            for mod_name, mod_obj in (
                ("db_pool", db_pool),
                ("cg_db", cg_db),
                ("dalton_sharing", dalton_sharing),
                ("user_db", user_db),
            ):
                self._orig_get_db_conns[mod_name] = getattr(
                    mod_obj, "get_db_connection"
                )
                setattr(mod_obj, "get_db_connection", _pooled_get_db_connection)

            # Clean only rows belonging to this harness prefix/users/sessions before seeding
            await _cleanup_test_rows(
                self.pool,
                prefix=self.prefix,
                user_ids=self.cleanup_user_ids,
                workspace_ids=self.cleanup_workspace_ids,
                thread_ids=self.cleanup_thread_ids,
                session_keys=self.cleanup_session_keys,
            )
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    for uid, label in (
                        (self.admin_1, "Admin One"),
                        (self.admin_2, "Admin Two"),
                        (self.internal_1, "Internal User"),
                        (self.external_1, "External User"),
                    ):
                        await cur.execute(
                            """
                            INSERT INTO users (user_id, email, name, created_at, updated_at)
                            VALUES (%s, %s, %s, NOW(), NOW())
                            ON CONFLICT (user_id) DO UPDATE
                            SET email = EXCLUDED.email, name = EXCLUDED.name, updated_at = NOW()
                            """,
                            (uid, f"{uid}@example.com", label),
                        )
                        home_ws_id = get_home_workspace_id(uid)
                        await cur.execute(
                            """
                            INSERT INTO workspaces (workspace_id, user_id, name, status, created_at, updated_at)
                            VALUES (%s, %s, %s, 'running', NOW(), NOW())
                            ON CONFLICT (workspace_id) DO UPDATE
                            SET status = 'running', updated_at = NOW()
                            """,
                            (home_ws_id, uid, f"{uid}_home"),
                        )

            app = self._build_fastapi_app()
            self.port = _pick_free_local_port()
            self.base_url = f"http://127.0.0.1:{self.port}"

            config = uvicorn.Config(
                app=app,
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                access_log=False,
            )
            self._server = uvicorn.Server(config)
            self._serve_task = asyncio.create_task(self._server.serve())

            deadline = asyncio.get_event_loop().time() + 10.0
            while not self._server.started:
                if self._serve_task.done():
                    exc = self._serve_task.exception()
                    raise RuntimeError(f"FastAPI test server failed to start: {exc}")
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError(
                        "Timed out waiting for FastAPI test server to start"
                    )
                await asyncio.sleep(0.02)

            return self.connection_info()
        except BaseException:
            await self.stop()
            raise

    def connection_info(self) -> dict[str, Any]:
        return {
            "ready": True,
            "base_url": self.base_url,
            "port": self.port,
            "prefix": self.prefix,
            "service_token": self.service_token,
            "admin_1": self.admin_1,
            "admin_2": self.admin_2,
            "internal_1": self.internal_1,
            "external_1": self.external_1,
        }

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._serve_task is not None:
            try:
                await asyncio.wait_for(self._serve_task, timeout=5.0)
            except Exception:
                self._serve_task.cancel()
            self._serve_task = None
        self._server = None

        if self.pool is not None:
            try:
                await _cleanup_test_rows(
                    self.pool,
                    prefix=self.prefix,
                    user_ids=self.cleanup_user_ids,
                    workspace_ids=self.cleanup_workspace_ids,
                    thread_ids=self.cleanup_thread_ids,
                    session_keys=self.cleanup_session_keys,
                )
            finally:
                await self.pool.close()
                self.pool = None

        for mod_name, orig_fn in self._orig_get_db_conns.items():
            mod_obj = {
                "db_pool": db_pool,
                "cg_db": cg_db,
                "dalton_sharing": dalton_sharing,
                "user_db": user_db,
            }[mod_name]
            setattr(mod_obj, "get_db_connection", orig_fn)
        self._orig_get_db_conns.clear()

        for key, orig_val in self._orig_env.items():
            if orig_val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = orig_val
        self._orig_env.clear()

    def _build_fastapi_app(self) -> FastAPI:
        app = FastAPI()
        app.include_router(dalton_router)

        default_admin = self.admin_1
        harness = self

        async def _resolve_current_user_id(request: Request) -> str:
            uid = (request.headers.get("X-User-Id") or "").strip()
            if not uid:
                raise HTTPException(status_code=401, detail="Missing X-User-Id header")
            return uid

        app.dependency_overrides[get_current_user_id] = _resolve_current_user_id

        @app.get("/health")
        async def _health() -> dict[str, Any]:
            return {"ok": True, "prefix": harness.prefix}

        @app.post("/test-control/gateway-url")
        async def _set_gateway_url(request: Request) -> dict[str, Any]:
            body = await request.json()
            gw_url = str(body.get("gateway_url") or "").strip()
            if gw_url:
                os.environ["CHANNEL_GATEWAY_URL"] = gw_url
            return {"ok": True, "gateway_url": gw_url}

        @app.post("/test-control/tiers")
        async def _set_tiers(request: Request) -> dict[str, Any]:
            body = await request.json()
            admins = [str(u).strip() for u in (body.get("admins") or []) if str(u).strip()]
            os.environ["DALTON_PM_USER_IDS"] = ",".join(admins)
            return {"ok": True, "admins": admins}

        @app.post("/test-control/expire-login-session")
        async def _expire_login_session(request: Request) -> dict[str, Any]:
            assert harness.pool is not None
            body: dict[str, Any] = {}
            try:
                raw = await request.json()
                if isinstance(raw, dict):
                    body = raw
            except Exception:
                body = {}
            sess_key = str(body.get("session_key") or "").strip()
            like_pat = f"%{harness.prefix}%"
            async with harness.pool.connection() as conn:
                async with conn.cursor() as cur:
                    if sess_key:
                        await cur.execute(
                            """
                            UPDATE dalton_channel_weixin_state
                            SET login_expires_at = NOW() - INTERVAL '30 seconds'
                            WHERE singleton_id = 1 AND login_session_key = %s
                            """,
                            (sess_key,),
                        )
                    else:
                        await cur.execute(
                            """
                            UPDATE dalton_channel_weixin_state
                            SET login_expires_at = NOW() - INTERVAL '30 seconds'
                            WHERE singleton_id = 1
                              AND (
                                  login_owner_user_id = ANY(%s)
                                  OR owner_user_id = ANY(%s)
                                  OR login_qrcode LIKE %s
                                  OR login_session_key = ANY(%s)
                              )
                            """,
                            (
                                harness.cleanup_user_ids,
                                harness.cleanup_user_ids,
                                like_pat,
                                harness.cleanup_session_keys,
                            ),
                        )
            return {"ok": True}

        @app.get("/test-control/inspect")
        async def _inspect_db() -> dict[str, Any]:
            assert harness.pool is not None
            like_pat = f"%{harness.prefix}%"
            async with harness.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT platform, message_id, dalton_user_id, chat_type,
                               chat_address, status, raw_payload, consumed_bind_result
                        FROM dalton_channel_inbound_events
                        WHERE platform = 'weixin'
                          AND (
                              dalton_user_id = ANY(%s)
                              OR message_id LIKE %s
                              OR raw_payload->>'platform_user_id' LIKE %s
                          )
                        ORDER BY created_at ASC
                        """,
                        (harness.cleanup_user_ids, like_pat, like_pat),
                    )
                    inbound_rows = [dict(r) for r in await cur.fetchall()]

                    await cur.execute(
                        """
                        SELECT ilink_bot_id, platform_user_id, bot_generation, seq
                        FROM dalton_channel_weixin_context_tokens
                        WHERE ilink_bot_id LIKE %s OR platform_user_id LIKE %s
                        ORDER BY ilink_bot_id, platform_user_id
                        """,
                        (like_pat, like_pat),
                    )
                    ctx_rows = [dict(r) for r in await cur.fetchall()]

                    await cur.execute(
                        """
                        SELECT delivery_key, kind, platform, target_address, status,
                               sent_chunks, total_chunks, error_message
                        FROM dalton_channel_deliveries
                        WHERE platform = 'weixin'
                          AND (
                              user_id = ANY(%s)
                              OR delivery_key LIKE %s
                              OR target_address LIKE %s
                          )
                        ORDER BY created_at ASC
                        """,
                        (harness.cleanup_user_ids, like_pat, like_pat),
                    )
                    delivery_rows = [dict(r) for r in await cur.fetchall()]

                    await cur.execute(
                        """
                        SELECT user_id, platform, platform_user_id, platform_username,
                               weixin_context_bot_id, weixin_context_bot_generation,
                               weixin_context_seq,
                               (encrypted_weixin_context_token IS NOT NULL) AS has_encrypted_ctx
                        FROM dalton_channel_identities
                        WHERE platform = 'weixin'
                          AND (
                              user_id = ANY(%s)
                              OR platform_user_id LIKE %s
                          )
                        ORDER BY user_id
                        """,
                        (harness.cleanup_user_ids, like_pat),
                    )
                    identity_rows = [dict(r) for r in await cur.fetchall()]

            return {
                "inbound_events": inbound_rows,
                "context_tokens": ctx_rows,
                "deliveries": delivery_rows,
                "identities": identity_rows,
            }

        @app.post("/api/v1/workspaces/home")
        async def _ensure_home_workspace(request: Request) -> dict[str, Any]:
            uid = (request.headers.get("X-User-Id") or default_admin).strip()
            ws_id = get_home_workspace_id(uid)
            return {"workspace_id": ws_id, "status": "running"}

        @app.post("/api/v1/workspaces/{workspace_id}/start")
        async def _start_workspace(workspace_id: str) -> dict[str, Any]:
            return {"workspace_id": workspace_id, "status": "running"}

        async def _dispatch_synthetic_sse_turn(
            request: Request, explicit_thread_id: str | None = None
        ) -> StreamingResponse:
            assert harness.pool is not None
            uid = (request.headers.get("X-User-Id") or default_admin).strip()
            body = await request.json()
            ws_id = str(body.get("workspace_id") or get_home_workspace_id(uid))
            raw_ext = body.get("external_thread_id")
            ext_tid = str(raw_ext).strip() if raw_ext else None
            raw_platform = body.get("platform") or body.get("turn_platform")
            platform = str(raw_platform).strip() if raw_platform else None
            run_id = str(uuid4())

            msgs = body.get("messages") or []
            user_text = ""
            if msgs and isinstance(msgs[0], dict):
                c = msgs[0].get("content")
                if isinstance(c, str):
                    user_text = c
                elif isinstance(c, list):
                    user_text = " ".join(
                        str(item.get("text") or "")
                        for item in c
                        if isinstance(item, dict)
                    )

            # Thread index is per workspace (COALESCE(MAX, -1)+1, retry the
            # unique_thread_index_per_workspace race). Turn index is per thread
            # (GREATEST of query/response maxima + 1). Both run in one
            # transaction because the pool is autocommit. Append reuses the
            # existing thread and does not allocate another thread_index.
            async with harness.pool.connection() as conn:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            INSERT INTO workspaces (
                                workspace_id, user_id, name, status, created_at, updated_at
                            )
                            VALUES (%s, %s, %s, 'running', NOW(), NOW())
                            ON CONFLICT (workspace_id) DO NOTHING
                            """,
                            (ws_id, uid, f"{uid}_ws"),
                        )
                        await cur.execute(
                            """
                            SELECT workspace_id FROM workspaces
                            WHERE workspace_id = %s
                            FOR UPDATE
                            """,
                            (ws_id,),
                        )
                        if await cur.fetchone() is None:
                            raise HTTPException(
                                status_code=404, detail="workspace not found"
                            )
                    if ws_id not in harness.cleanup_workspace_ids:
                        harness.cleanup_workspace_ids.append(ws_id)

                    if explicit_thread_id:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                """
                                SELECT conversation_thread_id
                                FROM conversation_threads
                                WHERE conversation_thread_id = %s
                                FOR UPDATE
                                """,
                                (explicit_thread_id,),
                            )
                            existing = await cur.fetchone()
                            if not existing:
                                raise HTTPException(
                                    status_code=404, detail="thread not found"
                                )
                            thread_id = str(existing["conversation_thread_id"])
                            await cur.execute(
                                """
                                SELECT w.workspace_id
                                FROM workspaces w
                                JOIN conversation_threads t
                                  ON t.workspace_id = w.workspace_id
                                WHERE t.conversation_thread_id = %s
                                FOR SHARE OF w
                                """,
                                (thread_id,),
                            )
                    else:
                        thread_id = str(uuid4())
                        inserted = False
                        for attempt in range(_THREAD_INDEX_ATTEMPTS):
                            async with conn.cursor() as cur:
                                await cur.execute(
                                    """
                                    SELECT COALESCE(MAX(thread_index), -1) + 1
                                           AS next_index
                                    FROM conversation_threads
                                    WHERE workspace_id = %s
                                    """,
                                    (ws_id,),
                                )
                                thread_index = int((await cur.fetchone())["next_index"])
                            try:
                                async with conn.transaction():
                                    async with conn.cursor() as cur:
                                        await cur.execute(
                                            """
                                            INSERT INTO conversation_threads (
                                                conversation_thread_id, workspace_id,
                                                user_id, external_id, platform,
                                                current_status, thread_index,
                                                created_at, updated_at
                                            )
                                            VALUES (
                                                %s, %s, %s, %s, %s,
                                                'in_progress', %s, NOW(), NOW()
                                            )
                                            """,
                                            (
                                                thread_id,
                                                ws_id,
                                                uid,
                                                ext_tid,
                                                platform,
                                                thread_index,
                                            ),
                                        )
                                inserted = True
                                break
                            except psycopg.errors.UniqueViolation as exc:
                                constraint = _unique_constraint_name(exc)
                                if constraint == _EXTERNAL_ID_INDEX:
                                    raise HTTPException(
                                        status_code=409,
                                        detail={
                                            "error_type": "external_id_conflict",
                                            "code": "external_id_conflict",
                                            "message": (
                                                "This (platform, external_id) is "
                                                "already linked to another thread."
                                            ),
                                            "platform": platform,
                                            "external_id": ext_tid,
                                        },
                                    ) from exc
                                if (
                                    constraint != _THREAD_INDEX_CONSTRAINT
                                    or attempt == _THREAD_INDEX_ATTEMPTS - 1
                                ):
                                    raise
                        if not inserted:
                            raise HTTPException(
                                status_code=409,
                                detail="thread_index conflict",
                            )
                        async with conn.cursor() as cur:
                            await cur.execute(
                                """
                                SELECT 1 FROM conversation_threads
                                WHERE conversation_thread_id = %s
                                FOR UPDATE
                                """,
                                (thread_id,),
                            )

                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            SELECT GREATEST(
                                COALESCE((
                                    SELECT MAX(turn_index) FROM conversation_queries
                                    WHERE conversation_thread_id = %s
                                ), -1),
                                COALESCE((
                                    SELECT MAX(turn_index) FROM conversation_responses
                                    WHERE conversation_thread_id = %s
                                ), -1)
                            ) + 1 AS next_turn
                            """,
                            (thread_id, thread_id),
                        )
                        turn_index = int((await cur.fetchone())["next_turn"])
                        await cur.execute(
                            """
                            INSERT INTO conversation_responses (
                                conversation_response_id, conversation_thread_id,
                                turn_index, status, user_id, created_at
                            )
                            VALUES (%s, %s, %s, 'in_progress', %s, NOW())
                            """,
                            (run_id, thread_id, turn_index, uid),
                        )
                        await cur.execute(
                            """
                            UPDATE conversation_threads
                            SET current_status = 'in_progress', updated_at = NOW()
                            WHERE conversation_thread_id = %s
                            """,
                            (thread_id,),
                        )

            if thread_id not in harness.cleanup_thread_ids:
                harness.cleanup_thread_ids.append(thread_id)

            reply_text = f"Chief of Staff reply to: {user_text.strip() or 'message'}"

            async def _sse_gen():
                yield (
                    "event: turn_start\n"
                    f"data: {json.dumps({'thread_id': thread_id, 'run_id': run_id, 'turn_index': turn_index})}\n\n"
                )
                yield (
                    "event: message_chunk\n"
                    f"data: {json.dumps({'content': reply_text, 'role': 'assistant', 'content_type': 'text'})}\n\n"
                )
                assert harness.pool is not None
                async with harness.pool.connection() as conn2:
                    async with conn2.cursor() as cur2:
                        await cur2.execute(
                            """
                            UPDATE conversation_responses
                            SET status = 'completed'
                            WHERE conversation_response_id = %s
                              AND status = 'in_progress'
                            """,
                            (run_id,),
                        )
                        await cur2.execute(
                            """
                            UPDATE conversation_threads
                            SET current_status = 'completed', updated_at = NOW()
                            WHERE conversation_thread_id = %s
                            """,
                            (thread_id,),
                        )
                yield (
                    "event: run_end\n"
                    f"data: {json.dumps({'thread_id': thread_id, 'run_id': run_id, 'turn_index': turn_index, 'outcome': 'completed'})}\n\n"
                )

            return StreamingResponse(_sse_gen(), media_type="text/event-stream")

        @app.post("/api/v1/threads/messages")
        async def _create_thread_messages(request: Request) -> StreamingResponse:
            return await _dispatch_synthetic_sse_turn(request, None)

        @app.post("/api/v1/threads/{thread_id}/messages")
        async def _append_thread_messages(
            thread_id: str, request: Request
        ) -> StreamingResponse:
            return await _dispatch_synthetic_sse_turn(request, thread_id)

        @app.get("/api/v1/threads/{thread_id}/status")
        async def _get_thread_status(thread_id: str) -> dict[str, Any]:
            assert harness.pool is not None
            async with harness.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT current_status FROM conversation_threads
                        WHERE conversation_thread_id = %s
                        """,
                        (thread_id,),
                    )
                    row = await cur.fetchone()
            if not row:
                return {"status": "not_found"}
            return {"status": row["current_status"], "thread_id": thread_id}

        @app.get("/api/v1/threads/{thread_id}/watch")
        async def _watch_thread(thread_id: str, request: Request) -> StreamingResponse:
            async def _watch_gen():
                yield f"event: heartbeat\ndata: {json.dumps({'thread_id': thread_id})}\n\n"
                while not await request.is_disconnected():
                    await asyncio.sleep(0.2)

            return StreamingResponse(_watch_gen(), media_type="text/event-stream")

        return app


async def _main() -> None:
    dsn = resolve_validated_test_dsn()
    harness = WeixinLiveBackendHarness(dsn)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    try:
        info = await harness.start()
        sys.stdout.write(json.dumps(info) + "\n")
        sys.stdout.flush()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except Exception:
                pass

        def _watch_stdin() -> None:
            try:
                data = sys.stdin.read(1)
                if not data:
                    stop_event.set()
            except Exception:
                stop_event.set()

        try:
            loop.add_reader(sys.stdin.fileno(), _watch_stdin)
        except Exception:
            pass

        await stop_event.wait()
    finally:
        try:
            loop.remove_reader(sys.stdin.fileno())
        except Exception:
            pass
        await harness.stop()


if __name__ == "__main__":
    asyncio.run(_main())
