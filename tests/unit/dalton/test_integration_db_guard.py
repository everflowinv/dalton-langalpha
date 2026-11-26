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
