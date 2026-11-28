"""Unit tests for scripts/dalton/ops/prod/wait_backend_idle.sh.

Uses a fake ``docker`` executable in PATH that runs the script's exact SQL
against an isolated SQLite substitute database (translating PostgreSQL's
``NOW() - INTERVAL '15 minutes'`` to SQLite's ``datetime('now', '-15 minutes')``),
verifying that:
1. Stale automation threads (>15m without update) whose associated automation
   executions are ALL terminal (completed / failed / skipped / cancelled / timeout)
   and have no live conversation_responses row do NOT block deployment.
2. Fresh (<15m) automation threads still count as busy.
3. Stale (>15m) automation threads with any non-terminal execution
   (pending / waiting / running) still count as busy.
4. Interactive user chat threads (no associated automation_executions) >15m old
   still count as busy.
5. Stale (>15m) automation threads with a live follow-up conversation_responses
   row (status='in_progress') still count as busy.
6. Active subagent_runs (status='in_progress') still count as busy.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WAIT_IDLE_SCRIPT = _REPO_ROOT / "scripts/dalton/ops/prod/wait_backend_idle.sh"

_FAKE_DOCKER_PY = r'''#!{python}
import os
import re
import sqlite3
import sys

args = sys.argv[1:]
# Expected invocation:
#   docker exec <container> psql -U postgres -d postgres -Atc "<SQL>"
try:
    idx = args.index("-Atc")
except ValueError:
    print(f"unexpected docker args: {{args}}", file=sys.stderr)
    sys.exit(2)

sql = args[idx + 1]
sql_sqlite = re.sub(
    r"NOW\(\)\s*-\s*INTERVAL\s*'(\d+)\s+minutes'",
    lambda m: f"datetime('now', '-{{m.group(1)}} minutes')",
    sql,
    flags=re.IGNORECASE,
)

db_path = os.environ["FAKE_PG_SQLITE_DB"]
conn = sqlite3.connect(db_path)
try:
    cur = conn.execute(sql_sqlite)
    row = cur.fetchone()
    print(row[0] if row is not None else "")
finally:
    conn.close()
'''


def _init_fake_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE conversation_threads (
            conversation_thread_id TEXT PRIMARY KEY,
            current_status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE automation_executions (
            automation_execution_id TEXT PRIMARY KEY,
            conversation_thread_id TEXT,
            status TEXT NOT NULL
        );
        CREATE TABLE conversation_responses (
            conversation_response_id TEXT PRIMARY KEY,
            conversation_thread_id TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE subagent_runs (
            task_run_id TEXT PRIMARY KEY,
            status TEXT NOT NULL
        );
        """
    )
    conn.commit()
    return conn


def _run_wait_backend_idle(
    tmp_path: Path,
    db_path: Path,
    *,
    max_checks: int = 2,
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir(exist_ok=True)
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        _FAKE_DOCKER_PY.format(python=sys.executable),
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["FAKE_PG_SQLITE_DB"] = str(db_path)
    env["WAIT_BACKEND_IDLE_SLEEP_SECONDS"] = "0"
    env["WAIT_BACKEND_IDLE_MAX_CHECKS"] = str(max_checks)

    return subprocess.run(
        ["bash", str(_WAIT_IDLE_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.mark.parametrize(
    "terminal_status",
    ["completed", "failed", "skipped", "cancelled", "timeout"],
)
def test_wait_backend_idle_ignores_stale_automation_thread_with_all_terminal_executions(
    tmp_path: Path, terminal_status: str
) -> None:
    """A >15m stale in_progress thread whose automation executions are all terminal is idle."""
    db_path = tmp_path / "db.sqlite3"
    conn = _init_fake_db(db_path)
    conn.execute(
        "INSERT INTO conversation_threads VALUES ('t-stale', 'in_progress', datetime('now', '-20 minutes'))"
    )
    conn.execute(
        "INSERT INTO automation_executions VALUES ('e-1', 't-stale', ?)",
        (terminal_status,),
    )
    conn.execute(
        "INSERT INTO automation_executions VALUES ('e-2', 't-stale', 'completed')"
    )
    conn.commit()
    conn.close()

    res = _run_wait_backend_idle(tmp_path, db_path, max_checks=2)
    assert res.returncode == 0, f"stderr={res.stderr} stdout={res.stdout}"
    assert "idle after 2 min" in res.stdout


def test_wait_backend_idle_treats_recent_automation_thread_as_busy(
    tmp_path: Path,
) -> None:
    """An automation thread updated <15m ago still counts as busy even if its execution failed."""
    db_path = tmp_path / "db.sqlite3"
    conn = _init_fake_db(db_path)
    conn.execute(
        "INSERT INTO conversation_threads VALUES ('t-fresh', 'in_progress', datetime('now', '-5 minutes'))"
    )
    conn.execute(
        "INSERT INTO automation_executions VALUES ('e-1', 't-fresh', 'failed')"
    )
    conn.commit()
    conn.close()

    res = _run_wait_backend_idle(tmp_path, db_path, max_checks=2)
    assert res.returncode == 1
    assert "threads=1 subagents=0" in res.stdout
    assert "still_busy" in res.stdout


@pytest.mark.parametrize("non_terminal", ["pending", "waiting", "running"])
def test_wait_backend_idle_treats_stale_thread_with_open_automation_execution_as_busy(
    tmp_path: Path, non_terminal: str
) -> None:
    """A >15m thread with any non-terminal automation execution remains busy."""
    db_path = tmp_path / "db.sqlite3"
    conn = _init_fake_db(db_path)
    conn.execute(
        "INSERT INTO conversation_threads VALUES ('t-open', 'in_progress', datetime('now', '-30 minutes'))"
    )
    conn.execute(
        "INSERT INTO automation_executions VALUES ('e-done', 't-open', 'failed')"
    )
    conn.execute(
        "INSERT INTO automation_executions VALUES ('e-live', 't-open', ?)",
        (non_terminal,),
    )
    conn.commit()
    conn.close()

    res = _run_wait_backend_idle(tmp_path, db_path, max_checks=2)
    assert res.returncode == 1
    assert "threads=1 subagents=0" in res.stdout


def test_wait_backend_idle_treats_stale_interactive_chat_thread_as_busy(
    tmp_path: Path,
) -> None:
    """A >15m in_progress thread with NO automation executions (user chat) stays busy."""
    db_path = tmp_path / "db.sqlite3"
    conn = _init_fake_db(db_path)
    conn.execute(
        "INSERT INTO conversation_threads VALUES ('t-user-chat', 'in_progress', datetime('now', '-25 minutes'))"
    )
    conn.commit()
    conn.close()

    res = _run_wait_backend_idle(tmp_path, db_path, max_checks=2)
    assert res.returncode == 1
    assert "threads=1 subagents=0" in res.stdout


def test_wait_backend_idle_treats_stale_automation_thread_with_live_response_as_busy(
    tmp_path: Path,
) -> None:
    """A >15m automation thread with a live in_progress conversation_responses turn stays busy."""
    db_path = tmp_path / "db.sqlite3"
    conn = _init_fake_db(db_path)
    conn.execute(
        "INSERT INTO conversation_threads VALUES ('t-followup', 'in_progress', datetime('now', '-25 minutes'))"
    )
    conn.execute(
        "INSERT INTO automation_executions VALUES ('e-1', 't-followup', 'completed')"
    )
    conn.execute(
        "INSERT INTO conversation_responses VALUES ('r-1', 't-followup', 'in_progress')"
    )
    conn.commit()
    conn.close()

    res = _run_wait_backend_idle(tmp_path, db_path, max_checks=2)
    assert res.returncode == 1
    assert "threads=1 subagents=0" in res.stdout


def test_wait_backend_idle_still_waits_for_in_progress_subagent_runs(
    tmp_path: Path,
) -> None:
    """Active subagent_runs still block idle even when no threads are busy."""
    db_path = tmp_path / "db.sqlite3"
    conn = _init_fake_db(db_path)
    conn.execute(
        "INSERT INTO conversation_threads VALUES ('t-stale', 'in_progress', datetime('now', '-20 minutes'))"
    )
    conn.execute(
        "INSERT INTO automation_executions VALUES ('e-1', 't-stale', 'failed')"
    )
    conn.execute(
        "INSERT INTO subagent_runs VALUES ('sr-1', 'in_progress')"
    )
    conn.commit()
    conn.close()

    res = _run_wait_backend_idle(tmp_path, db_path, max_checks=2)
    assert res.returncode == 1
    assert "threads=0 subagents=1" in res.stdout
