"""Tests for Item 7: integration DB fixtures refuse to fall back to localhost:5432."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

_DB_ENV_VARS = (
    "TEST_DB_DSN",
    "DALTON_TEST_DB_DSN",
    "TEST_DATABASE_URL",
    "DALTON_DATA_JOBS_TEST_DSN",
    "DALTON_CHANNEL_GATEWAY_TEST_DSN",
    "DALTON_CHAT_UPLOAD_TEST_DSN",
    "DALTON_TEST_PG_DSN",
    "TEST_DB_HOST",
    "TEST_DB_PORT",
    "TEST_DB_NAME",
    "TEST_DB_USER",
    "TEST_DB_PASSWORD",
    "TEST_DB_SSLMODE",
)


def _load_mod(name: str, rel_path: str) -> Any:
    path = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(name, None)
    return mod


def _clear_db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _DB_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_integration_conftest_build_db_uri_refuses_without_explicit_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tests/integration/conftest.py:_build_db_uri must refuse to run (not fall back to localhost:5432)
    when TEST_DB_HOST / TEST_DB_PORT / TEST_DB_NAME (or explicit DSN) are unset."""
    _clear_db_env(monkeypatch)
    mod = _load_mod("int_conftest_item7", "tests/integration/conftest.py")
    with pytest.raises((RuntimeError, ValueError, pytest.UsageError)) as exc_info:
        mod._build_db_uri()
    msg = str(exc_info.value)
    assert "TEST_DB_HOST" in msg and "TEST_DB_PORT" in msg and "TEST_DB_NAME" in msg
    assert "localhost:5432" in msg or "5432" in msg


def test_integration_conftest_build_db_uri_refuses_partial_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting only TEST_DB_NAME without TEST_DB_HOST and TEST_DB_PORT must still refuse."""
    _clear_db_env(monkeypatch)
    monkeypatch.setenv("TEST_DB_NAME", "langalpha_test")
    mod = _load_mod("int_conftest_item7_partial", "tests/integration/conftest.py")
    with pytest.raises((RuntimeError, ValueError, pytest.UsageError)) as exc_info:
        mod._build_db_uri()
    msg = str(exc_info.value)
    assert "TEST_DB_HOST" in msg
    assert "TEST_DB_PORT" in msg


def test_integration_conftest_build_db_uri_accepts_explicit_vars_or_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit TEST_DB_HOST + TEST_DB_PORT + TEST_DB_NAME or explicit DSN succeeds."""
    mod = _load_mod("int_conftest_item7_ok", "tests/integration/conftest.py")

    _clear_db_env(monkeypatch)
    monkeypatch.setenv("TEST_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("TEST_DB_PORT", "55432")
    monkeypatch.setenv("TEST_DB_NAME", "langalpha_test")
    uri = mod._build_db_uri()
    assert "@127.0.0.1:55432/langalpha_test" in uri

    _clear_db_env(monkeypatch)
    monkeypatch.setenv("TEST_DB_DSN", "postgresql://postgres:postgres@127.0.0.1:55432/dalton_test")
    assert mod._build_db_uri() == "postgresql://postgres:postgres@127.0.0.1:55432/dalton_test"


def test_dalton_integration_conftest_refuses_without_explicit_db_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tests/integration/dalton/conftest.py must refuse to default to langalpha_test / localhost:5432."""
    _clear_db_env(monkeypatch)
    mod = _load_mod("dalton_int_conftest_item7", "tests/integration/dalton/conftest.py")
    with pytest.raises(pytest.exit.Exception) as exc_info:
        mod._ensure_safe_test_db_name()
    msg = str(exc_info.value)
    assert "TEST_DB_HOST" in msg or "DSN" in msg


@pytest.mark.parametrize(
    "rel_path",
    [
        "tests/integration/dalton/test_automation_reports_db.py",
        "tests/integration/dalton/test_channel_chart_snapshots_db.py",
        "tests/integration/dalton/test_channel_gateway_db.py",
        "tests/integration/dalton/test_data_task_notifications_db.py",
        "tests/integration/dalton/test_discord_gateway_db.py",
    ],
)
def test_dalton_db_integration_modules_refuse_without_explicit_env(
    rel_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each Dalton DB integration test helper must refuse to fall back to localhost:5432."""
    _clear_db_env(monkeypatch)
    mod = _load_mod(f"dalton_db_mod_{Path(rel_path).stem}", rel_path)
    with pytest.raises(BaseException) as exc_info:
        mod._get_test_dsn()
    msg = str(exc_info.value)
    assert "TEST_DB_HOST" in msg or "DSN" in msg
    assert "Refusing" in msg or "拒绝" in msg


def test_dalton_integration_conftest_accepts_standalone_data_jobs_test_dsn_and_rejects_non_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Using DALTON_DATA_JOBS_TEST_DSN alone (pointing at a *_test DB) must pass the directory guard,
    while a non-_test DB name must still be rejected."""
    _clear_db_env(monkeypatch)
    monkeypatch.delenv("DALTON_DATA_JOBS_TEST_DSN", raising=False)
    mod = _load_mod("dalton_int_conftest_dj_dsn", "tests/integration/dalton/conftest.py")

    monkeypatch.setenv(
        "DALTON_DATA_JOBS_TEST_DSN",
        "postgresql://postgres:postgres@127.0.0.1:55432/data_jobs_test",
    )
    assert mod._ensure_safe_test_db_name() == "data_jobs_test"

    monkeypatch.setenv(
        "DALTON_DATA_JOBS_TEST_DSN",
        "postgresql://postgres:postgres@127.0.0.1:55432/langalpha_prod",
    )
    with pytest.raises(pytest.exit.Exception) as exc_info:
        mod._ensure_safe_test_db_name()
    assert "must end with '_test'" in str(exc_info.value)


def test_ci_backend_integration_workflow_sets_explicit_test_db_env_vars() -> None:
    """GitHub Actions backend-integration workflow must pass explicit TEST_DB_HOST/PORT/NAME
    so the integration DB guard does not fail collection in CI."""
    wf_text = (REPO_ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    assert "TEST_DB_HOST:" in wf_text
    assert "TEST_DB_PORT:" in wf_text
    assert "TEST_DB_NAME:" in wf_text


@pytest.mark.parametrize(
    "sneaky_dsn",
    [
        "postgresql://postgres:postgres@127.0.0.1:5432/langalpha_test?dbname=langalpha",
        "postgresql://postgres:postgres@127.0.0.1:5432/fake_test?dbname=postgres",
        "host=127.0.0.1 port=5432 dbname=langalpha",
        "postgresql://postgres:postgres@127.0.0.1:5432/langalpha_test?service=prod",
        "postgresql://postgres:postgres@127.0.0.1:5432/",
    ],
)
def test_integration_db_guards_reject_libpq_dbname_override_and_service_bypass(
    sneaky_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 High #4: libpq query-param `?dbname=prod`, key-value DSN, missing dbname, or `service=`
    must be rejected by conninfo_to_dict() across conftest, data_jobs, d048, and all Dalton DB modules."""
    _clear_db_env(monkeypatch)

    # 1. Root tests/integration/conftest.py:_build_db_uri
    monkeypatch.setenv("TEST_DB_DSN", sneaky_dsn)
    root_conftest = _load_mod("int_conftest_override", "tests/integration/conftest.py")
    with pytest.raises(RuntimeError):
        root_conftest._build_db_uri()
    monkeypatch.delenv("TEST_DB_DSN", raising=False)

    # 2. Dalton tests/integration/dalton/conftest.py:_ensure_safe_test_db_name
    monkeypatch.setenv("DALTON_DATA_JOBS_TEST_DSN", sneaky_dsn)
    dalton_conftest = _load_mod("dalton_int_conftest_override", "tests/integration/dalton/conftest.py")
    with pytest.raises(pytest.exit.Exception):
        dalton_conftest._ensure_safe_test_db_name()

    # 3. tests/integration/dalton/test_data_jobs_db.py:_get_test_dsn
    data_jobs_mod = _load_mod("dalton_data_jobs_override", "tests/integration/dalton/test_data_jobs_db.py")
    with pytest.raises(BaseException) as dj_exc:
        data_jobs_mod._get_test_dsn()
    assert "Refusing" in str(dj_exc.value)
    monkeypatch.delenv("DALTON_DATA_JOBS_TEST_DSN", raising=False)

    # 4. tests/integration/dalton/test_d048_invite_admin_tier_migration_db.py:_alembic_env_for_dsn
    d048_mod = _load_mod(
        "dalton_d048_override",
        "tests/integration/dalton/test_d048_invite_admin_tier_migration_db.py",
    )
    with pytest.raises(ValueError) as d048_exc:
        d048_mod._alembic_env_for_dsn(sneaky_dsn)
    assert "Refusing" in str(d048_exc.value)

    # 5. All other Dalton integration DB fixtures using _get_test_dsn / resolve_validated_test_dsn
    monkeypatch.setenv("DALTON_TEST_DB_DSN", sneaky_dsn)
    for rel_path in (
        "tests/integration/dalton/test_automation_reports_db.py",
        "tests/integration/dalton/test_channel_chart_snapshots_db.py",
        "tests/integration/dalton/test_channel_gateway_db.py",
        "tests/integration/dalton/test_data_task_notifications_db.py",
        "tests/integration/dalton/test_discord_gateway_db.py",
    ):
        mod = _load_mod(f"dalton_override_{Path(rel_path).stem}", rel_path)
        with pytest.raises(BaseException) as mod_exc:
            mod._get_test_dsn()
        assert "Refusing" in str(mod_exc.value), rel_path

    upload_mod = _load_mod(
        "dalton_override_chat_upload",
        "tests/integration/dalton/test_chat_large_upload_e2e.py",
    )
    with pytest.raises(BaseException) as up_exc:
        upload_mod._assert_isolated_test_dsn(sneaky_dsn, source="DALTON_TEST_DB_DSN")
    assert "Refusing" in str(up_exc.value)

    wx_mod = _load_mod(
        "dalton_override_wx_harness",
        "tests/integration/dalton/weixin_live_backend_harness.py",
    )
    with pytest.raises(RuntimeError) as wx_exc:
        wx_mod.resolve_validated_test_dsn(sneaky_dsn)
    assert "Refusing" in str(wx_exc.value)


@pytest.mark.parametrize(
    ("dsn", "expected_dbname"),
    [
        ("postgresql://postgres:p%3Fa%23s@127.0.0.1:5432/prod%3Ffoo_test", "prod?foo_test"),
        ("postgresql://postgres:pass@127.0.0.1:5432/prod%23foo_test", "prod#foo_test"),
        ("postgresql://postgres:pass@127.0.0.1:5432/prod%2Ffoo_test", "prod/foo_test"),
        ("postgresql://postgres:pass@127.0.0.1:5432/prod%25foo_test?sslmode=disable", "prod%foo_test"),
        (
            "host=127.0.0.1 port=55432 user=postgres password=secret dbname=isolated_test sslmode=require",
            "isolated_test",
        ),
    ],
)
def test_d048_alembic_subprocess_url_matches_guard_parsed_params(
    dsn: str,
    expected_dbname: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r6 A-fix: d048 Alembic subprocess must pass the guard's parsed libpq params via
    percent-encoded sqlalchemy.url so an encoded `?` (or `#`/`%`/`/`) in dbname cannot
    split the URL and redirect SQLAlchemy to a non-test database."""
    from alembic.config import Config
    from psycopg.conninfo import conninfo_to_dict
    from sqlalchemy import engine_from_config, pool

    monkeypatch.setenv("DB_NAME", "langalpha_prod")
    monkeypatch.setenv("DB_HOST", "prod.internal")
    monkeypatch.setenv("PGDATABASE", "langalpha_prod")
    monkeypatch.setenv("PGSERVICE", "prod")

    d048_mod = _load_mod(
        "dalton_d048_url_roundtrip",
        "tests/integration/dalton/test_d048_invite_admin_tier_migration_db.py",
    )
    env = d048_mod._alembic_env_for_dsn(dsn)
    assert "DB_NAME" not in env
    assert "DB_HOST" not in env
    assert "PGDATABASE" not in env
    assert "PGSERVICE" not in env

    sa_url = env["DALTON_ALEMBIC_SQLALCHEMY_URL"]
    expected_params = {k: str(v) for k, v in conninfo_to_dict(dsn).items() if v is not None}
    assert expected_params["dbname"] == expected_dbname

    for ini_rel in ("alembic.ini", "src/dalton/alembic.ini"):
        cfg = Config(str(REPO_ROOT / ini_rel))
        cfg.set_main_option("sqlalchemy.url", sa_url.replace("%", "%%"))
        section = cfg.get_section(cfg.config_ini_section, {})
        eng = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
        _, connect_params = eng.dialect.create_connect_args(eng.url)
        actual_params = {k: v for k, v in connect_params.items() if k != "context"}
        assert actual_params == expected_params, (ini_rel, actual_params, expected_params)
        assert actual_params["dbname"] == expected_dbname
