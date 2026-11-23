"""Offline SQL, static hygiene, and downgrade-protection checks for ``d047_local_auth_and_roles.py``."""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from alembic import op

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_FILE = (
    REPO_ROOT / "src" / "dalton" / "migrations" / "versions" / "d047_local_auth_and_roles.py"
)


def _load_d047_module():
    spec = importlib.util.spec_from_file_location("d047_local_auth_and_roles", MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_d047_migration_does_not_import_business_code() -> None:
    tree = ast.parse(MIGRATION_FILE.read_text(encoding="utf-8"))
    forbidden_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("src.") or alias.name == "src":
                    forbidden_imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("src.") or mod == "src":
                forbidden_imports.append(mod)
    assert forbidden_imports == []


def test_d047_alembic_offline_sql_upgrade_and_downgrade_guard() -> None:
    up = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "src/dalton/alembic.ini",
            "upgrade",
            "d045:d047",
            "--sql",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert up.returncode == 0, f"alembic upgrade --sql failed:\n{up.stderr}"
    sql = up.stdout
    for table in (
        "dalton_user_roles",
        "dalton_credential_holder",
        "dalton_local_credentials",
        "dalton_local_sessions",
        "dalton_local_refresh_tokens",
        "dalton_bootstrap_state",
        "dalton_bootstrap_tokens",
        "dalton_auth_attempts",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "password_changed_at TIMESTAMPTZ NOT NULL" in sql
    assert "ALTER TABLE dalton_invites" in sql
    assert "ADD COLUMN IF NOT EXISTS tier" in sql
    for upstream_tbl in ("users", "workspaces", "conversation_threads", "automations"):
        assert f"ALTER TABLE {upstream_tbl}" not in sql

    down = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "src/dalton/alembic.ini",
            "downgrade",
            "d047:d045",
            "--sql",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert down.returncode == 0, f"alembic downgrade --sql failed:\n{down.stderr}"
    # Item 9 / Round 3 Item 3: downgrade acquires the same bootstrap advisory lock
    # BEFORE checking dalton_bootstrap_state, and the guard runs BEFORE any DROP TABLE!
    from src.dalton.auth.bootstrap import BOOTSTRAP_ADVISORY_LOCK_KEY

    lock_pos = down.stdout.find(f"pg_advisory_xact_lock({BOOTSTRAP_ADVISORY_LOCK_KEY})")
    guard_pos = down.stdout.find("Refusing to downgrade d047")
    drop_pos = down.stdout.find("DROP TABLE IF EXISTS dalton_bootstrap_state")
    assert lock_pos != -1
    assert guard_pos != -1
    assert drop_pos != -1
    assert lock_pos < guard_pos < drop_pos
    assert "DROP TABLE IF EXISTS dalton_local_refresh_tokens" in down.stdout


def test_d047_downgrade_refuses_completed_bootstrap_before_dropping_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 9: verify downgrade aborts on completed bootstrap and leaves all tables untouched."""
    mod = _load_d047_module()
    dropped: list[str] = []
    bootstrap_completed = True

    def _fake_execute(sql: str, *args, **kwargs):
        text = str(sql)
        if "RAISE EXCEPTION" in text and "dalton_bootstrap_state" in text:
            if bootstrap_completed:
                raise RuntimeError(
                    "Refusing to downgrade d047: initial admin bootstrap has already completed"
                )
            return
        if "DROP TABLE" in text:
            dropped.append(text)

    monkeypatch.setattr(mod.op, "execute", create_autospec(op.execute, side_effect=_fake_execute))

    with pytest.raises(RuntimeError, match="Refusing to downgrade d047"):
        mod.downgrade()
    assert dropped == []

    # When bootstrap has NOT completed, downgrade proceeds to drop tables
    bootstrap_completed = False
    mod.downgrade()
    assert any("dalton_bootstrap_state" in stmt for stmt in dropped)
    assert any("dalton_local_refresh_tokens" in stmt for stmt in dropped)
