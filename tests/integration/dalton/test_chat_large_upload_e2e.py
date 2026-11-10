"""End-to-end integration driver for chat composer large-file uploads (up to 200 MiB).

Exercises the real FastAPI upload router, PostgreSQL ``dalton_chat_file_uploads``
state machine, ``DALTON_ASR_STORAGE_*`` MinIO multipart storage under
``chat-uploads/``, pre-stream ``validate_and_claim_request_attachments``, and
``attach_request_files`` materialization into a real Docker ``PTCSandbox`` via
``pull_direct`` — without any LLM or external messaging calls.

Suggested invocation by main:
    DALTON_ENABLED=true DALTON_CHAT_UPLOAD_REQUIRE_INFRA=1 \\
      uv run pytest tests/integration/dalton/test_chat_large_upload_e2e.py -m integration -v --tb=short
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import pkgutil
import shlex
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import create_autospec, patch
from urllib.parse import quote_plus, urlsplit, urlunsplit
from uuid import NAMESPACE_DNS, uuid4, uuid5

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI, HTTPException, Request
import httpx
import psycopg
from psycopg_pool import AsyncConnectionPool
import pytest
import pytest_asyncio

from ptc_agent.config.core import (
    CoreConfig,
    DaytonaConfig,
    DockerConfig,
    E2BConfig,
    FilesystemConfig,
    LoggingConfig,
    MCPConfig,
    SandboxConfig,
    SecurityConfig,
)
from ptc_agent.agent.middleware.steering import _steering_message
from ptc_agent.core.paths import WorkspaceLayout
from ptc_agent.core.project_context import ProjectContext, current_project
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

import src.dalton.database as dalton_database_pkg
import src.dalton.database.chat_uploads as chat_uploads_db
from src.dalton import chat_upload_storage as storage
from src.dalton import sharing as dalton_sharing
from src.dalton import transcription_storage as asr_store
from src.dalton.app.chat_uploads import router as chat_uploads_router
from src.dalton.services.chat_uploads import (
    CHAT_UPLOAD_PART_SIZE,
    MAX_ACTIVE_USER_UPLOADS,
    MAX_CHAT_UPLOAD_BYTES,
    ChatUploadSendScope,
    build_chat_upload_history_metadata,
    build_sandbox_upload_rel_path,
    chat_upload_claim_key,
    cleanup_deleted_workspace_chat_uploads,
    confirm_request_chat_uploads,
    ensure_workspace_chat_uploads_present,
    materialize_chat_uploads,
    release_request_chat_uploads,
    retire_deleted_workspace_chat_uploads,
    sweep_expired_chat_uploads_once,
    validate_and_claim_request_attachments,
)
from src.dalton.settings import get_dalton_settings
import src.server.database as server_database_pkg
from src.server.database.computer import (
    create_computer,
    try_bind_computer_provider_ref,
    try_claim_computer_for_start,
    update_computer_status,
)
from src.server.database.conversation.replay_rows import get_replay_thread_data
from src.server.database.conversation.threads_write import create_thread
from src.server.database.home_workspace import get_home_workspace_id
from src.server.database.pool import _configure_postgres_connection
from src.server.database.runs import lifecycle as tl_db
from src.server.database.runs import outbox as outbox_db
from src.server.database.session_lock import close_shared_lock_session
from src.server.database.user import create_user
from src.server.database.workspace import (
    bind_workspace_to_computer,
    create_workspace,
    create_workspace_on_computer,
    delete_workspace as db_delete_workspace,
    files_restore_incomplete,
    get_or_create_home_workspace,
)
from src.server.handlers.chat.admission_gate import wait_or_steer
from src.server.handlers.chat.attachments import attach_request_files
from src.server.handlers.chat.request_prep import serialize_context_metadata
from src.server.handlers.chat.steering import open_with_steers_ahead
from src.server.models.chat import ChatMessage, ChatRequest
from src.server.services.computer_disk import CRITICAL_FREE_BYTES
from src.server.services.history.replay import stub_page
from src.server.services.persistence.file import (
    FilePersistenceService,
    RestoreIdentityLost,
)
from src.server.services.report_back import notify_turn
from src.server.services.report_back.steer import SteeredReportInbox
from src.server.services.report_back.steer_executor import execute_steer
from src.server.services.runs.admission import RunScope, begin_run
from src.server.services.runs.executor import LocalRunExecutor
from src.server.services.workspace_manager import WorkspaceManager
from src.server.utils.api import get_current_user_id

pytestmark = pytest.mark.integration

_MIB = 1024 * 1024
_SIZE_36_MIB = 36 * _MIB
_SIZE_200_MIB = MAX_CHAT_UPLOAD_BYTES  # 209_715_200 bytes (exact 200 MiB)

_DSN_ENV_VARS = (
    "DALTON_CHAT_UPLOAD_TEST_DSN",
    "DALTON_TEST_DB_DSN",
    "TEST_DATABASE_URL",
)


def _rk(label: str) -> str:
    """Deterministic UUIDv5 request_key matching ChatRequest's UUID validator."""
    return str(uuid5(NAMESPACE_DNS, f"chat-upload-e2e:{label}"))


@pytest.fixture(scope="session", autouse=True)
def dalton_schema():
    """Override directory conftest autouse fixture so isolated test DSNs upgrade without DROP SCHEMA."""
    yield


@pytest.fixture(autouse=True)
def cleanup_dalton_tables():
    """Override directory conftest autouse fixture so isolated test DSNs do not open default test_db_pool."""
    yield


def _skip_or_fail_infra(reason: str) -> None:
    if os.environ.get("DALTON_CHAT_UPLOAD_REQUIRE_INFRA", "").strip() in ("1", "true", "yes"):
        pytest.fail(f"Required integration infrastructure unavailable: {reason}")
    pytest.skip(reason)


def _assert_isolated_test_dsn(dsn: str, *, source: str) -> str:
    parsed = urlsplit(dsn)
    db_name = (parsed.path or "").strip().lstrip("/")
    if not db_name or not db_name.endswith("_test"):
        pytest.fail(
            f"Refusing to run chat upload integration tests against non-test database "
            f"{db_name!r} (from {source}; database name must end with '_test')"
        )
    prod_db = (os.environ.get("DB_NAME") or "").strip().lstrip("/")
    if prod_db and not prod_db.endswith("_test") and db_name == prod_db:
        pytest.fail(
            f"Refusing to run chat upload integration tests against production DB_NAME={prod_db!r}"
        )
    return dsn


def _resolve_isolated_test_dsn() -> str:
    for var in _DSN_ENV_VARS:
        raw = (os.environ.get(var) or "").strip()
        if raw:
            dsn = _assert_isolated_test_dsn(raw, source=var)
            try:
                with psycopg.connect(dsn, connect_timeout=5):
                    return dsn
            except psycopg.OperationalError as exc:
                _skip_or_fail_infra(f"Test database from {var} is unreachable: {exc}")

    host = (os.environ.get("TEST_DB_HOST") or "localhost").strip()
    port = (os.environ.get("TEST_DB_PORT") or "5432").strip()
    name = (os.environ.get("TEST_DB_NAME") or "langalpha_test").strip().lstrip("/")
    user = os.environ.get("TEST_DB_USER") or "postgres"
    password = os.environ.get("TEST_DB_PASSWORD") or "postgres"
    sslmode = (os.environ.get("TEST_DB_SSLMODE") or "prefer").strip()
    fallback_dsn = (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{name}"
        f"?sslmode={sslmode}"
    )
    _assert_isolated_test_dsn(fallback_dsn, source="TEST_DB_NAME")
    try:
        with psycopg.connect(fallback_dsn, connect_timeout=3):
            return fallback_dsn
    except psycopg.OperationalError as exc:
        _skip_or_fail_infra(
            f"None of {_DSN_ENV_VARS} is set and default test DB {host}:{port}/{name} is unreachable: {exc}"
        )
    raise AssertionError("Unreachable")


def _to_sqlalchemy_psycopg_url(dsn: str) -> str:
    if dsn.startswith("postgresql+psycopg://"):
        sa_url = dsn
    elif dsn.startswith("postgres://"):
        sa_url = dsn.replace("postgres://", "postgresql+psycopg://", 1)
    elif dsn.startswith("postgresql://"):
        sa_url = dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    else:
        sa_url = dsn
    return sa_url.replace("%", "%%")


async def _ensure_migrations_applied(dsn: str) -> None:
    """Apply upstream head and Dalton d033 on the isolated _test database without dropping schema."""
    _assert_isolated_test_dsn(dsn, source="alembic_upgrade")
    project_root = Path(os.path.realpath(Path(__file__).resolve().parents[3]))
    sa_url = _to_sqlalchemy_psycopg_url(dsn)

    upstream_cfg = Config(str(project_root / "alembic.ini"))
    upstream_cfg.set_main_option("script_location", str(project_root / "migrations"))
    upstream_cfg.set_main_option("sqlalchemy.url", sa_url)
    upstream_head = ScriptDirectory.from_config(upstream_cfg).get_current_head()

    dalton_cfg = Config(str(project_root / "src" / "dalton" / "alembic.ini"))
    dalton_cfg.set_main_option(
        "script_location", str(project_root / "src" / "dalton" / "migrations")
    )
    dalton_cfg.set_main_option("sqlalchemy.url", sa_url)

    current_upstream: str | None = None
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT current_database(), to_regclass('public.alembic_version')")
            meta = await cur.fetchone()
            assert meta is not None and str(meta[0]).endswith("_test"), (
                f"Connected database {meta!r} does not end with '_test'"
            )
            if meta[1] is not None:
                await cur.execute("SELECT version_num FROM alembic_version LIMIT 1")
                vrow = await cur.fetchone()
                if vrow:
                    current_upstream = str(vrow[0])

    if current_upstream != upstream_head:
        await asyncio.to_thread(command.upgrade, upstream_cfg, "head")

    await asyncio.to_thread(command.upgrade, dalton_cfg, "d033")

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    current_database(),
                    to_regclass('public.dalton_chat_file_uploads'),
                    to_regclass('public.alembic_version_dalton'),
                    EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'dalton_chat_file_uploads'
                          AND column_name = 'claim_token'
                    ),
                    EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'dalton_chat_file_uploads'
                          AND column_name = 'pending_claims'
                    )
                """
            )
            verified = await cur.fetchone()
            assert verified is not None
            assert str(verified[0]).endswith("_test")
            assert verified[1] is not None, "Dalton migration d033 did not create dalton_chat_file_uploads"
            assert verified[2] is not None, "alembic_version_dalton table missing after d033 upgrade"
            if not (verified[3] and verified[4]):
                await asyncio.to_thread(command.downgrade, dalton_cfg, "d035")
                await asyncio.to_thread(command.upgrade, dalton_cfg, "d033")

            await cur.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'dalton_chat_file_uploads'
                          AND column_name = 'claim_token'
                    ),
                    EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'dalton_chat_file_uploads'
                          AND column_name = 'pending_claims'
                    ),
                    EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conname = 'chk_dalton_chat_file_uploads_cleaned_terminal'
                    )
                """
            )
            schema_flags = await cur.fetchone()
            assert schema_flags == (True, True, True), f"Incomplete d033 schema on _test DB: {schema_flags!r}"


@pytest_asyncio.fixture(scope="session")
async def chat_upload_db_pool():
    """Isolated session pool backed by an explicit _test database with upstream head + d033 applied."""
    dsn = _resolve_isolated_test_dsn()
    await _ensure_migrations_applied(dsn)

    pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=8,
        configure=_configure_postgres_connection,
        check=AsyncConnectionPool.check_connection,
        open=False,
    )
    await pool.open()
    await pool.wait()
    try:
        yield pool, dsn
    finally:
        await pool.close()


def _derive_container_signing_endpoint(endpoint_url: str) -> str:
    """Map host loopback MinIO endpoints to ``host.docker.internal`` for Docker containers."""
    parsed = urlsplit(endpoint_url)
    host = (parsed.hostname or "").lower()
    if host not in ("localhost", "127.0.0.1"):
        return endpoint_url
    port_suffix = f":{parsed.port}" if parsed.port else ""
    netloc = f"host.docker.internal{port_suffix}"
    return urlunsplit((parsed.scheme or "http", netloc, parsed.path, parsed.query, parsed.fragment))


def _iter_deterministic_parts(
    total_bytes: int,
    *,
    part_size: int = CHAT_UPLOAD_PART_SIZE,
    seed_tag: bytes = b"chat-upload",
) -> Iterator[tuple[int, bytes, str]]:
    """Yield ``(part_number, part_bytes, part_sha256_hex)`` holding at most one 8 MiB chunk at a time."""
    part_count = (total_bytes + part_size - 1) // part_size
    pattern_block_len = 64 * 1024
    for part_number in range(1, part_count + 1):
        chunk_len = (
            part_size
            if part_number < part_count
            else total_bytes - part_size * (part_count - 1)
        )
        block_seed = hashlib.sha256(seed_tag + f":{part_number}".encode("ascii")).digest()
        repeats = (pattern_block_len // len(block_seed)) + 1
        unit_64k = (block_seed * repeats)[:pattern_block_len]
        full_blocks, rem = divmod(chunk_len, pattern_block_len)
        buf = bytearray(chunk_len)
        offset = 0
        for _ in range(full_blocks):
            buf[offset : offset + pattern_block_len] = unit_64k
            offset += pattern_block_len
        if rem:
            buf[offset : offset + rem] = unit_64k[:rem]
        raw = bytes(buf)
        part_sha = hashlib.sha256(raw).hexdigest()
        yield part_number, raw, part_sha


def _make_docker_core_config(working_dir: str) -> CoreConfig:
    return CoreConfig(
        sandbox=SandboxConfig(
            provider="docker",
            daytona=DaytonaConfig(
                api_key="unused",
                base_url="https://app.daytona.io/api",
                snapshot_enabled=True,
                snapshot_name="langalpha",
                snapshot_auto_create=False,
            ),
            docker=DockerConfig(
                image=os.environ.get("DOCKER_SANDBOX_IMAGE", "langalpha-sandbox:latest"),
                dev_mode=False,
            ),
            e2b=E2BConfig(template_auto_create=False),
        ),
        security=SecurityConfig(
            max_execution_time=120,
            max_code_length=50000,
            max_file_size=MAX_CHAT_UPLOAD_BYTES,
            enable_code_validation=False,
            allowed_imports=[],
            blocked_patterns=[],
        ),
        mcp=MCPConfig(servers=[], tool_discovery_enabled=False),
        logging=LoggingConfig(),
        filesystem=FilesystemConfig(
            working_directory=working_dir,
            allowed_directories=[working_dir, "/tmp"],
            denied_directories=[],
            enable_path_validation=True,
        ),
    )


@pytest_asyncio.fixture
async def chat_upload_infra(monkeypatch, chat_upload_db_pool):
    """Wire isolated Postgres _test pool (with d033), MinIO ``DALTON_ASR_STORAGE_*``, and FastAPI test app."""
    pool, test_dsn = chat_upload_db_pool

    # Colima cannot bind-mount macOS `/private/var/folders`; keep any temporary host
    # directories strictly under project `.agy/` via `os.path.realpath`.
    project_root = Path(os.path.realpath(Path(__file__).resolve().parents[3]))
    agy_tmp_dir = Path(
        os.path.realpath(project_root / ".agy" / f"tmp_chat_upload_e2e_{uuid4().hex[:8]}")
    )
    agy_tmp_dir.mkdir(parents=True, exist_ok=True)
    prev_tempdir = tempfile.tempdir
    monkeypatch.setenv("TMPDIR", str(agy_tmp_dir))
    monkeypatch.setenv("TEMP", str(agy_tmp_dir))
    monkeypatch.setenv("TMP", str(agy_tmp_dir))
    tempfile.tempdir = str(agy_tmp_dir)

    monkeypatch.setenv("DALTON_ENABLED", "true")
    monkeypatch.setenv("STORAGE_PROVIDER", "none")

    endpoint = (os.environ.get("DALTON_ASR_STORAGE_ENDPOINT_URL") or "").strip()
    if not storage.is_chat_upload_storage_configured():
        _skip_or_fail_infra(
            "DALTON_ASR_STORAGE_ENDPOINT_URL / BUCKET_NAME / ACCESS_KEY_ID / SECRET_ACCESS_KEY must be set"
        )

    explicit_signing = (os.environ.get("DALTON_ASR_STORAGE_SIGNING_ENDPOINT_URL") or "").strip()
    if not explicit_signing and endpoint:
        monkeypatch.setenv(
            "DALTON_ASR_STORAGE_SIGNING_ENDPOINT_URL",
            _derive_container_signing_endpoint(endpoint),
        )

    get_dalton_settings.cache_clear()
    asr_store._reset_clients_for_test()

    try:
        asr_store._get_client().head_bucket(Bucket=asr_store._bucket_name())
    except Exception as exc:
        asr_store._reset_clients_for_test()
        get_dalton_settings.cache_clear()
        shutil.rmtree(agy_tmp_dir, ignore_errors=True)
        tempfile.tempdir = prev_tempdir
        _skip_or_fail_infra(f"MinIO bucket unreachable at {endpoint!r}: {exc}")

    @asynccontextmanager
    async def _test_get_db_connection(conn=None):
        if conn is not None:
            yield conn
            return
        async with pool.connection() as owned:
            yield owned

    async def _test_session_connection():
        conn = await psycopg.AsyncConnection.connect(test_dsn)
        await _configure_postgres_connection(conn)
        return conn

    from src.server.database.pool import get_db_connection as _pool_gdc

    for _info in pkgutil.walk_packages(
        server_database_pkg.__path__, prefix="src.server.database."
    ):
        importlib.import_module(_info.name)
    for _info in pkgutil.walk_packages(
        dalton_database_pkg.__path__, prefix="src.dalton.database."
    ):
        importlib.import_module(_info.name)

    holders = [
        module
        for name, module in list(sys.modules.items())
        if name.startswith(("src.", "ptc_agent."))
        and getattr(module, "get_db_connection", None) is _pool_gdc
    ]

    app = FastAPI()

    async def _header_user_id(request: Request) -> str:
        uid = (request.headers.get("x-test-user-id") or "").strip()
        if not uid:
            raise HTTPException(status_code=401, detail="Missing x-test-user-id")
        return uid

    app.dependency_overrides[get_current_user_id] = _header_user_id
    app.include_router(chat_uploads_router)

    tracked_keys: set[str] = set()
    tracked_multiparts: list[tuple[str, str]] = []

    with ExitStack() as stack:
        for module in holders:
            stack.enter_context(
                patch.object(module, "get_db_connection", _test_get_db_connection)
            )
        stack.enter_context(
            patch.object(chat_uploads_db, "get_db_connection", _test_get_db_connection)
        )
        stack.enter_context(
            patch("src.server.database.pool.get_db_connection_string", lambda: test_dsn)
        )
        stack.enter_context(
            patch("src.server.database.pool.open_session_connection", _test_session_connection)
        )
        await close_shared_lock_session()

        async with _test_get_db_connection() as conn:
            await conn.execute("TRUNCATE TABLE dalton_chat_file_uploads CASCADE")

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=120.0
        ) as client:
            try:
                yield SimpleNamespace(
                    client=client,
                    db=_test_get_db_connection,
                    tracked_keys=tracked_keys,
                    tracked_multiparts=tracked_multiparts,
                )
            finally:
                for obj_key, store_uid in tracked_multiparts:
                    try:
                        storage.abort_multipart_upload(obj_key, store_uid)
                    except Exception:
                        pass
                for obj_key in tracked_keys:
                    try:
                        if storage.is_chat_upload_key(obj_key):
                            storage.delete_object(obj_key)
                        else:
                            asr_store.delete_object(obj_key)
                    except Exception:
                        pass
                try:
                    async with _test_get_db_connection() as conn:
                        await conn.execute("TRUNCATE TABLE dalton_chat_file_uploads CASCADE")
                        await conn.execute(
                            """
                            DELETE FROM hook_outbox
                            WHERE conversation_thread_id IN (
                                SELECT conversation_thread_id FROM conversation_threads
                                WHERE workspace_id IN (
                                    SELECT workspace_id FROM workspaces
                                    WHERE user_id IN (
                                        SELECT user_id FROM users WHERE email LIKE '%@chat-upload-e2e.test'
                                    )
                                )
                            )
                            """
                        )
                        await conn.execute(
                            """
                            DELETE FROM conversation_responses
                            WHERE conversation_thread_id IN (
                                SELECT conversation_thread_id FROM conversation_threads
                                WHERE workspace_id IN (
                                    SELECT workspace_id FROM workspaces
                                    WHERE user_id IN (
                                        SELECT user_id FROM users WHERE email LIKE '%@chat-upload-e2e.test'
                                    )
                                )
                            )
                            """
                        )
                        await conn.execute(
                            """
                            DELETE FROM conversation_queries
                            WHERE conversation_thread_id IN (
                                SELECT conversation_thread_id FROM conversation_threads
                                WHERE workspace_id IN (
                                    SELECT workspace_id FROM workspaces
                                    WHERE user_id IN (
                                        SELECT user_id FROM users WHERE email LIKE '%@chat-upload-e2e.test'
                                    )
                                )
                            )
                            """
                        )
                        await conn.execute(
                            """
                            DELETE FROM conversation_threads
                            WHERE workspace_id IN (
                                SELECT workspace_id FROM workspaces
                                WHERE user_id IN (
                                    SELECT user_id FROM users WHERE email LIKE '%@chat-upload-e2e.test'
                                )
                            )
                            """
                        )
                        await conn.execute(
                            """
                            DELETE FROM dalton_workspace_sharing
                            WHERE workspace_id IN (
                                SELECT workspace_id FROM workspaces
                                WHERE user_id IN (
                                    SELECT user_id FROM users WHERE email LIKE '%@chat-upload-e2e.test'
                                )
                            )
                            """
                        )
                        await conn.execute(
                            """
                            DELETE FROM workspace_files
                            WHERE workspace_id IN (
                                SELECT workspace_id FROM workspaces
                                WHERE user_id IN (
                                    SELECT user_id FROM users WHERE email LIKE '%@chat-upload-e2e.test'
                                )
                            )
                            """
                        )
                        await conn.execute(
                            """
                            UPDATE computers
                            SET origin_workspace_id = NULL
                            WHERE user_id IN (
                                SELECT user_id FROM users WHERE email LIKE '%@chat-upload-e2e.test'
                            )
                            """
                        )
                        await conn.execute(
                            """
                            DELETE FROM workspaces
                            WHERE user_id IN (
                                SELECT user_id FROM users WHERE email LIKE '%@chat-upload-e2e.test'
                            )
                            """
                        )
                        await conn.execute(
                            """
                            DELETE FROM computers
                            WHERE user_id IN (
                                SELECT user_id FROM users WHERE email LIKE '%@chat-upload-e2e.test'
                            )
                            """
                        )
                        await conn.execute(
                            """
                            DELETE FROM user_preferences
                            WHERE user_id IN (
                                SELECT user_id FROM users WHERE email LIKE '%@chat-upload-e2e.test'
                            )
                            """
                        )
                        await conn.execute(
                            "DELETE FROM users WHERE email LIKE '%@chat-upload-e2e.test'"
                        )
                finally:
                    await close_shared_lock_session()
                    asr_store._reset_clients_for_test()
                    get_dalton_settings.cache_clear()
                    tempfile.tempdir = prev_tempdir
                    shutil.rmtree(agy_tmp_dir, ignore_errors=True)


async def _start_docker_sandbox(*, dir_name: str | None = None) -> PTCSandbox:
    """Create a fresh Docker ``PTCSandbox`` from the local prebuilt image without installing packages.

    ``DockerConfig`` has no snapshot/prebuilt flag and ``DockerRuntime.image_prebuilt``
    is ``False``, so ``PTCSandbox.setup_sandbox_workspace()`` always attempts
    ``uv pip install --system`` and ``scrapling install``. Inspecting the existing
    local image first prevents ``DockerProvider`` from building or pulling, and
    running ``provider.create`` + ``_setup_workspace`` exercises the real container
    Python and filesystem primitives directly.
    """
    if shutil.which("docker") is None:
        _skip_or_fail_infra("docker binary not found on PATH")

    working_dir = os.environ.get("DOCKER_SANDBOX_WORK_DIR", "/home/workspace")
    config = _make_docker_core_config(working_dir)
    sandbox = PTCSandbox(config)
    try:
        docker_client = await sandbox.provider._get_client()
        try:
            await docker_client.images.inspect(config.sandbox.docker.image)
        except Exception as img_exc:
            await sandbox.cleanup()
            _skip_or_fail_infra(
                f"Prebuilt Docker image {config.sandbox.docker.image!r} not present locally "
                f"(refusing to build or pull): {img_exc}"
            )

        sandbox_env = sandbox._build_sandbox_env_vars({})
        sandbox.runtime = await sandbox.provider.create(
            env_vars=sandbox_env or None,
            mcp_packages=None,
        )
        assert sandbox.runtime is not None
        sandbox.sandbox_id = sandbox.runtime.id
        await sandbox._setup_workspace(dir_name)
    except BaseException as exc:
        if isinstance(exc, pytest.skip.Exception):
            raise
        await sandbox.cleanup()
        _skip_or_fail_infra(f"Failed to start Docker PTCSandbox: {exc}")
    return sandbox


@pytest_asyncio.fixture
async def docker_ptc_sandbox():
    """Launch an isolated real Docker ``PTCSandbox`` and destroy it on teardown."""
    sandbox = await _start_docker_sandbox()
    try:
        yield sandbox
    finally:
        await sandbox.cleanup()


async def _upload_streamed_file_via_http(
    client: httpx.AsyncClient,
    *,
    user_id: str,
    workspace_id: str | None,
    filename: str,
    mime_type: str,
    total_bytes: int,
    seed_tag: bytes,
    tracked_keys: set[str],
    tracked_multiparts: list[tuple[str, str]],
) -> tuple[dict, str, str]:
    """Upload a deterministic multi-part payload via HTTP without holding the full file in RAM."""
    headers = {"x-test-user-id": user_id}
    init_resp = await client.post(
        "/api/v1/dalton/chat-uploads",
        headers=headers,
        json={
            "workspace_id": workspace_id,
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": total_bytes,
        },
    )
    assert init_resp.status_code == 200, init_resp.text
    init_body = init_resp.json()
    upload_id = init_body["upload_id"]

    row = await chat_uploads_db.get_upload(upload_id)
    assert row is not None
    object_key = str(row["object_key"])
    store_upload_id = str(row["store_upload_id"])
    assert object_key.startswith("chat-uploads/")
    tracked_keys.add(object_key)
    tracked_multiparts.append((object_key, store_upload_id))

    whole_hasher = hashlib.sha256()
    sent_parts = 0
    for part_number, chunk_bytes, part_sha in _iter_deterministic_parts(
        total_bytes, seed_tag=seed_tag
    ):
        whole_hasher.update(chunk_bytes)
        put_resp = await client.put(
            f"/api/v1/dalton/chat-uploads/{upload_id}/parts/{part_number}",
            headers={
                **headers,
                "content-type": "application/octet-stream",
                "x-part-sha256": part_sha,
            },
            content=chunk_bytes,
        )
        assert put_resp.status_code == 200, put_resp.text
        sent_parts += 1
        del chunk_bytes

    assert sent_parts == init_body["part_count"]
    expected_sha256 = whole_hasher.hexdigest()

    complete_resp = await client.post(
        f"/api/v1/dalton/chat-uploads/{upload_id}/complete",
        headers=headers,
        json={"sha256": expected_sha256},
    )
    assert complete_resp.status_code == 200, complete_resp.text
    complete_body = complete_resp.json()
    assert complete_body["state"] == "ready"
    assert complete_body["sha256"] == expected_sha256

    # Idempotent retry of /complete returns the same ready record without re-completing.
    retry_complete = await client.post(
        f"/api/v1/dalton/chat-uploads/{upload_id}/complete",
        headers=headers,
        json={"sha256": expected_sha256},
    )
    assert retry_complete.status_code == 200, retry_complete.text
    assert retry_complete.json()["sha256"] == expected_sha256

    return complete_body, object_key, expected_sha256


async def _stage_parts_without_completing(
    client: httpx.AsyncClient,
    *,
    user_id: str,
    workspace_id: str,
    filename: str,
    mime_type: str,
    total_bytes: int,
    seed_tag: bytes,
    tracked_keys: set[str],
    tracked_multiparts: list[tuple[str, str]],
) -> tuple[str, str, str, str]:
    """Initiate a chat upload and PUT all parts into MinIO without calling /complete."""
    headers = {"x-test-user-id": user_id}
    init_resp = await client.post(
        "/api/v1/dalton/chat-uploads",
        headers=headers,
        json={
            "workspace_id": workspace_id,
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": total_bytes,
        },
    )
    assert init_resp.status_code == 200, init_resp.text
    init_body = init_resp.json()
    upload_id = str(init_body["upload_id"])

    row = await chat_uploads_db.get_upload(upload_id)
    assert row is not None
    object_key = str(row["object_key"])
    store_upload_id = str(row["store_upload_id"])
    tracked_keys.add(object_key)
    tracked_multiparts.append((object_key, store_upload_id))

    whole_hasher = hashlib.sha256()
    for part_number, chunk_bytes, part_sha in _iter_deterministic_parts(
        total_bytes, seed_tag=seed_tag
    ):
        whole_hasher.update(chunk_bytes)
        put_resp = await client.put(
            f"/api/v1/dalton/chat-uploads/{upload_id}/parts/{part_number}",
            headers={
                **headers,
                "content-type": "application/octet-stream",
                "x-part-sha256": part_sha,
            },
            content=chunk_bytes,
        )
        assert put_resp.status_code == 200, put_resp.text

    return upload_id, object_key, store_upload_id, whole_hasher.hexdigest()


@pytest.mark.asyncio
class TestChatLargeUploadRealInfra:
    @pytest.mark.asyncio
    async def test_upload_36mib_and_exact_200mib_materialize_and_verify_hashes(
        self,
        chat_upload_infra,
        docker_ptc_sandbox: PTCSandbox,
    ) -> None:
        """Upload >30 MiB (36 MiB) and exact 200 MiB, verify MinIO + Docker sandbox SHA-256 & attachment note."""
        uid_tag = uuid4().hex[:8]
        user = await create_user(
            user_id=f"u-large-e2e-{uid_tag}",
            email=f"large-e2e-{uid_tag}@chat-upload-e2e.test",
            name="Large Upload Tester",
        )
        ws = await create_workspace(
            user_id=user["user_id"],
            name="Quant Research",
            description="E2E large upload workspace",
            status="running",
        )
        workspace_id = str(ws["workspace_id"])
        project = ProjectContext(workspace_id=workspace_id, dir_name="quant_research")
        await docker_ptc_sandbox._ensure_workspace_dirs(project.dir_name)

        up36, key36, sha36 = await _upload_streamed_file_via_http(
            chat_upload_infra.client,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            filename="../../unsafe_dir/ticks_36m.parquet",
            mime_type="application/octet-stream",
            total_bytes=_SIZE_36_MIB,
            seed_tag=b"e2e-36m",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )
        up200, key200, sha200 = await _upload_streamed_file_via_http(
            chat_upload_infra.client,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            filename="full_snapshot_200m.zip",
            mime_type="application/zip",
            total_bytes=_SIZE_200_MIB,
            seed_tag=b"e2e-200m",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )

        stored36 = storage.compute_stored_object_digest(key36)
        stored200 = storage.compute_stored_object_digest(key200)
        assert stored36 == (_SIZE_36_MIB, sha36)
        assert stored200 == (_SIZE_200_MIB, sha200)

        additional_context = [
            {
                "type": "chat_upload",
                "upload_id": up36["upload_id"],
                "name": up36["filename"],
                "mime_type": up36["mime_type"],
                "size": _SIZE_36_MIB,
            },
            {
                "type": "chat_upload",
                "upload_id": up200["upload_id"],
                "name": up200["filename"],
                "mime_type": up200["mime_type"],
                "size": _SIZE_200_MIB,
            },
        ]
        chat_req = ChatRequest(
            messages=[ChatMessage(role="user", content="Verify both datasets")],
            workspace_id=workspace_id,
            additional_context=additional_context,
        )

        # 1. Pre-stream provisional claim (`messaging.py` path) leaves used_at unset until durable admission
        claimed = await validate_and_claim_request_attachments(
            chat_req.additional_context,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            claim_key="req:e2e-hash-1",
        )
        assert [c["upload_id"] for c in claimed] == [up36["upload_id"], up200["upload_id"]]
        assert all(c["used_at"] is None for c in claimed)

        confirmed = await confirm_request_chat_uploads(
            chat_req.additional_context,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            claim_key="req:e2e-hash-1",
        )
        assert all(c["used_at"] is not None and c["expires_at"] is None for c in confirmed)

        # 2. History & metadata serialization carry zero file bytes / base64
        query_meta: dict[str, object] = {}
        serialize_context_metadata(chat_req, query_meta, "Verify both datasets")
        ctx_meta = query_meta.get("additional_context")
        hist_meta = await build_chat_upload_history_metadata(
            chat_req.additional_context,
            user_id=user["user_id"],
            workspace_id=workspace_id,
        )
        assert len(hist_meta) == 2
        assert "data:" not in str(ctx_meta)
        assert "data:" not in str(hist_meta)

        # 3. Actual `attach_request_files` sandbox materialization (`pull_direct` path)
        messages = [{"role": "user", "content": "Verify both datasets"}]
        session = SimpleNamespace(sandbox=docker_ptc_sandbox)
        llm_cfg = SimpleNamespace(input_modalities=["text", "image", "pdf"])
        updated_messages = await attach_request_files(
            messages,
            chat_req,
            session,
            "gpt-5",
            llm_cfg,
            project=project,
            user_id=user["user_id"],
            workspace_id=workspace_id,
        )

        reminder_text = str(updated_messages[-1]["content"])
        assert "<system-reminder>" in reminder_text
        assert "data:" not in reminder_text
        assert len(reminder_text) < 2048

        row36 = await chat_uploads_db.get_upload(up36["upload_id"])
        row200 = await chat_uploads_db.get_upload(up200["upload_id"])
        assert row36 is not None and row36["used_at"] is not None
        assert row200 is not None and row200["used_at"] is not None

        rel36 = build_sandbox_upload_rel_path(row36)
        rel200 = build_sandbox_upload_rel_path(row200)
        assert rel36.startswith("work/uploads/ticks_36m_")
        assert ".." not in rel36
        assert rel36 in reminder_text
        assert rel200 in reminder_text

        abs36 = docker_ptc_sandbox.normalize_path(rel36, project=project)
        abs200 = docker_ptc_sandbox.normalize_path(rel200, project=project)

        verify_script = (
            "python3 -c '"
            "import hashlib, json, os, sys\n"
            "out = {}\n"
            "for p in sys.argv[1:]:\n"
            "    h = hashlib.sha256()\n"
            "    sz = 0\n"
            "    with open(p, \"rb\") as f:\n"
            "        while True:\n"
            "            b = f.read(1048576)\n"
            "            if not b:\n"
            "                break\n"
            "            sz += len(b)\n"
            "            h.update(b)\n"
            "    out[p] = [sz, h.hexdigest()]\n"
            "print(json.dumps(out))\n"
            f"' {abs36!r} {abs200!r}"
        )
        bash_res = await docker_ptc_sandbox.execute_bash_command(verify_script, timeout=120)
        assert bash_res.get("success") is True, bash_res

        digests = json.loads((bash_res.get("stdout") or "").strip().splitlines()[-1])
        assert digests[abs36] == [_SIZE_36_MIB, sha36]
        assert digests[abs200] == [_SIZE_200_MIB, sha200]

    @pytest.mark.asyncio
    async def test_cancel_and_expired_cleanup_remove_minio_parts_and_preserve_asr_objects(
        self,
        chat_upload_infra,
    ) -> None:
        """Cancel aborts multipart parts; expired cleanup removes chat-uploads/ objects without touching ASR keys."""
        uid_tag = uuid4().hex[:8]
        user = await create_user(
            user_id=f"u-cancel-e2e-{uid_tag}",
            email=f"cancel-e2e-{uid_tag}@chat-upload-e2e.test",
            name="Cancel Tester",
        )
        ws = await create_workspace(
            user_id=user["user_id"],
            name="Cleanup Workspace",
            description="Cleanup test",
            status="running",
        )
        workspace_id = str(ws["workspace_id"])
        headers = {"x-test-user-id": user["user_id"]}

        # Seed a sentinel object under `transcriptions/` to prove ASR objects stay untouched.
        sentinel_key = f"transcriptions/e2e-sentinel-{uuid4().hex}.wav"
        sentinel_upload = asr_store.open_multipart_upload(sentinel_key, content_type="audio/wav")
        assert sentinel_upload is not None
        chat_upload_infra.tracked_keys.add(sentinel_key)
        sentinel_resp = asr_store._get_client().upload_part(
            Bucket=asr_store._bucket_name(),
            Key=asr_store.resolve_full_key(sentinel_key),
            UploadId=sentinel_upload,
            PartNumber=1,
            ContentLength=16,
            Body=b"RIFF0000WAVEfmt ",
        )
        assert asr_store.complete_multipart_upload(
            sentinel_key, sentinel_upload, [(1, sentinel_resp["ETag"])]
        )
        assert asr_store.object_size(sentinel_key) == 16

        # 1. Start a 3-part upload, upload 2 parts of 8 MiB, then DELETE (cancel).
        init_resp = await chat_upload_infra.client.post(
            "/api/v1/dalton/chat-uploads",
            headers=headers,
            json={
                "workspace_id": workspace_id,
                "filename": "aborted_bundle.tar",
                "mime_type": "application/x-tar",
                "size_bytes": 20 * _MIB,
            },
        )
        assert init_resp.status_code == 200, init_resp.text
        cancel_upload_id = init_resp.json()["upload_id"]
        cancel_row = await chat_uploads_db.get_upload(cancel_upload_id)
        assert cancel_row is not None
        cancel_key = str(cancel_row["object_key"])
        cancel_store_uid = str(cancel_row["store_upload_id"])
        chat_upload_infra.tracked_keys.add(cancel_key)
        chat_upload_infra.tracked_multiparts.append((cancel_key, cancel_store_uid))

        for part_num, chunk_bytes, part_sha in _iter_deterministic_parts(
            20 * _MIB, seed_tag=b"cancel-test"
        ):
            if part_num > 2:
                break
            put_resp = await chat_upload_infra.client.put(
                f"/api/v1/dalton/chat-uploads/{cancel_upload_id}/parts/{part_num}",
                headers={**headers, "x-part-sha256": part_sha},
                content=chunk_bytes,
            )
            assert put_resp.status_code == 200, put_resp.text

        parts_before = storage.list_uploaded_parts(cancel_key, cancel_store_uid)
        assert parts_before is not None and len(parts_before) == 2

        del_resp = await chat_upload_infra.client.delete(
            f"/api/v1/dalton/chat-uploads/{cancel_upload_id}",
            headers=headers,
        )
        assert del_resp.status_code == 200, del_resp.text
        assert del_resp.json()["state"] == "aborted"

        aborted_row = await chat_uploads_db.get_upload(cancel_upload_id)
        assert aborted_row is not None and aborted_row["cleaned_at"] is not None

        # Neither multipart parts nor assembled object remain in MinIO.
        assert storage.list_uploaded_parts(cancel_key, cancel_store_uid) is None
        assert storage.object_size(cancel_key) is None

        # 2. Upload a small completed object, backdate expires_at, run sweep, verify deleted.
        completed, exp_key, _ = await _upload_streamed_file_via_http(
            chat_upload_infra.client,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            filename="expired_artifact.bin",
            mime_type="application/octet-stream",
            total_bytes=1024 * 1024,
            seed_tag=b"expired-test",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )
        assert storage.object_size(exp_key) == 1024 * 1024

        past = datetime.now(timezone.utc) - timedelta(hours=2)
        async with chat_upload_infra.db() as conn:
            await conn.execute(
                "UPDATE dalton_chat_file_uploads SET expires_at = %s WHERE upload_id = %s",
                (past, completed["upload_id"]),
            )

        cleaned_count = await sweep_expired_chat_uploads_once()
        assert cleaned_count >= 1
        assert storage.object_size(exp_key) is None

        # Sentinel ASR transcription object is untouched.
        assert asr_store.object_size(sentinel_key) == 16

    @pytest.mark.asyncio
    async def test_rejects_over_limit_empty_forged_cross_user_and_cross_workspace_references(
        self,
        chat_upload_infra,
    ) -> None:
        """Enforces 200 MiB + 1 (413), 0-byte (400), Home workspace scoping, and cross-user/workspace 403s."""
        uid_tag = uuid4().hex[:8]
        owner = await create_user(
            user_id=f"u-owner-{uid_tag}",
            email=f"owner-{uid_tag}@chat-upload-e2e.test",
            name="Owner",
        )
        intruder = await create_user(
            user_id=f"u-intruder-{uid_tag}",
            email=f"intruder-{uid_tag}@chat-upload-e2e.test",
            name="Intruder",
        )
        ws_a = await create_workspace(
            user_id=owner["user_id"],
            name="Workspace A",
            description="A",
            status="running",
        )
        ws_b = await create_workspace(
            user_id=owner["user_id"],
            name="Workspace B",
            description="B",
            status="running",
        )
        owner_headers = {"x-test-user-id": owner["user_id"]}

        # 1. 200 MiB + 1 rejected with HTTP 413
        over_resp = await chat_upload_infra.client.post(
            "/api/v1/dalton/chat-uploads",
            headers=owner_headers,
            json={
                "workspace_id": str(ws_a["workspace_id"]),
                "filename": "over_200m.bin",
                "mime_type": "application/octet-stream",
                "size_bytes": MAX_CHAT_UPLOAD_BYTES + 1,
            },
        )
        assert over_resp.status_code == 413, over_resp.text

        # 2. 0-byte file rejected with HTTP 400
        empty_resp = await chat_upload_infra.client.post(
            "/api/v1/dalton/chat-uploads",
            headers=owner_headers,
            json={
                "workspace_id": str(ws_a["workspace_id"]),
                "filename": "empty.bin",
                "mime_type": "application/octet-stream",
                "size_bytes": 0,
            },
        )
        assert empty_resp.status_code == 400, empty_resp.text

        # 3. Upload scoped to Workspace A + upload scoped to Chief of Staff Home ("home")
        up_ws_a, _, _ = await _upload_streamed_file_via_http(
            chat_upload_infra.client,
            user_id=owner["user_id"],
            workspace_id=str(ws_a["workspace_id"]),
            filename="secret_a.csv",
            mime_type="text/csv",
            total_bytes=64 * 1024,
            seed_tag=b"sec-a",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )
        up_home, _, _ = await _upload_streamed_file_via_http(
            chat_upload_infra.client,
            user_id=owner["user_id"],
            workspace_id="home",
            filename="home_brief.pdf",
            mime_type="application/pdf",
            total_bytes=64 * 1024,
            seed_tag=b"sec-home",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )

        real_home_ws_id = get_home_workspace_id(owner["user_id"])
        assert up_home["workspace_id"] == str(real_home_ws_id)

        # Chief of Staff turn (`workspace_id=None`) accepts the Home upload
        claimed_home = await validate_and_claim_request_attachments(
            [
                {
                    "type": "chat_upload",
                    "upload_id": up_home["upload_id"],
                    "name": "home_brief.pdf",
                    "mime_type": "application/pdf",
                    "size": 64 * 1024,
                }
            ],
            user_id=owner["user_id"],
            workspace_id=None,
        )
        assert len(claimed_home) == 1

        ctx_ws_a = [
            {
                "type": "chat_upload",
                "upload_id": up_ws_a["upload_id"],
                "name": "secret_a.csv",
                "mime_type": "text/csv",
                "size": 64 * 1024,
            }
        ]

        # 4. Cross-user reference rejected with 403
        with pytest.raises(HTTPException) as cross_user_exc:
            await validate_and_claim_request_attachments(
                ctx_ws_a,
                user_id=intruder["user_id"],
                workspace_id=str(ws_a["workspace_id"]),
            )
        assert cross_user_exc.value.status_code == 403

        # 5. Cross-workspace reference (Workspace A upload used in Workspace B) rejected with 403
        with pytest.raises(HTTPException) as cross_ws_exc:
            await validate_and_claim_request_attachments(
                ctx_ws_a,
                user_id=owner["user_id"],
                workspace_id=str(ws_b["workspace_id"]),
            )
        assert cross_ws_exc.value.status_code == 403

        # 6. Forged / unknown UUID rejected with 404
        with pytest.raises(HTTPException) as forged_exc:
            await validate_and_claim_request_attachments(
                [
                    {
                        "type": "chat_upload",
                        "upload_id": str(uuid4()),
                        "name": "forged.bin",
                        "mime_type": "application/octet-stream",
                        "size": 1024,
                    }
                ],
                user_id=owner["user_id"],
                workspace_id=str(ws_a["workspace_id"]),
            )
        assert forged_exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_cold_workspace_recovery_of_200mib_file_and_retention_beyond_7_days(
        self,
        chat_upload_infra,
    ) -> None:
        """Verify 200 MiB source object survives +30d clock advance, cold Docker container
        recreation rematerializes exact bytes without re-upload, and explicit file/workspace
        delete cleans the retained source object with permission checks."""
        uid_tag = uuid4().hex[:8]
        owner = await create_user(
            user_id=f"u-cold-owner-{uid_tag}",
            email=f"cold-owner-{uid_tag}@chat-upload-e2e.test",
            name="Cold Recovery Owner",
        )
        intruder = await create_user(
            user_id=f"u-cold-intruder-{uid_tag}",
            email=f"cold-intruder-{uid_tag}@chat-upload-e2e.test",
            name="Cold Recovery Intruder",
        )
        working_dir = os.environ.get("DOCKER_SANDBOX_WORK_DIR", "/home/workspace")
        comp = await create_computer(
            owner["user_id"],
            kind="docker",
            name=f"cold-comp-{uid_tag}",
            is_primary=True,
            status="stopped",
            root_dir=working_dir,
        )
        computer_id = str(comp["computer_id"])
        ws = await create_workspace_on_computer(
            owner["user_id"],
            "cold_recovery_ws",
            computer_id,
            description="200 MiB cold sandbox recovery",
        )
        assert ws is not None
        ws_to_delete = await create_workspace(
            user_id=owner["user_id"],
            name="Workspace Delete Cleanup",
            description="Workspace deletion test",
            status="running",
        )
        workspace_id = str(ws["workspace_id"])
        del_workspace_id = str(ws_to_delete["workspace_id"])
        dir_name = str(ws["dir_name"])
        project = ProjectContext(workspace_id=workspace_id, dir_name=dir_name)
        layout = WorkspaceLayout(root=working_dir, dir_name=dir_name)

        sandbox_1: PTCSandbox | None = None
        sandbox_2: PTCSandbox | None = None
        try:
            # 1. Start Docker sandbox #1, bind computer/workspace identity via production CAS,
            #    upload exact 200 MiB + an unused upload, and materialize 200 MiB
            claimed_1 = await try_claim_computer_for_start(
                computer_id,
                from_status="stopped",
                expected_provider_ref=None,
                require_provider_ref=True,
            )
            assert claimed_1 is not None
            sandbox_1 = await _start_docker_sandbox(dir_name=dir_name)
            assert sandbox_1.sandbox_id is not None
            sandbox_1_id = sandbox_1.sandbox_id
            bound_1 = await try_bind_computer_provider_ref(
                computer_id,
                provider_ref=sandbox_1_id,
                expected_previous_provider_ref=None,
                platform_secret_version=0,
            )
            assert bound_1 is not None

            up200, key200, sha200 = await _upload_streamed_file_via_http(
                chat_upload_infra.client,
                user_id=owner["user_id"],
                workspace_id=workspace_id,
                filename="cold_durable_200m.zip",
                mime_type="application/zip",
                total_bytes=_SIZE_200_MIB,
                seed_tag=b"cold-200m",
                tracked_keys=chat_upload_infra.tracked_keys,
                tracked_multiparts=chat_upload_infra.tracked_multiparts,
            )
            _, unused_key, _ = await _upload_streamed_file_via_http(
                chat_upload_infra.client,
                user_id=owner["user_id"],
                workspace_id=workspace_id,
                filename="unused_draft.bin",
                mime_type="application/octet-stream",
                total_bytes=512 * 1024,
                seed_tag=b"unused-draft",
                tracked_keys=chat_upload_infra.tracked_keys,
                tracked_multiparts=chat_upload_infra.tracked_multiparts,
            )

            chat_req = ChatRequest(
                messages=[ChatMessage(role="user", content="Store 200 MiB archive")],
                workspace_id=workspace_id,
                additional_context=[
                    {
                        "type": "chat_upload",
                        "upload_id": up200["upload_id"],
                        "name": up200["filename"],
                        "mime_type": up200["mime_type"],
                        "size": _SIZE_200_MIB,
                    }
                ],
            )
            await confirm_request_chat_uploads(
                chat_req.additional_context,
                user_id=owner["user_id"],
                workspace_id=workspace_id,
            )
            await attach_request_files(
                [{"role": "user", "content": "Store 200 MiB archive"}],
                chat_req,
                SimpleNamespace(sandbox=sandbox_1),
                "gpt-5",
                SimpleNamespace(input_modalities=["text", "image", "pdf"]),
                project=project,
                user_id=owner["user_id"],
                workspace_id=workspace_id,
            )

            # Write a small companion file so sync_to_db backs up <=100 MiB while skipping 200 MiB
            note_abs = sandbox_1.normalize_path("work/readme.txt", project=project)
            await sandbox_1.execute_bash_command(
                f"mkdir -p {shlex.quote(layout.workspace + '/work')} "
                f"&& printf 'companion-note' > {shlex.quote(note_abs)}"
            )

            row200 = await chat_uploads_db.get_upload(up200["upload_id"])
            assert row200 is not None
            assert row200["used_at"] is not None
            assert row200["expires_at"] is None
            rel200 = build_sandbox_upload_rel_path(row200)

            # 2. Run real FilePersistenceService.sync_to_db -> 200 MiB is reported too_large, not in DB
            sync_res = await FilePersistenceService.sync_to_db(
                workspace_id, sandbox_1, layout=layout
            )
            assert any(
                u.path == rel200 and u.reason == "too_large"
                for u in sync_res.unsaved
            ), sync_res.unsaved

            # 3. Stop machine cleanly in DB and destroy Docker container #1 completely
            stopping_1 = await update_computer_status(
                computer_id, "stopping", expected="running"
            )
            assert stopping_1 is not None
            await sandbox_1.cleanup()
            sandbox_1 = None
            stopped_1 = await update_computer_status(
                computer_id, "stopped", expected="stopping"
            )
            assert stopped_1 is not None

            # Stopped-workspace file tree still exposes both work/readme.txt and the 200 MiB upload
            offline_tree = await FilePersistenceService.get_file_tree(workspace_id)
            offline_by_path = {entry["path"]: entry for entry in offline_tree}
            assert "work/readme.txt" in offline_by_path
            assert rel200 in offline_by_path
            assert offline_by_path[rel200]["size"] == _SIZE_200_MIB

            # 4. Advance sweeper clock +30 days: unused upload is cleaned, used 200 MiB stays durable
            plus_30d = datetime.now(timezone.utc) + timedelta(days=30)
            swept = await sweep_expired_chat_uploads_once(now=plus_30d)
            assert swept >= 1
            assert storage.object_size(unused_key) is None
            assert storage.object_size(key200) == _SIZE_200_MIB

            # 5. Start brand-new cold Docker container #2, verify unbound CAS rejects, then claim+bind
            #    using real production ComputerManager transitions and run maybe_restore + future-turn check
            sandbox_2 = await _start_docker_sandbox(dir_name=dir_name)
            assert sandbox_2.sandbox_id is not None
            assert sandbox_2.sandbox_id != sandbox_1_id

            # Before binding sandbox_2, workspace still names sandbox_1_id -> real CAS raises RestoreIdentityLost
            with pytest.raises(RestoreIdentityLost):
                await FilePersistenceService.maybe_restore(
                    workspace_id, sandbox_2, layout=layout
                )

            claimed_2 = await try_claim_computer_for_start(
                computer_id,
                from_status="stopped",
                expected_provider_ref=sandbox_1_id,
                require_provider_ref=True,
            )
            assert claimed_2 is not None
            bound_2 = await try_bind_computer_provider_ref(
                computer_id,
                provider_ref=sandbox_2.sandbox_id,
                expected_previous_provider_ref=sandbox_1_id,
                platform_secret_version=0,
            )
            assert bound_2 is not None

            restored = await FilePersistenceService.maybe_restore(
                workspace_id, sandbox_2, layout=layout
            )
            assert restored is True
            assert await files_restore_incomplete(workspace_id) is False

            turn_ensure = await ensure_workspace_chat_uploads_present(
                workspace_id, sandbox_2, project=project
            )
            assert turn_ensure == {"restored": 0, "errors": 0}

            abs200_cold = sandbox_2.normalize_path(rel200, project=project)
            note_abs_cold = sandbox_2.normalize_path("work/readme.txt", project=project)
            verify_cold_cmd = (
                "python3 -c '"
                "import hashlib, json, sys\n"
                "p, note_p = sys.argv[1], sys.argv[2]\n"
                "h = hashlib.sha256()\n"
                "sz = 0\n"
                "with open(p, \"rb\") as f:\n"
                "    while True:\n"
                "        b = f.read(1048576)\n"
                "        if not b:\n"
                "            break\n"
                "        sz += len(b)\n"
                "        h.update(b)\n"
                "note = open(note_p, \"r\", encoding=\"utf-8\").read()\n"
                "print(json.dumps([sz, h.hexdigest(), note]))\n"
                f"' {shlex.quote(abs200_cold)} {shlex.quote(note_abs_cold)}"
            )
            cold_res = await sandbox_2.execute_bash_command(verify_cold_cmd, timeout=120)
            assert cold_res.get("success") is True, cold_res
            cold_sz, cold_sha, cold_note = json.loads(
                (cold_res.get("stdout") or "").strip().splitlines()[-1]
            )
            assert cold_sz == _SIZE_200_MIB
            assert cold_sha == sha200
            assert cold_note == "companion-note"

            # 6. Explicit file delete enforces permissions, removes MinIO object, prevents resurrection
            with pytest.raises(HTTPException) as bad_del:
                await retire_deleted_workspace_chat_uploads(
                    workspace_id=workspace_id,
                    caller_user_id=intruder["user_id"],
                    deleted_client_paths=[rel200],
                )
            assert bad_del.value.status_code == 403
            assert storage.object_size(key200) == _SIZE_200_MIB

            cleaned_del = await retire_deleted_workspace_chat_uploads(
                workspace_id=workspace_id,
                caller_user_id=owner["user_id"],
                deleted_client_paths=[rel200],
            )
            assert cleaned_del == 1
            assert storage.object_size(key200) is None

            await sandbox_2.execute_bash_command(
                f"rm -f {shlex.quote(abs200_cold)} {shlex.quote(layout.workspace + '/.file_sync_marker')}"
            )
            await FilePersistenceService.maybe_restore(
                workspace_id, sandbox_2, layout=layout
            )
            exists_check = await sandbox_2.execute_bash_command(
                f"test ! -e {shlex.quote(abs200_cold)}"
            )
            assert exists_check.get("success") is True

            # 7. Workspace deletion retires and deletes workspace-bound source objects
            up_ws_del, ws_del_key, _ = await _upload_streamed_file_via_http(
                chat_upload_infra.client,
                user_id=owner["user_id"],
                workspace_id=del_workspace_id,
                filename="delete_with_ws.bin",
                mime_type="application/octet-stream",
                total_bytes=256 * 1024,
                seed_tag=b"ws-del",
                tracked_keys=chat_upload_infra.tracked_keys,
                tracked_multiparts=chat_upload_infra.tracked_multiparts,
            )
            await confirm_request_chat_uploads(
                [
                    {
                        "type": "chat_upload",
                        "upload_id": up_ws_del["upload_id"],
                        "name": "delete_with_ws.bin",
                        "mime_type": "application/octet-stream",
                        "size": 256 * 1024,
                    }
                ],
                user_id=owner["user_id"],
                workspace_id=del_workspace_id,
            )
            assert storage.object_size(ws_del_key) == 256 * 1024

            with pytest.raises(HTTPException) as not_deleted_yet:
                await cleanup_deleted_workspace_chat_uploads(
                    del_workspace_id, caller_user_id=owner["user_id"]
                )
            assert not_deleted_yet.value.status_code == 409

            assert await db_delete_workspace(del_workspace_id) is True
            purged = await cleanup_deleted_workspace_chat_uploads(
                del_workspace_id, caller_user_id=owner["user_id"]
            )
            assert purged == 1
            assert storage.object_size(ws_del_key) is None
        finally:
            if sandbox_1 is not None:
                await sandbox_1.cleanup()
            if sandbox_2 is not None:
                await sandbox_2.cleanup()

    @pytest.mark.asyncio
    async def test_completion_lease_ownership_stale_errors_and_late_cancel_cleanup(
        self,
        chat_upload_infra,
        docker_ptc_sandbox: PTCSandbox,
    ) -> None:
        """Verify PostgreSQL claim_token ownership across expired-lease recovery, stale
        reopen/mark_failed errors, and late-object assembly after cancellation."""
        uid_tag = uuid4().hex[:8]
        user = await create_user(
            user_id=f"u-lease-e2e-{uid_tag}",
            email=f"lease-e2e-{uid_tag}@chat-upload-e2e.test",
            name="Completion Lease Tester",
        )
        ws = await create_workspace(
            user_id=user["user_id"],
            name="Lease Recovery Workspace",
            description="Completion lease ownership E2E",
            status="running",
        )
        workspace_id = str(ws["workspace_id"])
        headers = {"x-test-user-id": user["user_id"]}
        project = ProjectContext(workspace_id=workspace_id, dir_name="lease_recovery")
        await docker_ptc_sandbox._ensure_workspace_dirs(project.dir_name)

        real_digest = storage.compute_stored_object_digest
        real_complete_mp = storage.complete_multipart_upload

        # ------------------------------------------------------------------
        # 1. Stale Worker A / Successor Worker B completion + used materialization
        # ------------------------------------------------------------------
        size_1 = CHAT_UPLOAD_PART_SIZE + 65536
        uid_1, key_1, _, sha_1 = await _stage_parts_without_completing(
            chat_upload_infra.client,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            filename="lease_winner.bin",
            mime_type="application/octet-stream",
            total_bytes=size_1,
            seed_tag=b"lease-race-1",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )

        entered_1 = threading.Event()
        release_1 = threading.Event()
        calls_1 = 0

        def _digest_race_1(object_key: str) -> tuple[int, str] | None:
            nonlocal calls_1
            calls_1 += 1
            res = real_digest(object_key)
            if calls_1 == 1:
                entered_1.set()
                if not release_1.wait(15):
                    raise TimeoutError("release_1 timed out")
            return res

        with patch.object(
            storage,
            "compute_stored_object_digest",
            create_autospec(real_digest, side_effect=_digest_race_1),
        ):
            task_1a = asyncio.create_task(
                chat_upload_infra.client.post(
                    f"/api/v1/dalton/chat-uploads/{uid_1}/complete",
                    headers=headers,
                    json={"sha256": sha_1},
                )
            )
            assert await asyncio.to_thread(entered_1.wait, 10)

            async with chat_upload_infra.db() as conn:
                await conn.execute(
                    "UPDATE dalton_chat_file_uploads SET lease_until = NOW() - INTERVAL '5 seconds' WHERE upload_id = %s",
                    (uid_1,),
                )

            try:
                resp_1b = await chat_upload_infra.client.post(
                    f"/api/v1/dalton/chat-uploads/{uid_1}/complete",
                    headers=headers,
                    json={"sha256": sha_1},
                )
                assert resp_1b.status_code == 200, resp_1b.text
                assert resp_1b.json()["state"] == "ready"
                assert resp_1b.json()["sha256"] == sha_1

                claimed_1 = await confirm_request_chat_uploads(
                    [
                        {
                            "type": "chat_upload",
                            "upload_id": uid_1,
                            "name": "lease_winner.bin",
                            "mime_type": "application/octet-stream",
                            "size": size_1,
                        }
                    ],
                    user_id=user["user_id"],
                    workspace_id=workspace_id,
                )
                assert len(claimed_1) == 1 and claimed_1[0]["used_at"] is not None
            finally:
                release_1.set()

            resp_1a = await task_1a
            assert resp_1a.status_code == 200, resp_1a.text
            assert resp_1a.json()["state"] == "ready"
            assert resp_1a.json()["sha256"] == sha_1

        row_1 = await chat_uploads_db.get_upload(uid_1)
        assert row_1 is not None
        assert row_1["state"] == "ready"
        assert row_1["used_at"] is not None
        assert row_1["cleaned_at"] is None
        assert row_1["claim_token"] is None
        assert real_digest(key_1) == (size_1, sha_1)

        # DB CHECK constraint rejects marking a ready row cleaned_at
        async with chat_upload_infra.db() as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE dalton_chat_file_uploads SET cleaned_at = NOW() WHERE upload_id = %s",
                        (uid_1,),
                    )

        # Used upload remains readable and materializes cleanly into the Docker sandbox
        chat_req_1 = ChatRequest(
            messages=[ChatMessage(role="user", content="Verify reclaimed upload")],
            workspace_id=workspace_id,
            additional_context=[
                {
                    "type": "chat_upload",
                    "upload_id": uid_1,
                    "name": "lease_winner.bin",
                    "mime_type": "application/octet-stream",
                    "size": size_1,
                }
            ],
        )
        await attach_request_files(
            [{"role": "user", "content": "Verify reclaimed upload"}],
            chat_req_1,
            SimpleNamespace(sandbox=docker_ptc_sandbox),
            "gpt-5",
            SimpleNamespace(input_modalities=["text", "image", "pdf"]),
            project=project,
            user_id=user["user_id"],
            workspace_id=workspace_id,
        )
        rel_1 = build_sandbox_upload_rel_path(row_1)
        abs_1 = docker_ptc_sandbox.normalize_path(rel_1, project=project)
        verify_1 = await docker_ptc_sandbox.execute_bash_command(
            "python3 -c 'import hashlib, json, sys; p=sys.argv[1]; b=open(p,\"rb\").read(); "
            "print(json.dumps([len(b), hashlib.sha256(b).hexdigest()]))' "
            f"{shlex.quote(abs_1)}",
            timeout=60,
        )
        assert verify_1.get("success") is True, verify_1
        assert json.loads((verify_1.get("stdout") or "").strip().splitlines()[-1]) == [
            size_1,
            sha_1,
        ]

        # ------------------------------------------------------------------
        # 2a. Stale Worker A transient reopen error while Worker B holds completing
        # ------------------------------------------------------------------
        size_2 = 512 * 1024
        uid_2a, key_2a, _, sha_2a = await _stage_parts_without_completing(
            chat_upload_infra.client,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            filename="stale_reopen.bin",
            mime_type="application/octet-stream",
            total_bytes=size_2,
            seed_tag=b"lease-reopen-2a",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )

        entered_2a = threading.Event()
        release_2a = threading.Event()
        entered_2b = threading.Event()
        release_2b = threading.Event()
        calls_2a = 0

        def _digest_reopen_2a(object_key: str) -> tuple[int, str] | None:
            nonlocal calls_2a
            calls_2a += 1
            if calls_2a == 1:
                entered_2a.set()
                if not release_2a.wait(15):
                    raise TimeoutError("release_2a timed out")
                return None
            entered_2b.set()
            if not release_2b.wait(15):
                raise TimeoutError("release_2b timed out")
            return real_digest(object_key)

        with patch.object(
            storage,
            "compute_stored_object_digest",
            create_autospec(real_digest, side_effect=_digest_reopen_2a),
        ):
            task_2a = asyncio.create_task(
                chat_upload_infra.client.post(
                    f"/api/v1/dalton/chat-uploads/{uid_2a}/complete",
                    headers=headers,
                    json={"sha256": sha_2a},
                )
            )
            assert await asyncio.to_thread(entered_2a.wait, 10)

            async with chat_upload_infra.db() as conn:
                await conn.execute(
                    "UPDATE dalton_chat_file_uploads SET lease_until = NOW() - INTERVAL '5 seconds' WHERE upload_id = %s",
                    (uid_2a,),
                )

            task_2b = asyncio.create_task(
                chat_upload_infra.client.post(
                    f"/api/v1/dalton/chat-uploads/{uid_2a}/complete",
                    headers=headers,
                    json={"sha256": sha_2a},
                )
            )
            assert await asyncio.to_thread(entered_2b.wait, 10)
            active_2b_row = await chat_uploads_db.get_upload(uid_2a)
            assert active_2b_row is not None and active_2b_row["state"] == "completing"
            b_claim_token = active_2b_row["claim_token"]
            assert b_claim_token is not None

            try:
                release_2a.set()
                resp_2a = await task_2a
                assert resp_2a.status_code == 409, resp_2a.text
                assert resp_2a.json()["detail"]["code"] == "completing_in_progress"

                after_stale_reopen = await chat_uploads_db.get_upload(uid_2a)
                assert after_stale_reopen is not None
                assert after_stale_reopen["state"] == "completing"
                assert after_stale_reopen["claim_token"] == b_claim_token
            finally:
                release_2b.set()

            resp_2b = await task_2b
            assert resp_2b.status_code == 200, resp_2b.text
            assert resp_2b.json()["state"] == "ready"

        assert real_digest(key_2a) == (size_2, sha_2a)

        # ------------------------------------------------------------------
        # 2b. Stale Worker A checksum mismatch mark_failed after B marked ready + used
        # ------------------------------------------------------------------
        uid_2b, key_2b, _, sha_2b = await _stage_parts_without_completing(
            chat_upload_infra.client,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            filename="stale_fail.bin",
            mime_type="application/octet-stream",
            total_bytes=size_2,
            seed_tag=b"lease-fail-2b",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )

        entered_2b_fail = threading.Event()
        release_2b_fail = threading.Event()
        calls_2b = 0

        def _digest_fail_2b(object_key: str) -> tuple[int, str] | None:
            nonlocal calls_2b
            calls_2b += 1
            if calls_2b == 1:
                entered_2b_fail.set()
                if not release_2b_fail.wait(15):
                    raise TimeoutError("release_2b_fail timed out")
                return (size_2, "0" * 64)
            return real_digest(object_key)

        tid_2b = str(uuid4())
        await create_thread(
            tid_2b,
            workspace_id,
            "completed",
            user_id=user["user_id"],
        )
        ctx_2b = [
            {
                "type": "chat_upload",
                "upload_id": uid_2b,
                "name": "stale_fail.bin",
                "mime_type": "application/octet-stream",
                "size": size_2,
            }
        ]
        scope_2b = RunScope(user_id=user["user_id"], burst_slot_id=None)
        try:
            with patch.object(
                storage,
                "compute_stored_object_digest",
                create_autospec(real_digest, side_effect=_digest_fail_2b),
            ):
                task_2b_stale = asyncio.create_task(
                    chat_upload_infra.client.post(
                        f"/api/v1/dalton/chat-uploads/{uid_2b}/complete",
                        headers=headers,
                        json={"sha256": sha_2b},
                    )
                )
                assert await asyncio.to_thread(entered_2b_fail.wait, 10)

                async with chat_upload_infra.db() as conn:
                    await conn.execute(
                        "UPDATE dalton_chat_file_uploads SET lease_until = NOW() - INTERVAL '5 seconds' WHERE upload_id = %s",
                        (uid_2b,),
                    )

                try:
                    resp_2b_win = await chat_upload_infra.client.post(
                        f"/api/v1/dalton/chat-uploads/{uid_2b}/complete",
                        headers=headers,
                        json={"sha256": sha_2b},
                    )
                    assert resp_2b_win.status_code == 200, resp_2b_win.text
                    rk_2b = _rk(f"2b-accept-{uid_tag}")
                    run_2b = str(uuid4())
                    claim_2b = chat_upload_claim_key(
                        request_key=rk_2b, run_id=run_2b
                    )
                    prov_2b = await validate_and_claim_request_attachments(
                        ctx_2b,
                        user_id=user["user_id"],
                        workspace_id=workspace_id,
                        claim_key=claim_2b,
                    )
                    assert prov_2b[0]["used_at"] is None
                    assert claim_2b in (prov_2b[0]["pending_claims"] or {})

                    await begin_run(
                        ChatRequest(
                            messages=[ChatMessage(role="user", content="Accept 2b")],
                            workspace_id=workspace_id,
                            request_key=rk_2b,
                            additional_context=ctx_2b,
                        ),
                        scope=scope_2b,
                        thread_id=tid_2b,
                        run_id=run_2b,
                        workspace_id=workspace_id,
                        user_id=user["user_id"],
                        is_byok=False,
                        query_content="Accept 2b",
                        query_type="initial",
                        feedback_action=None,
                        query_metadata={},
                        fork=None,
                        is_checkpoint_replay=False,
                    )
                finally:
                    release_2b_fail.set()

                resp_2b_stale = await task_2b_stale
                assert resp_2b_stale.status_code == 200, resp_2b_stale.text
                assert resp_2b_stale.json()["state"] == "ready"
                assert resp_2b_stale.json()["sha256"] == sha_2b
        finally:
            await scope_2b.fail_open("2b-cleanup")

        row_2b = await chat_uploads_db.get_upload(uid_2b)
        assert row_2b is not None
        assert row_2b["state"] == "ready"
        assert row_2b["used_at"] is not None
        assert row_2b["expires_at"] is None
        assert row_2b["cleaned_at"] is None
        assert real_digest(key_2b) == (size_2, sha_2b)

        # ------------------------------------------------------------------
        # 3a. Cancellation during finalization with late object assembly (cooperative)
        # ------------------------------------------------------------------
        uid_3a, key_3a, store_uid_3a, sha_3a = await _stage_parts_without_completing(
            chat_upload_infra.client,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            filename="cancel_late_coop.bin",
            mime_type="application/octet-stream",
            total_bytes=size_2,
            seed_tag=b"cancel-late-3a",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )
        late_bytes = b"late-assembled-minio-object-" * 1024
        assembling_3a = threading.Event()
        finish_3a = threading.Event()

        def _late_complete_3a(
            object_key: str, store_upload_id: str, parts: list[tuple[int, str]]
        ) -> bool:
            assembling_3a.set()
            if not finish_3a.wait(15):
                raise TimeoutError("finish_3a timed out")
            # Simulate S3/MinIO committing CompleteMultipartUpload right after DELETE ran
            asr_store._get_client().put_object(
                Bucket=asr_store._bucket_name(),
                Key=asr_store.resolve_full_key(object_key),
                Body=late_bytes,
            )
            return True

        with patch.object(
            storage,
            "complete_multipart_upload",
            create_autospec(real_complete_mp, side_effect=_late_complete_3a),
        ):
            task_3a = asyncio.create_task(
                chat_upload_infra.client.post(
                    f"/api/v1/dalton/chat-uploads/{uid_3a}/complete",
                    headers=headers,
                    json={"sha256": sha_3a},
                )
            )
            assert await asyncio.to_thread(assembling_3a.wait, 10)

            try:
                del_3a = await chat_upload_infra.client.delete(
                    f"/api/v1/dalton/chat-uploads/{uid_3a}",
                    headers=headers,
                )
                assert del_3a.status_code == 200, del_3a.text
                assert del_3a.json()["state"] == "aborted"
                mid_3a = await chat_uploads_db.get_upload(uid_3a)
                assert mid_3a is not None
                assert mid_3a["state"] == "aborted"
                assert mid_3a["cleaned_at"] is None
            finally:
                finish_3a.set()

            resp_3a = await task_3a
            assert resp_3a.status_code == 409, resp_3a.text

        final_3a = await chat_uploads_db.get_upload(uid_3a)
        assert final_3a is not None
        assert final_3a["state"] == "aborted"
        assert final_3a["cleaned_at"] is not None
        assert storage.list_uploaded_parts(key_3a, store_uid_3a) is None
        assert storage.object_size(key_3a) is None

        # ------------------------------------------------------------------
        # 3b. Cancellation during finalization when completion worker crashes (sweeper fallback)
        # ------------------------------------------------------------------
        uid_3b, key_3b, store_uid_3b, _ = await _stage_parts_without_completing(
            chat_upload_infra.client,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            filename="cancel_late_crash.bin",
            mime_type="application/octet-stream",
            total_bytes=size_2,
            seed_tag=b"cancel-late-3b",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )
        claimed_3b = await chat_uploads_db.claim_complete(
            uid_3b, lease=timedelta(minutes=5)
        )
        assert claimed_3b is not None and claimed_3b["state"] == "completing"

        del_3b = await chat_upload_infra.client.delete(
            f"/api/v1/dalton/chat-uploads/{uid_3b}",
            headers=headers,
        )
        assert del_3b.status_code == 200, del_3b.text
        mid_3b = await chat_uploads_db.get_upload(uid_3b)
        assert mid_3b is not None and mid_3b["cleaned_at"] is None

        # Simulate MinIO writing late assembled object right as worker died before cooperative cleanup
        asr_store._get_client().put_object(
            Bucket=asr_store._bucket_name(),
            Key=asr_store.resolve_full_key(key_3b),
            Body=late_bytes,
        )
        assert storage.object_size(key_3b) == len(late_bytes)

        swept_3b = await sweep_expired_chat_uploads_once()
        assert swept_3b >= 1
        final_3b = await chat_uploads_db.get_upload(uid_3b)
        assert final_3b is not None and final_3b["cleaned_at"] is not None
        assert storage.list_uploaded_parts(key_3b, store_uid_3b) is None
        assert storage.object_size(key_3b) is None

    @pytest.mark.asyncio
    async def test_durable_acceptance_batch_validation_admission_conflicts_concurrent_claims_and_quotas(
        self,
        chat_upload_infra,
    ) -> None:
        """Verify whole-batch validation before mutation (413/404), atomic retention at real
        PostgreSQL run/steer admission, rollback on 409 conflict, concurrent accepted + rejected
        request safety, and per-user active + ready-unused file/byte quotas."""
        uid_tag = uuid4().hex[:8]
        user = await create_user(
            user_id=f"u-admit-{uid_tag}",
            email=f"admit-{uid_tag}@chat-upload-e2e.test",
            name="Admission Durability Tester",
        )
        ws = await create_workspace(
            user_id=user["user_id"],
            name="Admission Durability WS",
            description="Durable acceptance E2E",
            status="running",
        )
        workspace_id = str(ws["workspace_id"])
        headers = {"x-test-user-id": user["user_id"]}

        # Upload 4 x 200 MiB ready unused files (streamed in 8 MiB parts).
        uploads_200: list[tuple[dict, str, str]] = []
        for idx in range(1, 5):
            up_item = await _upload_streamed_file_via_http(
                chat_upload_infra.client,
                user_id=user["user_id"],
                workspace_id=workspace_id,
                filename=f"batch_200m_{idx}.bin",
                mime_type="application/octet-stream",
                total_bytes=_SIZE_200_MIB,
                seed_tag=f"admit-200m-{idx}".encode(),
                tracked_keys=chat_upload_infra.tracked_keys,
                tracked_multiparts=chat_upload_infra.tracked_multiparts,
            )
            uploads_200.append(up_item)

        # 1a. Rejected 4 x 200 MiB (800 MiB > 600 MiB message cap) -> 413 without mutating any row
        ctx_4x200 = [
            {
                "type": "chat_upload",
                "upload_id": up["upload_id"],
                "name": up["filename"],
                "mime_type": up["mime_type"],
                "size": _SIZE_200_MIB,
            }
            for up, _, _ in uploads_200
        ]
        with pytest.raises(HTTPException) as over_batch_exc:
            await validate_and_claim_request_attachments(
                ctx_4x200,
                user_id=user["user_id"],
                workspace_id=workspace_id,
                claim_key="req:rejected-4x200",
            )
        assert over_batch_exc.value.status_code == 413

        for up, _, _ in uploads_200:
            row = await chat_uploads_db.get_upload(up["upload_id"])
            assert row is not None
            assert row["used_at"] is None
            assert row["expires_at"] is not None
            assert (row["pending_claims"] or {}) == {}

        # 1b. Valid first upload + unknown second upload -> 404 without mutating the first upload
        with pytest.raises(HTTPException) as bad_second_exc:
            await validate_and_claim_request_attachments(
                [
                    ctx_4x200[0],
                    {
                        "type": "chat_upload",
                        "upload_id": str(uuid4()),
                        "name": "missing.bin",
                        "mime_type": "application/octet-stream",
                        "size": 1024,
                    },
                ],
                user_id=user["user_id"],
                workspace_id=workspace_id,
                claim_key="req:rejected-missing-id",
            )
        assert bad_second_exc.value.status_code == 404
        row_first = await chat_uploads_db.get_upload(uploads_200[0][0]["upload_id"])
        assert row_first is not None
        assert row_first["used_at"] is None
        assert row_first["expires_at"] is not None
        assert (row_first["pending_claims"] or {}) == {}

        # 1c. Upload 5th 200 MiB file -> 5 x 200 MiB = 1000 MiB ready-unused byte cap reached;
        #     6th initiation (1 MiB) is rejected with HTTP 429.
        up5, key5, _ = await _upload_streamed_file_via_http(
            chat_upload_infra.client,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            filename="batch_200m_5.bin",
            mime_type="application/octet-stream",
            total_bytes=_SIZE_200_MIB,
            seed_tag=b"admit-200m-5",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )
        over_byte_quota = await chat_upload_infra.client.post(
            "/api/v1/dalton/chat-uploads",
            headers=headers,
            json={
                "workspace_id": workspace_id,
                "filename": "over_1000m_cap.bin",
                "mime_type": "application/octet-stream",
                "size_bytes": _MIB,
            },
        )
        assert over_byte_quota.status_code == 429, over_byte_quota.text

        # Cancel uploads #4 and #5 via DELETE -> proves rejected 413/404 left rows cancellable
        # and frees 400 MiB of byte quota.
        for cancel_uid, cancel_key in (
            (uploads_200[3][0]["upload_id"], uploads_200[3][1]),
            (up5["upload_id"], key5),
        ):
            del_resp = await chat_upload_infra.client.delete(
                f"/api/v1/dalton/chat-uploads/{cancel_uid}",
                headers=headers,
            )
            assert del_resp.status_code == 200, del_resp.text
            assert del_resp.json()["state"] == "aborted"
            assert storage.object_size(cancel_key) is None

        # 2. Real PostgreSQL START admission (`begin_run`), 409 conflict release, and concurrent steer
        up_turn1, key_turn1, _ = uploads_200[0]
        up_conflict, key_conflict, _ = uploads_200[1]
        up_shared, key_shared, _ = uploads_200[2]

        thread_id = str(uuid4())
        await create_thread(
            thread_id,
            workspace_id,
            "completed",
            user_id=user["user_id"],
        )

        ctx_turn1 = [ctx_4x200[0]]
        rk_turn1 = _rk(f"turn1-{uid_tag}")
        run_a_id = str(uuid4())
        claim_turn1 = chat_upload_claim_key(
            request_key=rk_turn1, run_id=run_a_id
        )
        prov_turn1 = await validate_and_claim_request_attachments(
            ctx_turn1,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            claim_key=claim_turn1,
        )
        assert prov_turn1[0]["used_at"] is None
        assert claim_turn1 in (prov_turn1[0]["pending_claims"] or {})

        req_turn1 = ChatRequest(
            messages=[ChatMessage(role="user", content="Start turn 1")],
            workspace_id=workspace_id,
            request_key=rk_turn1,
            additional_context=ctx_turn1,
        )
        scope_a = RunScope(user_id=user["user_id"], burst_slot_id=None)
        try:
            await begin_run(
                req_turn1,
                scope=scope_a,
                thread_id=thread_id,
                run_id=run_a_id,
                workspace_id=workspace_id,
                user_id=user["user_id"],
                is_byok=False,
                query_content="Start turn 1",
                query_type="initial",
                feedback_action=None,
                query_metadata={},
                fork=None,
                is_checkpoint_replay=False,
            )
            run_a_row = await tl_db.get_run(run_a_id)
            assert run_a_row is not None and run_a_row["status"] == "in_progress"

            row_turn1_after = await chat_uploads_db.get_upload(up_turn1["upload_id"])
            assert row_turn1_after is not None
            assert row_turn1_after["used_at"] is not None
            assert row_turn1_after["expires_at"] is None
            assert (row_turn1_after["pending_claims"] or {}) == {}
            assert storage.object_size(key_turn1) == _SIZE_200_MIB

            # Conflicting non-steerable send (`can_steer=False`) hits 409 and releases provisional claim
            ctx_conflict = [ctx_4x200[1]]
            rk_conflict = _rk(f"conflict-{uid_tag}")
            run_conflict = str(uuid4())
            claim_conflict = chat_upload_claim_key(
                request_key=rk_conflict, run_id=run_conflict
            )
            prov_conflict = await validate_and_claim_request_attachments(
                ctx_conflict,
                user_id=user["user_id"],
                workspace_id=workspace_id,
                claim_key=claim_conflict,
            )
            assert prov_conflict[0]["used_at"] is None
            assert claim_conflict in (prov_conflict[0]["pending_claims"] or {})

            with pytest.raises(HTTPException) as conflict_exc:
                async with ChatUploadSendScope(
                    additional_context=ctx_conflict,
                    user_id=user["user_id"],
                    workspace_id=workspace_id,
                    request_key=rk_conflict,
                    run_id=run_conflict,
                    preclaimed=True,
                ) as conflict_scope:
                    await wait_or_steer(
                        LocalRunExecutor.get_instance(),
                        thread_id,
                        "Non-steerable follow-up",
                        user["user_id"],
                        can_steer=False,
                        request_key=rk_conflict,
                        workspace_id=workspace_id,
                        additional_context=ctx_conflict,
                        upload_claim_key=conflict_scope.claim_key,
                    )
            assert conflict_exc.value.status_code == 409
            assert isinstance(conflict_exc.value.detail, dict)
            assert conflict_exc.value.detail.get("code") == "running"

            row_conflict_after = await chat_uploads_db.get_upload(up_conflict["upload_id"])
            assert row_conflict_after is not None
            assert row_conflict_after["used_at"] is None
            assert row_conflict_after["expires_at"] is not None
            assert (row_conflict_after["pending_claims"] or {}) == {}

            del_conflict = await chat_upload_infra.client.delete(
                f"/api/v1/dalton/chat-uploads/{up_conflict['upload_id']}",
                headers=headers,
            )
            assert del_conflict.status_code == 200, del_conflict.text
            assert storage.object_size(key_conflict) is None

            # Concurrent accepted steer (`rk_win`) + rejected request (`rk_lose`) on `up_shared`
            ctx_shared = [ctx_4x200[2]]
            rk_win = _rk(f"win-{uid_tag}")
            rk_lose = _rk(f"lose-{uid_tag}")
            claim_win = chat_upload_claim_key(request_key=rk_win, run_id=str(uuid4()))
            claim_lose = chat_upload_claim_key(request_key=rk_lose, run_id=str(uuid4()))
            await validate_and_claim_request_attachments(
                ctx_shared,
                user_id=user["user_id"],
                workspace_id=workspace_id,
                claim_key=claim_win,
            )
            await validate_and_claim_request_attachments(
                ctx_shared,
                user_id=user["user_id"],
                workspace_id=workspace_id,
                claim_key=claim_lose,
            )
            row_both = await chat_uploads_db.get_upload(up_shared["upload_id"])
            assert row_both is not None and row_both["used_at"] is None
            assert set((row_both["pending_claims"] or {}).keys()) == {
                claim_win,
                claim_lose,
            }

            ready_flag, steer_sse = await wait_or_steer(
                LocalRunExecutor.get_instance(),
                thread_id,
                "Steer with shared 200 MiB",
                user["user_id"],
                can_steer=True,
                request_key=rk_win,
                workspace_id=workspace_id,
                additional_context=ctx_shared,
                upload_claim_key=claim_win,
            )
            assert ready_flag is False
            assert isinstance(steer_sse, str)
            assert steer_sse.startswith("event: steering_accepted\ndata: ")
            accepted_json = json.loads(
                steer_sse.split("data: ", 1)[1].strip()
            )
            assert len(accepted_json.get("attachments") or []) == 1
            assert accepted_json["attachments"][0]["name"] == up_shared["filename"]

            # Rejecting `rk_lose` AND releasing duplicate `rk_win` must NOT undo durable acceptance
            await release_request_chat_uploads(
                user_id=user["user_id"],
                claim_key=claim_lose,
                request_key=rk_lose,
                additional_context=ctx_shared,
            )
            await release_request_chat_uploads(
                user_id=user["user_id"],
                claim_key=claim_win,
                request_key=rk_win,
                additional_context=ctx_shared,
            )
            row_shared_final = await chat_uploads_db.get_upload(up_shared["upload_id"])
            assert row_shared_final is not None
            assert row_shared_final["used_at"] is not None
            assert row_shared_final["expires_at"] is None
            assert storage.object_size(key_shared) == _SIZE_200_MIB
        finally:
            await scope_a.fail_open("test-cleanup")

        # 3. Per-user 10-file count quota across ready-unused rows + release via cancel/sweep/confirm
        user_q = await create_user(
            user_id=f"u-quota-{uid_tag}",
            email=f"quota-{uid_tag}@chat-upload-e2e.test",
            name="Quota Tester",
        )
        ws_q = await create_workspace(
            user_id=user_q["user_id"],
            name="Quota WS",
            description="Ready unused count quota",
            status="running",
        )
        ws_q_id = str(ws_q["workspace_id"])
        q_headers = {"x-test-user-id": user_q["user_id"]}

        ready_q_ids: list[str] = []
        for idx in range(MAX_ACTIVE_USER_UPLOADS):
            up_q, _, _ = await _upload_streamed_file_via_http(
                chat_upload_infra.client,
                user_id=user_q["user_id"],
                workspace_id=ws_q_id,
                filename=f"ready_slot_{idx}.bin",
                mime_type="application/octet-stream",
                total_bytes=64 * 1024,
                seed_tag=f"slot-{idx}".encode(),
                tracked_keys=chat_upload_infra.tracked_keys,
                tracked_multiparts=chat_upload_infra.tracked_multiparts,
            )
            ready_q_ids.append(str(up_q["upload_id"]))

        resp_11th = await chat_upload_infra.client.post(
            "/api/v1/dalton/chat-uploads",
            headers=q_headers,
            json={
                "workspace_id": ws_q_id,
                "filename": "blocked_11th.bin",
                "mime_type": "application/octet-stream",
                "size_bytes": 64 * 1024,
            },
        )
        assert resp_11th.status_code == 429, resp_11th.text

        # Cancel slot 0 -> frees 1 slot
        del_q0 = await chat_upload_infra.client.delete(
            f"/api/v1/dalton/chat-uploads/{ready_q_ids[0]}",
            headers=q_headers,
        )
        assert del_q0.status_code == 200, del_q0.text
        up_q10, _, _ = await _upload_streamed_file_via_http(
            chat_upload_infra.client,
            user_id=user_q["user_id"],
            workspace_id=ws_q_id,
            filename="ready_slot_10.bin",
            mime_type="application/octet-stream",
            total_bytes=64 * 1024,
            seed_tag=b"slot-10",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )
        ready_q_ids.append(str(up_q10["upload_id"]))

        # Expire slot 1 + run sweeper -> frees 1 slot
        async with chat_upload_infra.db() as conn:
            await conn.execute(
                """
                UPDATE dalton_chat_file_uploads
                SET expires_at = NOW() - INTERVAL '10 minutes'
                WHERE upload_id = %s
                """,
                (ready_q_ids[1],),
            )
        assert await sweep_expired_chat_uploads_once() >= 1
        up_q11, _, _ = await _upload_streamed_file_via_http(
            chat_upload_infra.client,
            user_id=user_q["user_id"],
            workspace_id=ws_q_id,
            filename="ready_slot_11.bin",
            mime_type="application/octet-stream",
            total_bytes=64 * 1024,
            seed_tag=b"slot-11",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )
        ready_q_ids.append(str(up_q11["upload_id"]))

        # Confirm durable acceptance on slot 2 -> leaves temporary upload pool and frees 1 slot
        await confirm_request_chat_uploads(
            [{"type": "chat_upload", "upload_id": ready_q_ids[2]}],
            user_id=user_q["user_id"],
            workspace_id=ws_q_id,
        )
        resp_after_confirm = await chat_upload_infra.client.post(
            "/api/v1/dalton/chat-uploads",
            headers=q_headers,
            json={
                "workspace_id": ws_q_id,
                "filename": "allowed_after_confirm.bin",
                "mime_type": "application/octet-stream",
                "size_bytes": 64 * 1024,
            },
        )
        assert resp_after_confirm.status_code == 200, resp_after_confirm.text
        extra_uid = resp_after_confirm.json()["upload_id"]
        extra_row = await chat_uploads_db.get_upload(extra_uid)
        assert extra_row is not None
        chat_upload_infra.tracked_keys.add(str(extra_row["object_key"]))
        chat_upload_infra.tracked_multiparts.append(
            (str(extra_row["object_key"]), str(extra_row["store_upload_id"]))
        )
        await chat_upload_infra.client.delete(
            f"/api/v1/dalton/chat-uploads/{extra_uid}",
            headers=q_headers,
        )

    @pytest.mark.asyncio
    async def test_deferred_steers_ahead_project_isolation_transient_retry_and_history_separation(
        self,
        chat_upload_infra,
        docker_ptc_sandbox: PTCSandbox,
    ) -> None:
        """Verify ahead-of-turn steers with current_project() == None materialize strictly inside
        each workspace's own folder (two sibling workspaces + Home, zero computer-root copy),
        transient storage failures stay retriable and deliver once on recovery, revoked/retired
        uploads close permanently as dropped, and visible user content omits <system-reminder>
        while supporting file-only steers."""
        assert current_project() is None
        uid_tag = uuid4().hex[:8]
        user = await create_user(
            user_id=f"u-steer-{uid_tag}",
            email=f"steer-{uid_tag}@chat-upload-e2e.test",
            name="Steer Isolation Tester",
        )
        working_dir = os.environ.get("DOCKER_SANDBOX_WORK_DIR", "/home/workspace")
        comp = await create_computer(
            user["user_id"],
            kind="docker",
            name=f"steer-comp-{uid_tag}",
            is_primary=True,
            status="stopped",
            root_dir=working_dir,
        )
        computer_id = str(comp["computer_id"])
        assert docker_ptc_sandbox.sandbox_id is not None
        claimed_c = await try_claim_computer_for_start(
            computer_id,
            from_status="stopped",
            expected_provider_ref=None,
            require_provider_ref=True,
        )
        assert claimed_c is not None
        bound_c = await try_bind_computer_provider_ref(
            computer_id,
            provider_ref=docker_ptc_sandbox.sandbox_id,
            expected_previous_provider_ref=None,
            platform_secret_version=0,
        )
        assert bound_c is not None

        ws_alpha = await create_workspace_on_computer(
            user["user_id"],
            "alpha_desk",
            computer_id,
            description="Alpha workspace on shared computer",
        )
        ws_beta = await create_workspace_on_computer(
            user["user_id"],
            "beta_desk",
            computer_id,
            description="Beta workspace on shared computer",
        )
        assert ws_alpha is not None and ws_beta is not None
        alpha_ws_id = str(ws_alpha["workspace_id"])
        beta_ws_id = str(ws_beta["workspace_id"])
        alpha_dir = str(ws_alpha["dir_name"])
        beta_dir = str(ws_beta["dir_name"])

        home_row = await get_or_create_home_workspace(user["user_id"])
        home_ws_id = str(home_row["workspace_id"])
        bound_home = await bind_workspace_to_computer(
            home_ws_id,
            computer_id,
            expected_computer_id=None,
            dir_name="Home",
            primary_only=True,
        )
        assert bound_home is not None

        await docker_ptc_sandbox._ensure_workspace_dirs(alpha_dir)
        await docker_ptc_sandbox._ensure_workspace_dirs(beta_dir)
        await docker_ptc_sandbox._ensure_workspace_dirs("Home")
        await docker_ptc_sandbox.execute_bash_command(
            f"rm -rf {shlex.quote(working_dir + '/work/uploads')}"
        )

        # Upload one ahead-of-turn attachment into ws_alpha, ws_beta, and Home
        up_alpha, key_alpha, sha_alpha = await _upload_streamed_file_via_http(
            chat_upload_infra.client,
            user_id=user["user_id"],
            workspace_id=alpha_ws_id,
            filename="alpha_model.parquet",
            mime_type="application/octet-stream",
            total_bytes=256 * 1024,
            seed_tag=b"ahead-alpha",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )
        up_beta_ahead, _, sha_beta_ahead = await _upload_streamed_file_via_http(
            chat_upload_infra.client,
            user_id=user["user_id"],
            workspace_id=beta_ws_id,
            filename="beta_ledger.csv",
            mime_type="text/csv",
            total_bytes=256 * 1024,
            seed_tag=b"ahead-beta",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )
        up_home, _, sha_home = await _upload_streamed_file_via_http(
            chat_upload_infra.client,
            user_id=user["user_id"],
            workspace_id="home",
            filename="cos_brief.pdf",
            mime_type="application/pdf",
            total_bytes=256 * 1024,
            seed_tag=b"ahead-home",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )

        session_ns = SimpleNamespace(sandbox=docker_ptc_sandbox)
        wm_stub = create_autospec(WorkspaceManager, instance=True)
        wm_stub.get_session_for_workspace.return_value = session_ns

        async def _run_ahead_for_workspace(
            target_ws_id: str,
            upload_body: dict,
            *,
            explicit_project: ProjectContext | None,
            keep_second_run_open: bool = False,
        ) -> tuple[str, str, RunScope, list[dict]]:
            tid = str(uuid4())
            await create_thread(
                tid,
                target_ws_id,
                "completed",
                user_id=user["user_id"],
            )

            # Admit a parked steer into run #1, then settle run #1 so run #2 opens with it ahead
            r1_id = str(uuid4())
            s1 = RunScope(user_id=user["user_id"], burst_slot_id=None)
            rk_r1 = _rk(f"r1-{tid}")
            rk_ahead = _rk(f"ahead-{tid}")
            rk_r2 = _rk(f"r2-{tid}")
            claim_ahead = chat_upload_claim_key(
                request_key=rk_ahead, run_id=str(uuid4())
            )
            await begin_run(
                ChatRequest(
                    messages=[ChatMessage(role="user", content="Turn 1")],
                    workspace_id=target_ws_id,
                    request_key=rk_r1,
                ),
                scope=s1,
                thread_id=tid,
                run_id=r1_id,
                workspace_id=target_ws_id,
                user_id=user["user_id"],
                is_byok=False,
                query_content="Turn 1",
                query_type="initial",
                feedback_action=None,
                query_metadata={},
                fork=None,
                is_checkpoint_replay=False,
            )
            chat_ctx = [
                {
                    "type": "chat_upload",
                    "upload_id": upload_body["upload_id"],
                    "name": upload_body["filename"],
                    "mime_type": upload_body["mime_type"],
                    "size": upload_body["size_bytes"],
                }
            ]
            await validate_and_claim_request_attachments(
                chat_ctx,
                user_id=user["user_id"],
                workspace_id=target_ws_id,
                claim_key=claim_ahead,
            )
            ready_s, _ = await wait_or_steer(
                LocalRunExecutor.get_instance(),
                tid,
                f"Parked steer for {upload_body['filename']}",
                user["user_id"],
                can_steer=True,
                request_key=rk_ahead,
                workspace_id=target_ws_id,
                additional_context=chat_ctx,
                upload_claim_key=claim_ahead,
            )
            assert ready_s is False
            await s1.fail_open("turn-1-ended", status="completed")

            async with chat_upload_infra.db() as conn:
                await conn.execute(
                    """
                    UPDATE hook_outbox
                    SET created_at = NOW() - INTERVAL '10 seconds'
                    WHERE conversation_thread_id = %s::uuid AND hook_type = 'steer'
                    """,
                    (tid,),
                )

            r2_id = str(uuid4())
            s2 = RunScope(user_id=user["user_id"], burst_slot_id=None)
            req_r2 = ChatRequest(
                messages=[ChatMessage(role="user", content="Fresh turn 2")],
                workspace_id=target_ws_id,
                request_key=rk_r2,
            )
            await begin_run(
                req_r2,
                scope=s2,
                thread_id=tid,
                run_id=r2_id,
                workspace_id=target_ws_id,
                user_id=user["user_id"],
                is_byok=False,
                query_content="Fresh turn 2",
                query_type="follow_up",
                feedback_action=None,
                query_metadata={},
                fork=None,
                is_checkpoint_replay=False,
            )
            input_state = {
                "messages": [
                    {"role": "user", "id": f"msg-{tid[:8]}", "content": "Fresh turn 2"}
                ]
            }
            try:
                assert current_project() is None
                input_id = await open_with_steers_ahead(
                    tid,
                    r2_id,
                    req_r2,
                    input_state,
                    project=explicit_project,
                    session=session_ns,
                )
                assert input_id == f"msg-{tid[:8]}"
                assert len(input_state["messages"]) == 2
                ahead_msg = input_state["messages"][0]
                ahead_kwargs = getattr(ahead_msg, "additional_kwargs", {}) or {}
                held_ids = list(
                    (ahead_kwargs.get("held_jobs") or {}).get("job_ids") or []
                )
                assert len(held_ids) == 1
                ahead_delivered = (
                    ahead_kwargs.get("steering_delivered") or {}
                ).get("messages") or []
                assert len(ahead_delivered) == 1
                assert "<system-reminder>" not in ahead_delivered[0]["content"]
                assert len(ahead_delivered[0].get("attachments") or []) == 1
                await SteeredReportInbox().confirm(r2_id, held_ids)
            finally:
                if not keep_second_run_open:
                    await s2.fail_open("turn-2-cleanup")
            return tid, r2_id, s2, input_state["messages"]

        with patch.object(
            WorkspaceManager,
            "get_instance",
            create_autospec(WorkspaceManager.get_instance, return_value=wm_stub),
        ):
            # Alpha uses explicit ProjectContext; Beta and Home pass project=None to test DB derivation!
            await _run_ahead_for_workspace(
                alpha_ws_id,
                up_alpha,
                explicit_project=ProjectContext(
                    workspace_id=alpha_ws_id, dir_name=alpha_dir
                ),
            )
            await _run_ahead_for_workspace(
                home_ws_id,
                up_home,
                explicit_project=None,
            )
            beta_tid, beta_r2_id, beta_scope, _ = await _run_ahead_for_workspace(
                beta_ws_id,
                up_beta_ahead,
                explicit_project=None,
                keep_second_run_open=True,
            )
            try:
                row_alpha = await chat_uploads_db.get_upload(up_alpha["upload_id"])
                row_beta_ahead = await chat_uploads_db.get_upload(up_beta_ahead["upload_id"])
                row_home = await chat_uploads_db.get_upload(up_home["upload_id"])
                assert row_alpha is not None and row_beta_ahead is not None and row_home is not None

                rel_alpha = build_sandbox_upload_rel_path(row_alpha)
                rel_beta_ahead = build_sandbox_upload_rel_path(row_beta_ahead)
                rel_home = build_sandbox_upload_rel_path(row_home)

                abs_alpha = f"{working_dir}/{alpha_dir}/{rel_alpha}"
                abs_beta_ahead = f"{working_dir}/{beta_dir}/{rel_beta_ahead}"
                abs_home = f"{working_dir}/Home/{rel_home}"
                root_uploads = f"{working_dir}/work/uploads"

                check_paths_cmd = (
                    "python3 -c '"
                    "import hashlib, json, os, sys\n"
                    "paths = sys.argv[1:4]\n"
                    "root_u = sys.argv[4]\n"
                    "out = {\"root_exists\": os.path.exists(root_u), \"shas\": []}\n"
                    "for p in paths:\n"
                    "    h = hashlib.sha256(open(p, \"rb\").read()).hexdigest()\n"
                    "    out[\"shas\"].append(h)\n"
                    "print(json.dumps(out))\n"
                    f"' {shlex.quote(abs_alpha)} {shlex.quote(abs_beta_ahead)} "
                    f"{shlex.quote(abs_home)} {shlex.quote(root_uploads)}"
                )
                probe_res = await docker_ptc_sandbox.execute_bash_command(check_paths_cmd)
                assert probe_res.get("success") is True, probe_res
                parsed_probe = json.loads(str(probe_res.get("stdout") or "").strip())
                assert parsed_probe["root_exists"] is False
                assert parsed_probe["shas"] == [sha_alpha, sha_beta_ahead, sha_home]

                # Explicit delete in ws_alpha removes its single file and cleans MinIO object
                await docker_ptc_sandbox.execute_bash_command(
                    f"rm -f {shlex.quote(abs_alpha)}"
                )
                retired_alpha = await retire_deleted_workspace_chat_uploads(
                    workspace_id=alpha_ws_id,
                    caller_user_id=user["user_id"],
                    deleted_client_paths=[rel_alpha],
                )
                assert retired_alpha == 1
                assert storage.object_size(key_alpha) is None

                # Live steers on ws_beta: (1) text + file, (2) file-only (""), (3) permanently retired
                up_s_text, _, _ = await _upload_streamed_file_via_http(
                    chat_upload_infra.client,
                    user_id=user["user_id"],
                    workspace_id=beta_ws_id,
                    filename="q4_margins.xlsx",
                    mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    total_bytes=128 * 1024,
                    seed_tag=b"steer-text",
                    tracked_keys=chat_upload_infra.tracked_keys,
                    tracked_multiparts=chat_upload_infra.tracked_multiparts,
                )
                up_s_file_only, _, _ = await _upload_streamed_file_via_http(
                    chat_upload_infra.client,
                    user_id=user["user_id"],
                    workspace_id=beta_ws_id,
                    filename="raw_audio_notes.wav",
                    mime_type="audio/wav",
                    total_bytes=128 * 1024,
                    seed_tag=b"steer-file-only",
                    tracked_keys=chat_upload_infra.tracked_keys,
                    tracked_multiparts=chat_upload_infra.tracked_multiparts,
                )

                rk_beta_text = _rk(f"beta-text-{uid_tag}")
                rk_beta_empty = _rk(f"beta-empty-{uid_tag}")
                for rk, msg_text, up_item in (
                    (rk_beta_text, "Compare Q4 margins", up_s_text),
                    (rk_beta_empty, "", up_s_file_only),
                ):
                    ctx_s = [
                        {
                            "type": "chat_upload",
                            "upload_id": up_item["upload_id"],
                            "name": up_item["filename"],
                            "mime_type": up_item["mime_type"],
                            "size": 128 * 1024,
                        }
                    ]
                    claim_s = chat_upload_claim_key(
                        request_key=rk, run_id=str(uuid4())
                    )
                    await validate_and_claim_request_attachments(
                        ctx_s,
                        user_id=user["user_id"],
                        workspace_id=beta_ws_id,
                        claim_key=claim_s,
                    )
                    r_flag, _ = await wait_or_steer(
                        LocalRunExecutor.get_instance(),
                        beta_tid,
                        msg_text,
                        user["user_id"],
                        can_steer=True,
                        request_key=rk,
                        workspace_id=beta_ws_id,
                        additional_context=ctx_s,
                        upload_claim_key=claim_s,
                    )
                    assert r_flag is False

                # Transient MinIO outage: neither steer is checkpointed or closed; hold is released!
                with (
                    patch.object(
                        storage,
                        "sign_download_url",
                        create_autospec(storage.sign_download_url, return_value=None),
                    ),
                    patch.object(
                        storage,
                        "iter_stored_object_chunks",
                        create_autospec(
                            storage.iter_stored_object_chunks,
                            side_effect=RuntimeError("MinIO temporarily unreachable"),
                        ),
                    ),
                ):
                    taken_during_outage = await SteeredReportInbox().take(
                        beta_tid, beta_r2_id
                    )
                assert taken_during_outage == []

                async with chat_upload_infra.db() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            SELECT status, payload
                            FROM hook_outbox
                            WHERE conversation_thread_id = %s::uuid
                              AND idempotency_key = ANY(%s)
                            ORDER BY created_at ASC
                            """,
                            (
                                beta_tid,
                                [f"steer:{rk_beta_text}", f"steer:{rk_beta_empty}"],
                            ),
                        )
                        outage_rows = await cur.fetchall()
                assert len(outage_rows) == 2
                for st_val, pl_val in outage_rows:
                    assert st_val == "pending"
                    assert pl_val.get("outcome") is None
                    assert pl_val.get("steered_run_id") is None

                # Storage recovers -> delivered once with clean shown user content + attachments
                taken_recovered = await SteeredReportInbox().take(beta_tid, beta_r2_id)
                assert len(taken_recovered) == 2
                human_msg, delivered_payload = _steering_message(
                    taken_recovered, beta_r2_id, 1700000000.0
                )
                await SteeredReportInbox().confirm(
                    beta_r2_id, [s.job_id for s in taken_recovered]
                )

                assert "<system-reminder>" in str(human_msg.content)
                assert "q4_margins.xlsx" in str(human_msg.content)
                assert "raw_audio_notes.wav" in str(human_msg.content)

                msgs_shown = delivered_payload["messages"]
                assert len(msgs_shown) == 2
                assert msgs_shown[0]["content"] == "Compare Q4 margins"
                assert "<system-reminder>" not in msgs_shown[0]["content"]
                assert len(msgs_shown[0].get("attachments") or []) == 1
                assert msgs_shown[0]["attachments"][0]["name"] == "q4_margins.xlsx"

                assert msgs_shown[1]["content"] == ""
                assert len(msgs_shown[1].get("attachments") or []) == 1
                assert msgs_shown[1]["attachments"][0]["name"] == "raw_audio_notes.wav"

                # Subsequent poll delivers nothing (delivered exactly once)
                assert await SteeredReportInbox().take(beta_tid, beta_r2_id) == []

                # Permanently retired upload on a steer closes the outbox job as `failed`
                up_s_retired, _, _ = await _upload_streamed_file_via_http(
                    chat_upload_infra.client,
                    user_id=user["user_id"],
                    workspace_id=beta_ws_id,
                    filename="retired_before_take.bin",
                    mime_type="application/octet-stream",
                    total_bytes=64 * 1024,
                    seed_tag=b"steer-retired",
                    tracked_keys=chat_upload_infra.tracked_keys,
                    tracked_multiparts=chat_upload_infra.tracked_multiparts,
                )
                ctx_ret = [
                    {
                        "type": "chat_upload",
                        "upload_id": up_s_retired["upload_id"],
                        "name": up_s_retired["filename"],
                        "mime_type": up_s_retired["mime_type"],
                        "size": 64 * 1024,
                    }
                ]
                rk_beta_retired = _rk(f"beta-retired-{uid_tag}")
                claim_ret = chat_upload_claim_key(
                    request_key=rk_beta_retired, run_id=str(uuid4())
                )
                await validate_and_claim_request_attachments(
                    ctx_ret,
                    user_id=user["user_id"],
                    workspace_id=beta_ws_id,
                    claim_key=claim_ret,
                )
                await wait_or_steer(
                    LocalRunExecutor.get_instance(),
                    beta_tid,
                    "Steer whose upload is deleted before model step",
                    user["user_id"],
                    can_steer=True,
                    request_key=rk_beta_retired,
                    workspace_id=beta_ws_id,
                    additional_context=ctx_ret,
                    upload_claim_key=claim_ret,
                )
                row_ret = await chat_uploads_db.get_upload(up_s_retired["upload_id"])
                assert row_ret is not None
                await retire_deleted_workspace_chat_uploads(
                    workspace_id=beta_ws_id,
                    caller_user_id=user["user_id"],
                    deleted_client_paths=[build_sandbox_upload_rel_path(row_ret)],
                )

                assert await SteeredReportInbox().take(beta_tid, beta_r2_id) == []
                failed_notices = await SteeredReportInbox().take_failed(
                    beta_tid, beta_r2_id
                )
                assert len(failed_notices) == 1
                assert failed_notices[0]["request_key"] == rk_beta_retired
                assert failed_notices[0]["reason"] == "attachment_failed"
                assert await SteeredReportInbox().take_failed(beta_tid, beta_r2_id) == []

                dropped_job = await outbox_db.get_steer_job(f"steer:{rk_beta_retired}")
                assert dropped_job is not None
                assert dropped_job["status"] == "done"
                assert dropped_job["payload"].get("outcome") == "failed"
                assert dropped_job["payload"].get("delivery_error")
            finally:
                await beta_scope.fail_open("beta-r2-cleanup")

    @pytest.mark.asyncio
    async def test_stalled_http_part_upload_deadlines_release_slots_and_recheck_expiry(
        self,
        chat_upload_infra,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify stalled HTTP part bodies time out with 408, release semaphore slots so
        subsequent uploads succeed, and recheck durable row expiry (410) before MinIO PUT."""
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_PART_IDLE_TIMEOUT_S", "0.15")
        monkeypatch.setenv("DALTON_CHAT_UPLOAD_PART_TOTAL_TIMEOUT_S", "0.45")
        get_dalton_settings.cache_clear()

        uid_tag = uuid4().hex[:8]
        user = await create_user(
            user_id=f"u-stall-{uid_tag}",
            email=f"stall-{uid_tag}@chat-upload-e2e.test",
            name="Stalled Part Tester",
        )
        ws = await create_workspace(
            user_id=user["user_id"],
            name="Stalled Part WS",
            description="Part timeout E2E",
            status="running",
        )
        workspace_id = str(ws["workspace_id"])
        headers = {"x-test-user-id": user["user_id"]}

        payload_bytes = b"valid-part-after-stall-" * 4096
        part_sha = hashlib.sha256(payload_bytes).hexdigest()

        init_resp = await chat_upload_infra.client.post(
            "/api/v1/dalton/chat-uploads",
            headers=headers,
            json={
                "workspace_id": workspace_id,
                "filename": "stalled_then_resumed.bin",
                "mime_type": "application/octet-stream",
                "size_bytes": len(payload_bytes),
            },
        )
        assert init_resp.status_code == 200, init_resp.text
        upload_id = str(init_resp.json()["upload_id"])
        row = await chat_uploads_db.get_upload(upload_id)
        assert row is not None
        chat_upload_infra.tracked_keys.add(str(row["object_key"]))
        chat_upload_infra.tracked_multiparts.append(
            (str(row["object_key"]), str(row["store_upload_id"]))
        )

        async def _stalled_http_stream():
            yield b"x" * 1024
            await asyncio.sleep(5.0)
            yield b"y" * 1024

        # Launch 6 concurrent stalled HTTP PUTs (> 4 relay slots) -> all must fail promptly with 408
        stalled_resps = await asyncio.gather(
            *(
                chat_upload_infra.client.put(
                    f"/api/v1/dalton/chat-uploads/{upload_id}/parts/1",
                    headers={
                        **headers,
                        "content-type": "application/octet-stream",
                    },
                    content=_stalled_http_stream(),
                )
                for _ in range(6)
            )
        )
        assert [r.status_code for r in stalled_resps] == [408] * 6

        # Subsequent valid part upload + complete succeeds immediately because all slots were released
        ok_put = await chat_upload_infra.client.put(
            f"/api/v1/dalton/chat-uploads/{upload_id}/parts/1",
            headers={
                **headers,
                "content-type": "application/octet-stream",
                "x-part-sha256": part_sha,
            },
            content=payload_bytes,
        )
        assert ok_put.status_code == 200, ok_put.text
        ok_complete = await chat_upload_infra.client.post(
            f"/api/v1/dalton/chat-uploads/{upload_id}/complete",
            headers=headers,
            json={"sha256": part_sha},
        )
        assert ok_complete.status_code == 200, ok_complete.text
        assert storage.compute_stored_object_digest(str(row["object_key"])) == (
            len(payload_bytes),
            part_sha,
        )

        # Expired upload rechecked after slot acquisition returns 410 without writing to MinIO
        init_exp = await chat_upload_infra.client.post(
            "/api/v1/dalton/chat-uploads",
            headers=headers,
            json={
                "workspace_id": workspace_id,
                "filename": "expired_before_put.bin",
                "mime_type": "application/octet-stream",
                "size_bytes": len(payload_bytes),
            },
        )
        assert init_exp.status_code == 200, init_exp.text
        exp_uid = str(init_exp.json()["upload_id"])
        exp_row = await chat_uploads_db.get_upload(exp_uid)
        assert exp_row is not None
        exp_key = str(exp_row["object_key"])
        exp_store_uid = str(exp_row["store_upload_id"])
        chat_upload_infra.tracked_keys.add(exp_key)
        chat_upload_infra.tracked_multiparts.append((exp_key, exp_store_uid))

        async with chat_upload_infra.db() as conn:
            await conn.execute(
                """
                UPDATE dalton_chat_file_uploads
                SET expires_at = NOW() - INTERVAL '2 minutes'
                WHERE upload_id = %s
                """,
                (exp_uid,),
            )

        exp_put = await chat_upload_infra.client.put(
            f"/api/v1/dalton/chat-uploads/{exp_uid}/parts/1",
            headers={
                **headers,
                "content-type": "application/octet-stream",
                "x-part-sha256": part_sha,
            },
            content=payload_bytes,
        )
        assert exp_put.status_code == 410, exp_put.text
        assert (storage.list_uploaded_parts(exp_key, exp_store_uid) or []) == []

    @pytest.mark.asyncio
    async def test_shared_workspace_accepted_file_recovers_for_owner_after_colleague_access_removal(
        self,
        chat_upload_infra,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify an accepted attachment uploaded by a collaborator in a shared workspace
        remains recoverable for the workspace owner on cold sandbox recreation after the
        collaborator's write/roster access is revoked, while denying new uploads by the
        removed collaborator with 403."""
        uid_tag = uuid4().hex[:8]
        owner = await create_user(
            user_id=f"u-share-owner-{uid_tag}",
            email=f"share-owner-{uid_tag}@chat-upload-e2e.test",
            name="Shared Workspace Owner",
        )
        colleague = await create_user(
            user_id=f"u-share-colleague-{uid_tag}",
            email=f"share-colleague-{uid_tag}@chat-upload-e2e.test",
            name="Shared Workspace Colleague",
        )

        monkeypatch.setattr("src.config.settings.HOST_MODE", "oss")
        monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
        monkeypatch.setenv("DALTON_COLLEAGUE_DISPATCH_ENABLED", "true")
        monkeypatch.setenv(
            "DALTON_ALLOWED_EMAILS",
            f"{owner['email']},{colleague['email']}",
        )
        get_dalton_settings.cache_clear()

        working_dir = os.environ.get("DOCKER_SANDBOX_WORK_DIR", "/home/workspace")
        comp = await create_computer(
            owner["user_id"],
            kind="docker",
            name=f"shared-comp-{uid_tag}",
            is_primary=True,
            status="stopped",
            root_dir=working_dir,
        )
        computer_id = str(comp["computer_id"])
        ws_shared = await create_workspace_on_computer(
            owner["user_id"],
            "shared_deal_room",
            computer_id,
            description="Shared team workspace",
        )
        assert ws_shared is not None
        ws_shared_id = str(ws_shared["workspace_id"])
        dir_name = str(ws_shared["dir_name"])
        project = ProjectContext(workspace_id=ws_shared_id, dir_name=dir_name)
        layout = WorkspaceLayout(root=working_dir, dir_name=dir_name)

        await dalton_sharing.upsert_workspace_sharing(
            ws_shared_id,
            cos_access="rw",
            web_access="rw",
            updated_by=owner["user_id"],
        )

        sandbox_1: PTCSandbox | None = None
        sandbox_2: PTCSandbox | None = None
        try:
            claimed_1 = await try_claim_computer_for_start(
                computer_id,
                from_status="stopped",
                expected_provider_ref=None,
                require_provider_ref=True,
            )
            assert claimed_1 is not None
            sandbox_1 = await _start_docker_sandbox(dir_name=dir_name)
            assert sandbox_1.sandbox_id is not None
            sandbox_1_id = sandbox_1.sandbox_id
            bound_1 = await try_bind_computer_provider_ref(
                computer_id,
                provider_ref=sandbox_1_id,
                expected_previous_provider_ref=None,
                platform_secret_version=0,
            )
            assert bound_1 is not None

            # Colleague uploads a multi-part file (~10 MiB) and turn durably accepts it
            colleague_file_size = CHAT_UPLOAD_PART_SIZE + 2 * _MIB
            up_col, key_col, sha_col = await _upload_streamed_file_via_http(
                chat_upload_infra.client,
                user_id=colleague["user_id"],
                workspace_id=ws_shared_id,
                filename="colleague_due_diligence.bin",
                mime_type="application/octet-stream",
                total_bytes=colleague_file_size,
                seed_tag=b"colleague-shared-10m",
                tracked_keys=chat_upload_infra.tracked_keys,
                tracked_multiparts=chat_upload_infra.tracked_multiparts,
            )
            ctx_col = [
                {
                    "type": "chat_upload",
                    "upload_id": up_col["upload_id"],
                    "name": up_col["filename"],
                    "mime_type": up_col["mime_type"],
                    "size": colleague_file_size,
                }
            ]
            await validate_and_claim_request_attachments(
                ctx_col,
                user_id=colleague["user_id"],
                workspace_id=ws_shared_id,
                claim_key="req:colleague-accepted",
            )
            await confirm_request_chat_uploads(
                ctx_col,
                user_id=colleague["user_id"],
                workspace_id=ws_shared_id,
                claim_key="req:colleague-accepted",
            )
            chat_req = ChatRequest(
                messages=[ChatMessage(role="user", content="Uploaded DD model")],
                workspace_id=ws_shared_id,
                additional_context=ctx_col,
            )
            await attach_request_files(
                [{"role": "user", "content": "Uploaded DD model"}],
                chat_req,
                SimpleNamespace(sandbox=sandbox_1),
                "gpt-5",
                SimpleNamespace(input_modalities=["text", "image", "pdf"]),
                project=project,
                user_id=colleague["user_id"],
                workspace_id=ws_shared_id,
            )

            row_col = await chat_uploads_db.get_upload(up_col["upload_id"])
            assert row_col is not None and row_col["used_at"] is not None
            rel_col = build_sandbox_upload_rel_path(row_col)

            # Owner revokes colleague write access (`web_access='ro'`) AND colleague later leaves allowlist
            await dalton_sharing.upsert_workspace_sharing(
                ws_shared_id,
                cos_access="ro",
                web_access="ro",
                updated_by=owner["user_id"],
            )
            colleague_headers = {"x-test-user-id": colleague["user_id"]}
            denied_ro = await chat_upload_infra.client.post(
                "/api/v1/dalton/chat-uploads",
                headers=colleague_headers,
                json={
                    "workspace_id": ws_shared_id,
                    "filename": "unauthorized_after_revoke.bin",
                    "mime_type": "application/octet-stream",
                    "size_bytes": 4096,
                },
            )
            assert denied_ro.status_code == 403, denied_ro.text

            with pytest.raises(HTTPException) as denied_claim_exc:
                await validate_and_claim_request_attachments(
                    ctx_col,
                    user_id=colleague["user_id"],
                    workspace_id=ws_shared_id,
                    claim_key="req:colleague-reuse-after-revoke",
                )
            assert denied_claim_exc.value.status_code == 403

            monkeypatch.setenv("DALTON_ALLOWED_EMAILS", owner["email"])
            get_dalton_settings.cache_clear()
            denied_offboarded = await chat_upload_infra.client.post(
                "/api/v1/dalton/chat-uploads",
                headers=colleague_headers,
                json={
                    "workspace_id": ws_shared_id,
                    "filename": "unauthorized_after_offboard.bin",
                    "mime_type": "application/octet-stream",
                    "size_bytes": 4096,
                },
            )
            assert denied_offboarded.status_code in (403, 404), denied_offboarded.text

            # Destroy sandbox #1, start cold sandbox #2, and verify owner recovers the accepted file
            assert await update_computer_status(computer_id, "stopping", expected="running") is not None
            await sandbox_1.cleanup()
            sandbox_1 = None
            assert await update_computer_status(computer_id, "stopped", expected="stopping") is not None

            offline_tree = await FilePersistenceService.get_file_tree(ws_shared_id)
            offline_by_path = {entry["path"]: entry for entry in offline_tree}
            assert rel_col in offline_by_path
            assert offline_by_path[rel_col]["size"] == colleague_file_size

            sandbox_2 = await _start_docker_sandbox(dir_name=dir_name)
            assert sandbox_2.sandbox_id is not None
            assert await try_claim_computer_for_start(
                computer_id,
                from_status="stopped",
                expected_provider_ref=sandbox_1_id,
                require_provider_ref=True,
            ) is not None
            assert await try_bind_computer_provider_ref(
                computer_id,
                provider_ref=sandbox_2.sandbox_id,
                expected_previous_provider_ref=sandbox_1_id,
                platform_secret_version=0,
            ) is not None

            restored = await FilePersistenceService.maybe_restore(
                ws_shared_id, sandbox_2, layout=layout
            )
            assert restored is True
            assert storage.object_size(key_col) == colleague_file_size

            abs_col_cold = sandbox_2.normalize_path(rel_col, project=project)
            verify_res = await sandbox_2.execute_bash_command(
                "python3 -c '"
                "import hashlib, json, os, sys\n"
                "p = sys.argv[1]\n"
                "data = open(p, \"rb\").read()\n"
                "print(json.dumps({\"size\": len(data), \"sha256\": hashlib.sha256(data).hexdigest()}))\n"
                f"' {shlex.quote(abs_col_cold)}"
            )
            assert verify_res.get("success") is True, verify_res
            cold_info = json.loads(str(verify_res.get("stdout") or "").strip())
            assert cold_info == {"size": colleague_file_size, "sha256": sha_col}
        finally:
            if sandbox_1 is not None:
                await sandbox_1.cleanup()
            if sandbox_2 is not None:
                await sandbox_2.cleanup()

    @pytest.mark.asyncio
    async def test_mixed_200mib_and_inline_steers_parked_promotion_and_partial_stage_cleanup(
        self,
        chat_upload_infra,
        docker_ptc_sandbox: PTCSandbox,
    ) -> None:
        """Verify mixed 200 MiB + inline PNG/PDF steers, small-only steers, file-only steers,
        parked-steer turn promotion, and pre-acceptance rollback of staged inline blobs."""
        uid_tag = uuid4().hex[:8]
        user = await create_user(
            user_id=f"u-mixed-{uid_tag}",
            email=f"mixed-{uid_tag}@chat-upload-e2e.test",
            name="Mixed Steer Tester",
        )
        working_dir = os.environ.get("DOCKER_SANDBOX_WORK_DIR", "/home/workspace")
        comp = await create_computer(
            user["user_id"],
            kind="docker",
            name=f"mixed-comp-{uid_tag}",
            is_primary=True,
            status="stopped",
            root_dir=working_dir,
        )
        computer_id = str(comp["computer_id"])
        assert docker_ptc_sandbox.sandbox_id is not None
        assert (
            await try_claim_computer_for_start(
                computer_id,
                from_status="stopped",
                expected_provider_ref=None,
                require_provider_ref=True,
            )
            is not None
        )
        assert (
            await try_bind_computer_provider_ref(
                computer_id,
                provider_ref=docker_ptc_sandbox.sandbox_id,
                expected_previous_provider_ref=None,
                platform_secret_version=0,
            )
            is not None
        )
        ws = await create_workspace_on_computer(
            user["user_id"],
            "mixed_steer_ws",
            computer_id,
            description="Mixed steer E2E",
        )
        assert ws is not None
        workspace_id = str(ws["workspace_id"])
        dir_name = str(ws["dir_name"])
        project = ProjectContext(workspace_id=workspace_id, dir_name=dir_name)
        await docker_ptc_sandbox._ensure_workspace_dirs(dir_name)

        up200, key200, sha200 = await _upload_streamed_file_via_http(
            chat_upload_infra.client,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            filename="ledger_200m.bin",
            mime_type="application/octet-stream",
            total_bytes=_SIZE_200_MIB,
            seed_tag=b"mixed-200m",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )

        png_bytes = b"\x89PNG\r\n\x1a\n" + b"chart-pixels-" * 128
        pdf_bytes = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
        png_sha = hashlib.sha256(png_bytes).hexdigest()
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
        png_url = f"data:image/png;base64,{base64.b64encode(png_bytes).decode()}"
        pdf_url = f"data:application/pdf;base64,{base64.b64encode(pdf_bytes).decode()}"

        tid = str(uuid4())
        await create_thread(tid, workspace_id, "completed", user_id=user["user_id"])
        run1_id = str(uuid4())
        scope1 = RunScope(user_id=user["user_id"], burst_slot_id=None)

        session_ns = SimpleNamespace(sandbox=docker_ptc_sandbox)
        wm_stub = create_autospec(WorkspaceManager, instance=True)
        wm_stub.get_session_for_workspace.return_value = session_ns

        try:
            await begin_run(
                ChatRequest(
                    messages=[ChatMessage(role="user", content="Open live run")],
                    workspace_id=workspace_id,
                    request_key=_rk(f"mixed-open-{uid_tag}"),
                ),
                scope=scope1,
                thread_id=tid,
                run_id=run1_id,
                workspace_id=workspace_id,
                user_id=user["user_id"],
                is_byok=False,
                query_content="Open live run",
                query_type="initial",
                feedback_action=None,
                query_metadata={},
                fork=None,
                is_checkpoint_replay=False,
            )

            # 1. Partial staging failure rolls back newly staged inline blob without aborting caller's 200 MiB upload
            real_upload_part = storage.upload_part_bytes
            part_calls = 0

            def _fail_second_inline(
                object_key: str,
                store_upload_id: str,
                *,
                part_number: int,
                data: bytes,
            ) -> str | None:
                nonlocal part_calls
                part_calls += 1
                if part_calls == 2:
                    return None
                return real_upload_part(
                    object_key,
                    store_upload_id,
                    part_number=part_number,
                    data=data,
                )

            mixed_ctx = [
                {
                    "type": "chat_upload",
                    "upload_id": up200["upload_id"],
                    "name": up200["filename"],
                    "mime_type": up200["mime_type"],
                    "size": _SIZE_200_MIB,
                },
                {"type": "image", "data": png_url, "description": "margin_chart.png"},
                {"type": "pdf", "data": pdf_url, "description": "board_memo.pdf"},
            ]
            with patch.object(
                storage,
                "upload_part_bytes",
                create_autospec(real_upload_part, side_effect=_fail_second_inline),
            ):
                with pytest.raises(HTTPException) as partial_exc:
                    await wait_or_steer(
                        LocalRunExecutor.get_instance(),
                        tid,
                        "Partial failure steer",
                        user["user_id"],
                        can_steer=True,
                        request_key=_rk(f"partial-fail-{uid_tag}"),
                        workspace_id=workspace_id,
                        additional_context=mixed_ctx,
                    )
                assert partial_exc.value.status_code == 503

            # Caller's 200 MiB upload remains ready + intact, and zero orphan staged rows or leases remain
            row200_after_fail = await chat_uploads_db.get_upload(up200["upload_id"])
            assert row200_after_fail is not None and row200_after_fail["state"] == "ready"
            assert storage.object_size(key200) == _SIZE_200_MIB
            async with chat_upload_infra.db() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT upload_id, state, cleaned_at, lease_until, claim_token, object_key, store_upload_id
                        FROM dalton_chat_file_uploads
                        WHERE user_id = %s AND upload_id <> %s
                        """,
                        (user["user_id"], up200["upload_id"]),
                    )
                    staged_fail_rows = await cur.fetchall()
            assert len(staged_fail_rows) == 2
            for _, st_val, cl_val, lease_val, tok_val, obj_k, mp_id in staged_fail_rows:
                assert st_val in ("aborted", "failed")
                assert cl_val is not None
                assert lease_val is None
                assert tok_val is None
                assert storage.object_size(str(obj_k)) is None
                assert (storage.list_uploaded_parts(str(obj_k), str(mp_id)) or []) == []

            # 2. Mixed 200 MiB + inline PNG + inline PDF live steer preserves exact order and zero base64 in DB
            rk_mixed = _rk(f"mixed-ok-{uid_tag}")
            claim_mixed = chat_upload_claim_key(request_key=rk_mixed, run_id=str(uuid4()))
            await validate_and_claim_request_attachments(
                mixed_ctx,
                user_id=user["user_id"],
                workspace_id=workspace_id,
                claim_key=claim_mixed,
            )
            ready_m, sse_m = await wait_or_steer(
                LocalRunExecutor.get_instance(),
                tid,
                "Analyze 200m + chart + memo",
                user["user_id"],
                can_steer=True,
                request_key=rk_mixed,
                workspace_id=workspace_id,
                additional_context=mixed_ctx,
                upload_claim_key=claim_mixed,
            )
            assert ready_m is False and isinstance(sse_m, str)
            accepted_m = json.loads(sse_m.split("data: ", 1)[1].strip())
            assert [a["name"] for a in accepted_m["attachments"]] == [
                "ledger_200m.bin",
                "margin_chart.png",
                "board_memo.pdf",
            ]
            assert [a["type"] for a in accepted_m["attachments"]] == [
                "file",
                "image",
                "pdf",
            ]

            mixed_job = await outbox_db.get_steer_job(f"steer:{rk_mixed}")
            assert mixed_job is not None
            assert "data:" not in json.dumps(mixed_job["payload"])
            for item in mixed_job["payload"]["chat_uploads"]:
                staged_row = await chat_uploads_db.get_upload(item["upload_id"])
                assert staged_row is not None and staged_row["used_at"] is not None
                chat_upload_infra.tracked_keys.add(str(staged_row["object_key"]))

            # 3. Small-only inline steer (no pre-uploaded chat_upload) and file-only steer (empty text)
            rk_small = _rk(f"small-only-{uid_tag}")
            ready_s, sse_s = await wait_or_steer(
                LocalRunExecutor.get_instance(),
                tid,
                "Inline screenshot only",
                user["user_id"],
                can_steer=True,
                request_key=rk_small,
                workspace_id=workspace_id,
                additional_context=[
                    {"type": "image", "data": png_url, "description": "solo_shot"}
                ],
            )
            assert ready_s is False and isinstance(sse_s, str)
            small_job = await outbox_db.get_steer_job(f"steer:{rk_small}")
            assert small_job is not None
            for item in small_job["payload"]["chat_uploads"]:
                sr = await chat_uploads_db.get_upload(item["upload_id"])
                assert sr is not None
                chat_upload_infra.tracked_keys.add(str(sr["object_key"]))

            rk_file_only = _rk(f"file-only-{uid_tag}")
            ready_fo, sse_fo = await wait_or_steer(
                LocalRunExecutor.get_instance(),
                tid,
                "",
                user["user_id"],
                can_steer=True,
                request_key=rk_file_only,
                workspace_id=workspace_id,
                additional_context=[
                    {
                        "type": "chat_upload",
                        "upload_id": up200["upload_id"],
                        "name": up200["filename"],
                        "mime_type": up200["mime_type"],
                        "size": _SIZE_200_MIB,
                    }
                ],
            )
            assert ready_fo is False and isinstance(sse_fo, str)

            with patch.object(
                WorkspaceManager,
                "get_instance",
                create_autospec(WorkspaceManager.get_instance, return_value=wm_stub),
            ):
                taken = await SteeredReportInbox().take(tid, run1_id)
            assert len(taken) == 3
            msg_live, delivered_live = _steering_message(
                taken, run1_id, 1700000100.0
            )
            await SteeredReportInbox().confirm(run1_id, [t.job_id for t in taken])
            assert "data:" not in str(msg_live.content)
            pos_200 = str(msg_live.content).index("ledger_200m_")
            pos_png = str(msg_live.content).index("margin_chart_")
            pos_pdf = str(msg_live.content).index("board_memo_")
            assert pos_200 < pos_png < pos_pdf
            assert [a["name"] for a in delivered_live["messages"][0]["attachments"]] == [
                "ledger_200m.bin",
                "margin_chart.png",
                "board_memo.pdf",
            ]
            assert [a["name"] for a in delivered_live["messages"][1]["attachments"]] == [
                "solo_shot.png"
            ]
            assert delivered_live["messages"][2]["content"] == ""
            assert [a["name"] for a in delivered_live["messages"][2]["attachments"]] == [
                "ledger_200m.bin"
            ]

            # Verify all 3 files from mixed steer exist in Docker sandbox with exact SHA-256
            mixed_rows = [
                await chat_uploads_db.get_upload(d["upload_id"])
                for d in mixed_job["payload"]["chat_uploads"]
            ]
            abs_mixed = [
                docker_ptc_sandbox.normalize_path(
                    build_sandbox_upload_rel_path(r), project=project
                )
                for r in mixed_rows
                if r is not None
            ]
            verify_cmd = (
                "python3 -c 'import hashlib, json, sys; "
                "out=[];\n"
                "for p in sys.argv[1:]:\n"
                "    h=hashlib.sha256();\n"
                "    with open(p,\"rb\") as f:\n"
                "        while b:=f.read(1048576): h.update(b)\n"
                "    out.append(h.hexdigest())\n"
                "print(json.dumps(out))' "
                + " ".join(shlex.quote(p) for p in abs_mixed)
            )
            vres = await docker_ptc_sandbox.execute_bash_command(verify_cmd, timeout=120)
            assert vres.get("success") is True, vres
            assert json.loads((vres.get("stdout") or "").strip().splitlines()[-1]) == [
                sha200,
                png_sha,
                pdf_sha,
            ]

            # 4. Parked mixed steer promoted into its own turn via execute_steer
            rk_parked = _rk(f"parked-promo-{uid_tag}")
            await wait_or_steer(
                LocalRunExecutor.get_instance(),
                tid,
                "Parked mixed turn",
                user["user_id"],
                can_steer=True,
                request_key=rk_parked,
                workspace_id=workspace_id,
                additional_context=[
                    {"type": "pdf", "data": pdf_url, "description": "parked_deck.pdf"},
                    {
                        "type": "chat_upload",
                        "upload_id": up200["upload_id"],
                        "name": up200["filename"],
                        "mime_type": up200["mime_type"],
                        "size": _SIZE_200_MIB,
                    },
                ],
            )
            parked_job_before = await outbox_db.get_steer_job(f"steer:{rk_parked}")
            assert parked_job_before is not None
            for item in parked_job_before["payload"]["chat_uploads"]:
                pr = await chat_uploads_db.get_upload(item["upload_id"])
                if pr is not None:
                    chat_upload_infra.tracked_keys.add(str(pr["object_key"]))
        finally:
            await scope1.fail_open("turn-1-completed", status="completed")

        claimed_jobs = await outbox_db.claim_outbox_jobs(
            limit=20, lease_seconds=60, max_attempts=5
        )
        target_claimed = next(
            j for j in claimed_jobs if j.get("idempotency_key") == f"steer:{rk_parked}"
        )

        promoted_bodies: list[dict] = []

        async def _fake_post_turn(
            *,
            thread_id: str,
            body: dict,
            user_id: str,
            **kwargs,
        ) -> tuple[str, str | None]:
            promoted_bodies.append(body)
            promo_req = ChatRequest(**body)
            promo_run_id = str(uuid4())
            promo_scope = RunScope(user_id=user_id, burst_slot_id=None)
            try:
                await begin_run(
                    promo_req,
                    scope=promo_scope,
                    thread_id=thread_id,
                    run_id=promo_run_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    is_byok=False,
                    query_content=body["messages"][0]["content"],
                    query_type="follow_up",
                    feedback_action=None,
                    query_metadata={},
                    fork=None,
                    is_checkpoint_replay=False,
                )
                msgs = await attach_request_files(
                    [{"role": "user", "content": body["messages"][0]["content"]}],
                    promo_req,
                    session_ns,
                    "gpt-5",
                    SimpleNamespace(input_modalities=["text", "image", "pdf"]),
                    project=project,
                    user_id=user_id,
                    workspace_id=workspace_id,
                )
                txt = str(msgs[-1]["content"])
                assert txt.index("parked_deck_") < txt.index("ledger_200m_")
            finally:
                await promo_scope.fail_open("promo-done", status="completed")
            return "started", promo_run_id

        with patch.object(
            notify_turn,
            "post_notification_turn",
            create_autospec(notify_turn.post_notification_turn, side_effect=_fake_post_turn),
        ):
            await execute_steer(target_claimed)

        assert len(promoted_bodies) == 1
        settled_parked = await outbox_db.get_steer_job(f"steer:{rk_parked}")
        assert settled_parked is not None
        assert settled_parked["payload"].get("outcome") == "turn"

    @pytest.mark.asyncio
    async def test_duplicate_request_key_provisional_release_preserves_unused_expiry_and_quota(
        self,
        chat_upload_infra,
    ) -> None:
        """Verify duplicate/concurrent requests sharing request_key with different uploads never
        confirm the rejected upload set, keeping rejected rows unused, expiring, cancellable, and
        counted toward quota while preserving accepted uploads across retries."""
        uid_tag = uuid4().hex[:8]
        user = await create_user(
            user_id=f"u-dup-{uid_tag}",
            email=f"dup-{uid_tag}@chat-upload-e2e.test",
            name="Duplicate Key Tester",
        )
        ws = await create_workspace(
            user_id=user["user_id"],
            name="Dup Key WS",
            description="Provisional release safety",
            status="running",
        )
        workspace_id = str(ws["workspace_id"])
        headers = {"x-test-user-id": user["user_id"]}

        up_accepted, key_accepted, _ = await _upload_streamed_file_via_http(
            chat_upload_infra.client,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            filename="accepted_doc.bin",
            mime_type="application/octet-stream",
            total_bytes=128 * 1024,
            seed_tag=b"dup-accepted",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )
        up_rejected, key_rejected, _ = await _upload_streamed_file_via_http(
            chat_upload_infra.client,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            filename="rejected_dup_doc.bin",
            mime_type="application/octet-stream",
            total_bytes=128 * 1024,
            seed_tag=b"dup-rejected",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )

        tid = str(uuid4())
        await create_thread(tid, workspace_id, "completed", user_id=user["user_id"])
        shared_rk = _rk(f"shared-rk-{uid_tag}")
        run_win = str(uuid4())
        run_dup = str(uuid4())

        ctx_acc = [{"type": "chat_upload", "upload_id": up_accepted["upload_id"]}]
        ctx_rej = [{"type": "chat_upload", "upload_id": up_rejected["upload_id"]}]

        # Both concurrent copies acquire scoped provisional claims under the same request_key
        claim_win = chat_upload_claim_key(request_key=shared_rk, run_id=run_win)
        claim_dup = chat_upload_claim_key(request_key=shared_rk, run_id=run_dup)
        await validate_and_claim_request_attachments(
            ctx_acc, user_id=user["user_id"], workspace_id=workspace_id, claim_key=claim_win
        )
        await validate_and_claim_request_attachments(
            ctx_rej, user_id=user["user_id"], workspace_id=workspace_id, claim_key=claim_dup
        )
        # Also test copy/retry of the SAME accepted file with its own invocation claim
        claim_same_retry = chat_upload_claim_key(
            request_key=shared_rk, run_id=str(uuid4())
        )
        await validate_and_claim_request_attachments(
            ctx_acc,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            claim_key=claim_same_retry,
        )

        scope_win = RunScope(user_id=user["user_id"], burst_slot_id=None)
        try:
            await begin_run(
                ChatRequest(
                    messages=[ChatMessage(role="user", content="Winner")],
                    workspace_id=workspace_id,
                    request_key=shared_rk,
                    additional_context=ctx_acc,
                ),
                scope=scope_win,
                thread_id=tid,
                run_id=run_win,
                workspace_id=workspace_id,
                user_id=user["user_id"],
                is_byok=False,
                query_content="Winner",
                query_type="initial",
                feedback_action=None,
                query_metadata={},
                fork=None,
                is_checkpoint_replay=False,
            )

            # Duplicate request with DIFFERENT upload fails dedup -> ChatUploadSendScope releases claim
            with pytest.raises(tl_db.DuplicateRequestError):
                async with ChatUploadSendScope(
                    additional_context=ctx_rej,
                    user_id=user["user_id"],
                    workspace_id=workspace_id,
                    request_key=shared_rk,
                    run_id=run_dup,
                    preclaimed=True,
                ):
                    await wait_or_steer(
                        LocalRunExecutor.get_instance(),
                        tid,
                        "Duplicate with different file",
                        user["user_id"],
                        can_steer=True,
                        request_key=shared_rk,
                        workspace_id=workspace_id,
                        additional_context=ctx_rej,
                        upload_claim_key=claim_dup,
                    )

            # Release retry copy on the accepted file too -> accepted file stays retained!
            await release_request_chat_uploads(
                user_id=user["user_id"],
                claim_key=claim_same_retry,
                request_key=shared_rk,
                additional_context=ctx_acc,
            )
        finally:
            await scope_win.fail_open("dup-cleanup")

        row_acc = await chat_uploads_db.get_upload(up_accepted["upload_id"])
        row_rej = await chat_uploads_db.get_upload(up_rejected["upload_id"])
        assert row_acc is not None and row_acc["used_at"] is not None
        assert row_acc["expires_at"] is None
        assert storage.object_size(key_accepted) == 128 * 1024

        # Rejected upload MUST still be unused, carry expires_at, have empty pending_claims, and be cancellable!
        assert row_rej is not None
        assert row_rej["used_at"] is None
        assert row_rej["expires_at"] is not None
        assert (row_rej["pending_claims"] or {}) == {}

        del_rej = await chat_upload_infra.client.delete(
            f"/api/v1/dalton/chat-uploads/{up_rejected['upload_id']}",
            headers=headers,
        )
        assert del_rej.status_code == 200, del_rej.text
        assert storage.object_size(key_rejected) is None

    @pytest.mark.asyncio
    async def test_part_upload_live_workspace_write_revocation_before_slot_and_mid_stream(
        self,
        chat_upload_infra,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify receive_chat_upload_part rechecks live workspace write permission before reading,
        after waiting on _PART_UPLOAD_SEMAPHORE, and mid-stream before MinIO upload_part_bytes."""
        from src.dalton.services import chat_uploads as cu_svc

        uid_tag = uuid4().hex[:8]
        owner = await create_user(
            user_id=f"u-part-own-{uid_tag}",
            email=f"part-own-{uid_tag}@chat-upload-e2e.test",
            name="Part Owner",
        )
        colleague = await create_user(
            user_id=f"u-part-col-{uid_tag}",
            email=f"part-col-{uid_tag}@chat-upload-e2e.test",
            name="Part Colleague",
        )
        monkeypatch.setattr("src.config.settings.HOST_MODE", "oss")
        monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
        monkeypatch.setenv("DALTON_COLLEAGUE_DISPATCH_ENABLED", "true")
        monkeypatch.setenv(
            "DALTON_ALLOWED_EMAILS", f"{owner['email']},{colleague['email']}"
        )
        get_dalton_settings.cache_clear()

        ws = await create_workspace(
            user_id=owner["user_id"],
            name="Part Revoke WS",
            description="Live part permission checks",
            status="running",
        )
        ws_id = str(ws["workspace_id"])
        await dalton_sharing.upsert_workspace_sharing(
            ws_id, cos_access="rw", web_access="rw", updated_by=owner["user_id"]
        )

        col_headers = {"x-test-user-id": colleague["user_id"]}
        own_headers = {"x-test-user-id": owner["user_id"]}
        part_bytes = b"streamed-colleague-part-" * 2048
        part_sha = hashlib.sha256(part_bytes).hexdigest()

        async def _init_colleague_upload(name: str) -> tuple[str, str, str]:
            resp = await chat_upload_infra.client.post(
                "/api/v1/dalton/chat-uploads",
                headers=col_headers,
                json={
                    "workspace_id": ws_id,
                    "filename": name,
                    "mime_type": "application/octet-stream",
                    "size_bytes": len(part_bytes),
                },
            )
            assert resp.status_code == 200, resp.text
            uid = str(resp.json()["upload_id"])
            r = await chat_uploads_db.get_upload(uid)
            assert r is not None
            k = str(r["object_key"])
            s = str(r["store_upload_id"])
            chat_upload_infra.tracked_keys.add(k)
            chat_upload_infra.tracked_multiparts.append((k, s))
            return uid, k, s

        uid_pre, key_pre, sid_pre = await _init_colleague_upload("pre_revoke.bin")
        uid_slot, key_slot, sid_slot = await _init_colleague_upload("slot_revoke.bin")
        uid_mid, key_mid, sid_mid = await _init_colleague_upload("mid_revoke.bin")

        real_upload_part = storage.upload_part_bytes
        spy_upload_part = create_autospec(real_upload_part, side_effect=real_upload_part)

        with patch.object(storage, "upload_part_bytes", spy_upload_part):
            # 1. Revoke BEFORE part request -> 403, zero MinIO upload_part_bytes calls
            await dalton_sharing.upsert_workspace_sharing(
                ws_id, cos_access="ro", web_access="ro", updated_by=owner["user_id"]
            )
            r_pre = await chat_upload_infra.client.put(
                f"/api/v1/dalton/chat-uploads/{uid_pre}/parts/1",
                headers={**col_headers, "x-part-sha256": part_sha},
                content=part_bytes,
            )
            assert r_pre.status_code == 403, r_pre.text
            assert spy_upload_part.call_count == 0
            assert (storage.list_uploaded_parts(key_pre, sid_pre) or []) == []

            # Restore rw, saturate semaphore slots, revoke WHILE waiting for slot -> 403, zero MinIO calls
            await dalton_sharing.upsert_workspace_sharing(
                ws_id, cos_access="rw", web_access="rw", updated_by=owner["user_id"]
            )
            sem = asyncio.Semaphore(1)
            monkeypatch.setattr(cu_svc, "_PART_UPLOAD_SEMAPHORE", sem)
            await sem.acquire()
            slot_wait_task: asyncio.Task | None = None
            try:
                slot_wait_task = asyncio.create_task(
                    chat_upload_infra.client.put(
                        f"/api/v1/dalton/chat-uploads/{uid_slot}/parts/1",
                        headers={**col_headers, "x-part-sha256": part_sha},
                        content=part_bytes,
                    )
                )
                await asyncio.sleep(0.05)
                await dalton_sharing.upsert_workspace_sharing(
                    ws_id, cos_access="ro", web_access="ro", updated_by=owner["user_id"]
                )
            finally:
                sem.release()
            r_slot = await slot_wait_task
            assert r_slot.status_code == 403, r_slot.text
            assert spy_upload_part.call_count == 0
            assert (storage.list_uploaded_parts(key_slot, sid_slot) or []) == []

            # 3. Restore rw, revoke MID-STREAM during slow chunked body -> 403, zero MinIO calls, slot freed
            await dalton_sharing.upsert_workspace_sharing(
                ws_id, cos_access="rw", web_access="rw", updated_by=owner["user_id"]
            )
            first_chunk_sent = asyncio.Event()
            allow_second_chunk = asyncio.Event()

            async def _slow_chunks():
                half = len(part_bytes) // 2
                yield part_bytes[:half]
                first_chunk_sent.set()
                await allow_second_chunk.wait()
                yield part_bytes[half:]

            mid_task = asyncio.create_task(
                chat_upload_infra.client.put(
                    f"/api/v1/dalton/chat-uploads/{uid_mid}/parts/1",
                    headers={**col_headers, "x-part-sha256": part_sha},
                    content=_slow_chunks(),
                )
            )
            await asyncio.wait_for(first_chunk_sent.wait(), timeout=5.0)
            await dalton_sharing.upsert_workspace_sharing(
                ws_id, cos_access="ro", web_access="ro", updated_by=owner["user_id"]
            )
            allow_second_chunk.set()
            r_mid = await mid_task
            assert r_mid.status_code == 403, r_mid.text
            assert spy_upload_part.call_count == 0
            assert (storage.list_uploaded_parts(key_mid, sid_mid) or []) == []

            # Semaphore slot was cleanly released -> owner's own upload succeeds immediately
            own_init = await chat_upload_infra.client.post(
                "/api/v1/dalton/chat-uploads",
                headers=own_headers,
                json={
                    "workspace_id": ws_id,
                    "filename": "owner_ok.bin",
                    "mime_type": "application/octet-stream",
                    "size_bytes": len(part_bytes),
                },
            )
            assert own_init.status_code == 200
            own_uid = str(own_init.json()["upload_id"])
            own_row = await chat_uploads_db.get_upload(own_uid)
            assert own_row is not None
            chat_upload_infra.tracked_keys.add(str(own_row["object_key"]))
            chat_upload_infra.tracked_multiparts.append(
                (str(own_row["object_key"]), str(own_row["store_upload_id"]))
            )
            own_put = await chat_upload_infra.client.put(
                f"/api/v1/dalton/chat-uploads/{own_uid}/parts/1",
                headers={**own_headers, "x-part-sha256": part_sha},
                content=part_bytes,
            )
            assert own_put.status_code == 200, own_put.text
            assert spy_upload_part.call_count == 1

    @pytest.mark.asyncio
    async def test_permanent_steer_attachment_failure_live_sse_and_replay_idempotency(
        self,
        chat_upload_infra,
    ) -> None:
        """Verify permanently failed held AND parked steers record outcome='failed' with full
        user metadata, emit once live via take_failed, replay via steering_returned on reload,
        never reach model messages, and never revive retired files."""
        uid_tag = uuid4().hex[:8]
        user = await create_user(
            user_id=f"u-fail-{uid_tag}",
            email=f"fail-{uid_tag}@chat-upload-e2e.test",
            name="Failed Steer Replay Tester",
        )
        ws = await create_workspace(
            user_id=user["user_id"],
            name="Failed Steer WS",
            description="Durable steer failure surfacing",
            status="running",
        )
        workspace_id = str(ws["workspace_id"])
        tid = str(uuid4())
        await create_thread(tid, workspace_id, "completed", user_id=user["user_id"])

        up_held, key_held, _ = await _upload_streamed_file_via_http(
            chat_upload_infra.client,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            filename="deleted_mid_turn.csv",
            mime_type="text/csv",
            total_bytes=32 * 1024,
            seed_tag=b"fail-held",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )
        up_parked, key_parked, _ = await _upload_streamed_file_via_http(
            chat_upload_infra.client,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            filename="deleted_while_parked.csv",
            mime_type="text/csv",
            total_bytes=32 * 1024,
            seed_tag=b"fail-parked",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )

        run1_id = str(uuid4())
        scope1 = RunScope(user_id=user["user_id"], burst_slot_id=None)
        rk_held = _rk(f"held-fail-{uid_tag}")
        rk_parked = _rk(f"parked-fail-{uid_tag}")

        try:
            await begin_run(
                ChatRequest(
                    messages=[ChatMessage(role="user", content="Turn 1 prompt")],
                    workspace_id=workspace_id,
                    request_key=_rk(f"fail-t1-{uid_tag}"),
                ),
                scope=scope1,
                thread_id=tid,
                run_id=run1_id,
                workspace_id=workspace_id,
                user_id=user["user_id"],
                is_byok=False,
                query_content="Turn 1 prompt",
                query_type="initial",
                feedback_action=None,
                query_metadata={},
                fork=None,
                is_checkpoint_replay=False,
            )

            await wait_or_steer(
                LocalRunExecutor.get_instance(),
                tid,
                "Please check deleted_mid_turn.csv",
                user["user_id"],
                can_steer=True,
                request_key=rk_held,
                workspace_id=workspace_id,
                additional_context=[
                    {"type": "chat_upload", "upload_id": up_held["upload_id"]}
                ],
            )
            row_h = await chat_uploads_db.get_upload(up_held["upload_id"])
            assert row_h is not None
            await retire_deleted_workspace_chat_uploads(
                workspace_id=workspace_id,
                caller_user_id=user["user_id"],
                deleted_client_paths=[build_sandbox_upload_rel_path(row_h)],
            )
            assert storage.object_size(key_held) is None

            # Model inbox returns empty (model never sees failed steer), live failure claims once
            assert await SteeredReportInbox().take(tid, run1_id) == []
            live_failed = await SteeredReportInbox().take_failed(tid, run1_id)
            assert len(live_failed) == 1
            assert live_failed[0]["request_key"] == rk_held
            assert live_failed[0]["content"] == "Please check deleted_mid_turn.csv"
            assert live_failed[0]["attachments"][0]["name"] == "deleted_mid_turn.csv"
            assert await SteeredReportInbox().take_failed(tid, run1_id) == []

            # Admit second steer, let run 1 complete so steer parks, then retire its attachment
            await wait_or_steer(
                LocalRunExecutor.get_instance(),
                tid,
                "Please check deleted_while_parked.csv",
                user["user_id"],
                can_steer=True,
                request_key=rk_parked,
                workspace_id=workspace_id,
                additional_context=[
                    {"type": "chat_upload", "upload_id": up_parked["upload_id"]}
                ],
            )
        finally:
            await scope1.fail_open("turn-1-completed", status="completed")

        row_p = await chat_uploads_db.get_upload(up_parked["upload_id"])
        assert row_p is not None
        await retire_deleted_workspace_chat_uploads(
            workspace_id=workspace_id,
            caller_user_id=user["user_id"],
            deleted_client_paths=[build_sandbox_upload_rel_path(row_p)],
        )
        assert storage.object_size(key_parked) is None

        due_jobs = await outbox_db.claim_outbox_jobs(
            limit=20, lease_seconds=60, max_attempts=5
        )
        parked_claimed = next(
            j for j in due_jobs if j.get("idempotency_key") == f"steer:{rk_parked}"
        )
        await execute_steer(parked_claimed)

        parked_after = await outbox_db.get_steer_job(f"steer:{rk_parked}")
        assert parked_after is not None
        assert parked_after["status"] == "done"
        assert parked_after["payload"].get("outcome") == "failed"

        # Replay thread data & stub_page surface BOTH failed steers deterministically across reloads
        rows_1 = await get_replay_thread_data(tid)
        assert rows_1 is not None
        page_1 = await stub_page(rows_1)
        returned_1 = [
            line.item()["data"]
            for line in page_1.lines
            if line.item()["event"] == "steering_returned"
        ]
        assert [r["request_key"] for r in returned_1] == [rk_held, rk_parked]
        assert all(r["reason"] == "attachment_failed" and r.get("error") for r in returned_1)

        # Second reload produces the exact same two events without duplication
        rows_2 = await get_replay_thread_data(tid)
        assert rows_2 is not None
        page_2 = await stub_page(rows_2)
        returned_2 = [
            line.item()["data"]
            for line in page_2.lines
            if line.item()["event"] == "steering_returned"
        ]
        assert [r["request_key"] for r in returned_2] == [rk_held, rk_parked]

    @pytest.mark.asyncio
    async def test_materialize_disk_budget_charges_only_missing_or_corrupt_files(
        self,
        chat_upload_infra,
        docker_ptc_sandbox: PTCSandbox,
    ) -> None:
        """Verify materialize_chat_uploads skips charging disk space for already-present SHA-verified
        200 MiB files, rejects same-size corrupt files when disk is tight, and deduplicates concurrent
        or repeated descriptors without double-charging."""
        from src.dalton.services import chat_uploads as cu_svc

        uid_tag = uuid4().hex[:8]
        user = await create_user(
            user_id=f"u-disk-{uid_tag}",
            email=f"disk-{uid_tag}@chat-upload-e2e.test",
            name="Disk Budget Tester",
        )
        ws = await create_workspace(
            user_id=user["user_id"],
            name="Disk Budget WS",
            description="Exact disk accounting E2E",
            status="running",
        )
        workspace_id = str(ws["workspace_id"])
        project = ProjectContext(workspace_id=workspace_id, dir_name=f"disk_{uid_tag}")
        await docker_ptc_sandbox._ensure_workspace_dirs(project.dir_name)

        up200, _, sha200 = await _upload_streamed_file_via_http(
            chat_upload_infra.client,
            user_id=user["user_id"],
            workspace_id=workspace_id,
            filename="already_on_disk_200m.bin",
            mime_type="application/octet-stream",
            total_bytes=_SIZE_200_MIB,
            seed_tag=b"disk-200m",
            tracked_keys=chat_upload_infra.tracked_keys,
            tracked_multiparts=chat_upload_infra.tracked_multiparts,
        )
        ctx200 = [
            {
                "type": "chat_upload",
                "upload_id": up200["upload_id"],
                "name": up200["filename"],
                "mime_type": up200["mime_type"],
                "size": _SIZE_200_MIB,
            }
        ]
        await confirm_request_chat_uploads(
            ctx200, user_id=user["user_id"], workspace_id=workspace_id
        )

        # Step 1 (ensure_workspace_chat_uploads_present): places 200 MiB on disk
        ensured = await ensure_workspace_chat_uploads_present(
            workspace_id, docker_ptc_sandbox, project=project
        )
        assert ensured == {"restored": 1, "errors": 0}

        row200 = await chat_uploads_db.get_upload(up200["upload_id"])
        assert row200 is not None
        rel200 = build_sandbox_upload_rel_path(row200)
        abs200 = docker_ptc_sandbox.normalize_path(rel200, project=project)

        # Step 2: Only 120 MiB above CRITICAL_FREE_BYTES remains (< 200 MiB).
        # Because exact 200 MiB SHA-verified file is already on disk, materialize_chat_uploads MUST succeed!
        low_free = CRITICAL_FREE_BYTES + 120 * _MIB
        real_pull = cu_svc.pull_direct
        pull_spy = create_autospec(real_pull, side_effect=real_pull)

        async def _low_disk(_sbx, _work_dir: str) -> int | None:
            return low_free

        with (
            patch.object(
                cu_svc,
                "_free_disk_bytes",
                create_autospec(cu_svc._free_disk_bytes, side_effect=_low_disk),
            ),
            patch.object(cu_svc, "pull_direct", pull_spy),
        ):
            notes_ok = await materialize_chat_uploads(
                ctx200,
                sandbox=docker_ptc_sandbox,
                user_id=user["user_id"],
                workspace_id=workspace_id,
                project=project,
            )
            assert len(notes_ok) == 1 and rel200 in notes_ok[0]
            assert pull_spy.call_count == 0

            # Step 3: Corrupt 1 byte in place (same 200 MiB size!) -> SHA probe catches corruption and
            # enforces disk check (rejecting under low_free instead of trusting same-size corrupt file!)
            corrupt_res = await docker_ptc_sandbox.execute_bash_command(
                "python3 -c 'import sys; p=sys.argv[1]; "
                "f=open(p,\"r+b\"); b=f.read(1); f.seek(0); f.write(bytes([b[0]^0xFF])); f.close()' "
                f"{shlex.quote(abs200)}"
            )
            assert corrupt_res.get("success") is True, corrupt_res

            with pytest.raises(RuntimeError, match="Not enough free disk space"):
                await materialize_chat_uploads(
                    ctx200,
                    sandbox=docker_ptc_sandbox,
                    user_id=user["user_id"],
                    workspace_id=workspace_id,
                    project=project,
                )

        # Step 4: With 250 MiB above CRITICAL_FREE_BYTES (< 400 MiB!), duplicate descriptors [ctx200, ctx200]
        # and concurrent placements charge 200 MiB once (not 400 MiB), heal the corrupt file, and verify SHA!
        enough_for_one = CRITICAL_FREE_BYTES + 250 * _MIB

        async def _single_budget(_sbx, _work_dir: str) -> int | None:
            return enough_for_one

        with patch.object(
            cu_svc,
            "_free_disk_bytes",
            create_autospec(cu_svc._free_disk_bytes, side_effect=_single_budget),
        ):
            res_a, res_b = await asyncio.gather(
                materialize_chat_uploads(
                    ctx200 + ctx200,
                    sandbox=docker_ptc_sandbox,
                    user_id=user["user_id"],
                    workspace_id=workspace_id,
                    project=project,
                ),
                materialize_chat_uploads(
                    ctx200,
                    sandbox=docker_ptc_sandbox,
                    user_id=user["user_id"],
                    workspace_id=workspace_id,
                    project=project,
                ),
            )
            assert len(res_a) == 2 and len(res_b) == 1

        healed = await docker_ptc_sandbox.execute_bash_command(
            "python3 -c 'import hashlib, json, sys; p=sys.argv[1]; h=hashlib.sha256(); n=0;\n"
            "with open(p,\"rb\") as f:\n"
            "    while b:=f.read(1048576): n+=len(b); h.update(b)\n"
            "print(json.dumps([n, h.hexdigest()]))' "
            f"{shlex.quote(abs200)}",
            timeout=120,
        )
        assert healed.get("success") is True, healed
        assert json.loads((healed.get("stdout") or "").strip().splitlines()[-1]) == [
            _SIZE_200_MIB,
            sha200,
        ]
