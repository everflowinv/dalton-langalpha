"""Unit tests for `scripts/dalton/backup_agent_preference_096.py` using `create_autospec`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import psycopg
import pytest

from scripts.dalton import backup_agent_preference_096 as mod


def _make_fake_conn(rows: list[dict[str, Any]]) -> tuple[psycopg.Connection[Any], psycopg.Cursor[Any]]:
    conn = create_autospec(psycopg.Connection, instance=True)
    cur = create_autospec(psycopg.Cursor, instance=True)
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchall.return_value = rows
    conn.cursor.return_value = cur
    return conn, cur


def test_backup_sets_read_only_exports_matching_rows_and_prints_count(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [
        {
            "user_id": "user-1",
            "agent_preference": {"output_format": "html", "deliverable": "text", "tone": "direct"},
        },
        {
            "user_id": "user-2",
            "agent_preference": {"deliverable": "visual"},
        },
    ]
    conn, cur = _make_fake_conn(rows)
    out_file = tmp_path / "backup_096.json"

    rc = mod.main([str(out_file)], conn=conn)
    assert rc == 0

    executed_sql = [call.args[0] for call in cur.execute.call_args_list]
    assert executed_sql[0] == "SET TRANSACTION READ ONLY"
    assert "WHERE agent_preference ?| ARRAY['output_format', 'deliverable']" in executed_sql[1]

    saved = json.loads(out_file.read_text(encoding="utf-8"))
    assert saved == rows

    stdout = capsys.readouterr().out
    assert "Backed up 2 rows" in stdout


def test_verify_sets_read_only_compares_against_backup_and_prints_changed_count(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backup_rows = [
        {
            "user_id": "user-1",
            "agent_preference": {"output_format": "html", "deliverable": "text"},
        },
        {
            "user_id": "user-2",
            "agent_preference": {"output_format": ["visual", "markdown", "text"]},
        },
        {
            "user_id": "user-3",
            "agent_preference": {"deliverable": "visual"},
        },
    ]
    backup_file = tmp_path / "backup_096.json"
    backup_file.write_text(json.dumps(backup_rows), encoding="utf-8")

    # Simulate post-096 database state: user-1 and user-3 changed, user-2 unchanged
    current_rows = [
        {
            "user_id": "user-1",
            "agent_preference": {"output_format": ["visual", "markdown", "text"]},
        },
        {
            "user_id": "user-2",
            "agent_preference": {"output_format": ["visual", "markdown", "text"]},
        },
        {
            "user_id": "user-3",
            "agent_preference": {"output_format": ["visual", "markdown", "text"]},
        },
    ]
    conn, cur = _make_fake_conn(current_rows)

    rc = mod.main(["--verify", str(backup_file)], conn=conn)
    assert rc == 0

    executed_sql = [call.args[0] for call in cur.execute.call_args_list]
    assert executed_sql[0] == "SET TRANSACTION READ ONLY"
    assert "WHERE user_id = ANY(%s)" in executed_sql[1]
    assert cur.execute.call_args_list[1].args[1] == (["user-1", "user-2", "user-3"],)

    stdout = capsys.readouterr().out
    assert "2 changed, 1 unchanged" in stdout


def test_open_read_only_connection_uses_database_url_env_var(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    conn, cur = _make_fake_conn(
        [{"user_id": "user-env", "agent_preference": {"output_format": "markdown"}}]
    )
    connect_spy = create_autospec(psycopg.connect, return_value=conn)
    monkeypatch.setattr(mod.psycopg, "connect", connect_spy)
    monkeypatch.setenv("DATABASE_URL", "postgresql://readonly@example.invalid/dalton")

    out_file = tmp_path / "env_backup.json"
    records = mod.backup_agent_preferences(out_file)
    assert len(records) == 1
    assert connect_spy.call_args.args[0] == "postgresql://readonly@example.invalid/dalton"
    assert cur.execute.call_args_list[0].args[0] == "SET TRANSACTION READ ONLY"
    assert conn.read_only is True
    conn.close.assert_called_once()


def test_open_read_only_connection_requires_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        mod.open_read_only_connection()
