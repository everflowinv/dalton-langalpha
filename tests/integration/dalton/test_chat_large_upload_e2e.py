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
from uuid import uuid4

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
from ptc_agent.core.paths import WorkspaceLayout
from ptc_agent.core.project_context import ProjectContext
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

import src.dalton.database as dalton_database_pkg
import src.dalton.database.chat_uploads as chat_uploads_db
from src.dalton import chat_upload_storage as storage
from src.dalton import transcription_storage as asr_store
from src.dalton.app.chat_uploads import router as chat_uploads_router
from src.dalton.services.chat_uploads import (
    CHAT_UPLOAD_PART_SIZE,
    MAX_CHAT_UPLOAD_BYTES,
    build_chat_upload_history_metadata,
    build_sandbox_upload_rel_path,
    cleanup_deleted_workspace_chat_uploads,
    ensure_workspace_chat_uploads_present,
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
from src.server.database.home_workspace import get_home_workspace_id
from src.server.database.pool import _configure_postgres_connection
from src.server.database.session_lock import close_shared_lock_session
from src.server.database.user import create_user
from src.server.database.workspace import (
    create_workspace,
    create_workspace_on_computer,
    delete_workspace as db_delete_workspace,
    files_restore_incomplete,
)
from src.server.handlers.chat.attachments import attach_request_files
from src.server.handlers.chat.request_prep import serialize_context_metadata
from src.server.models.chat import ChatMessage, ChatRequest
from src.server.services.persistence.file import (
    FilePersistenceService,
    RestoreIdentityLost,
)
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
                    )
                """
            )
            verified = await cur.fetchone()
            assert verified is not None
            assert str(verified[0]).endswith("_test")
            assert verified[1] is not None, "Dalton migration d033 did not create dalton_chat_file_uploads"
            assert verified[2] is not None, "alembic_version_dalton table missing after d033 upgrade"
            if not verified[3]:
                await asyncio.to_thread(command.downgrade, dalton_cfg, "d030")
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
                        FROM pg_constraint
                        WHERE conname = 'chk_dalton_chat_file_uploads_cleaned_terminal'
                    )
                """
            )
            schema_flags = await cur.fetchone()
            assert schema_flags == (True, True), f"Incomplete d033 schema on _test DB: {schema_flags!r}"


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

        # 1. Pre-stream claim & ownership check (`messaging.py` path)
        claimed = await validate_and_claim_request_attachments(
            chat_req.additional_context,
            user_id=user["user_id"],
            workspace_id=workspace_id,
        )
        assert [c["upload_id"] for c in claimed] == [up36["upload_id"], up200["upload_id"]]

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
            await validate_and_claim_request_attachments(
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

                claimed_1 = await validate_and_claim_request_attachments(
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
                await validate_and_claim_request_attachments(
                    [
                        {
                            "type": "chat_upload",
                            "upload_id": uid_2b,
                            "name": "stale_fail.bin",
                            "mime_type": "application/octet-stream",
                            "size": size_2,
                        }
                    ],
                    user_id=user["user_id"],
                    workspace_id=workspace_id,
                )
            finally:
                release_2b_fail.set()

            resp_2b_stale = await task_2b_stale
            assert resp_2b_stale.status_code == 200, resp_2b_stale.text
            assert resp_2b_stale.json()["state"] == "ready"
            assert resp_2b_stale.json()["sha256"] == sha_2b

        row_2b = await chat_uploads_db.get_upload(uid_2b)
        assert row_2b is not None
        assert row_2b["state"] == "ready"
        assert row_2b["used_at"] is not None
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
