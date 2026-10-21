"""Unit tests for scripts/dalton/migrate_owner_identity.py.

Uses ``unittest.mock.create_autospec`` exclusively for all test doubles.
"""

from __future__ import annotations

import json
import logging
import stat
import uuid
from pathlib import Path
from typing import Any, Self
from unittest.mock import create_autospec

import pytest

from scripts.dalton import migrate_owner_identity as mod
from scripts.dalton.realrun._common import KNOWN_MIGRATED_WORKSPACES, is_valid_realrun_workspace_row
from src.dalton.settings import get_home_workspace_aliases
from src.server.database.home_workspace import (
    HOME_WORKSPACE_NAMESPACE,
    get_home_workspace_id,
    is_home_id,
    is_home_row,
)


class _CursorProto:
    rowcount: int = 0

    def __enter__(self) -> Self:
        raise NotImplementedError

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        raise NotImplementedError

    def execute(self, query: str, params: Any = None) -> None:
        raise NotImplementedError

    def fetchone(self) -> dict[str, Any] | None:
        raise NotImplementedError

    def fetchall(self) -> list[dict[str, Any]]:
        raise NotImplementedError


class _TxProto:
    def __enter__(self) -> Self:
        raise NotImplementedError

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        raise NotImplementedError


class _ConnProto:
    def cursor(self, *, row_factory: Any = None) -> _CursorProto:
        raise NotImplementedError

    def transaction(self) -> _TxProto:
        raise NotImplementedError


def _default_company_and_home_workspaces(user_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "workspace_id": mod.OLD_HOME_WORKSPACE_ID,
            "user_id": user_id,
            "name": "Home",
            "dir_name": "Home",
            "status": "stopped",
        }
    ]
    for ticker, wid in sorted(KNOWN_MIGRATED_WORKSPACES.items()):
        rows.append(
            {
                "workspace_id": wid,
                "user_id": user_id,
                "name": ticker,
                "dir_name": ticker,
                "status": "stopped",
            }
        )
    return rows


def _make_db_harness(
    *,
    source_user: dict[str, Any] | None = None,
    target_user: dict[str, Any] | None = None,
    email_conflict_user: dict[str, Any] | None = None,
    select_rows_by_table: dict[str, list[dict[str, Any]]] | None = None,
    update_rowcount_overrides: dict[str, int] | None = None,
    post_check_workspaces: list[dict[str, Any]] | None = None,
    db_name: str = "langalpha",
) -> tuple[_ConnProto, list[tuple[str, Any]], dict[str, int]]:
    executed: list[tuple[str, Any]] = []
    tx_state = {"entered": 0, "committed": 0, "rolled_back": 0}

    src_user = (
        dict(source_user)
        if source_user is not None
        else {
            "user_id": mod.OLD_USER_ID,
            "email": None,
            "name": "Local Dev",
            "timezone": "America/New_York",
            "onboarding_completed": True,
        }
    )
    rows_map = dict(select_rows_by_table or {})
    if "workspaces" not in rows_map:
        rows_map["workspaces"] = _default_company_and_home_workspaces(mod.OLD_USER_ID)
    if "computers" not in rows_map:
        rows_map["computers"] = [
            {"computer_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "user_id": mod.OLD_USER_ID}
        ]
    if "dalton_verdicts" not in rows_map:
        rows_map["dalton_verdicts"] = [
            {"verdict_id": "v1", "user_id": mod.OLD_USER_ID},
            {"verdict_id": "v2", "user_id": mod.OLD_USER_ID},
        ]
    if "store" not in rows_map:
        rows_map["store"] = [
            {"prefix": f"{mod.OLD_USER_ID}.memory", "key": "prefs.md", "value": {}}
        ]
    if "checkpoints" not in rows_map:
        rows_map["checkpoints"] = [
            {
                "thread_id": "t1",
                "checkpoint_ns": "",
                "checkpoint_id": "cp1",
                "parent_checkpoint_id": None,
                "type": "json",
                "metadata": {"user_id": mod.OLD_USER_ID},
            }
        ]

    overrides = dict(update_rowcount_overrides or {})
    final_ws = (
        list(post_check_workspaces)
        if post_check_workspaces is not None
        else _default_company_and_home_workspaces(mod.NEW_USER_ID)
    )

    cur = create_autospec(_CursorProto, instance=True)
    tx = create_autospec(_TxProto, instance=True)
    conn = create_autospec(_ConnProto, instance=True)

    state: dict[str, Any] = {"one": None, "all": [], "rowcount": 0}

    updated_tables: set[str] = set()

    def _execute(query: str, params: Any = None) -> None:
        q = " ".join(query.split())
        executed.append((q, params))
        state["one"] = None
        state["all"] = []
        state["rowcount"] = 0

        if q.startswith("SELECT 1 FROM pg_trigger"):
            state["one"] = None if overrides.get("MISSING_TRIGGER") == params[1] else {"?column?": 1}
        elif "SELECT current_database()" in q:
            state["one"] = {"db_name": db_name}
        elif q.startswith("SELECT * FROM users WHERE user_id = %(new_user)s"):
            state["one"] = dict(target_user) if target_user is not None else None
        elif q.startswith("SELECT * FROM users WHERE user_id = %(old_user)s"):
            state["one"] = dict(src_user) if src_user is not None else None
        elif "FROM users WHERE LOWER(email) = LOWER(%(owner_email)s)" in q:
            state["one"] = dict(email_conflict_user) if email_conflict_user is not None else None
        elif q.startswith("INSERT INTO users"):
            state["rowcount"] = 1
        elif q.startswith("DELETE FROM users WHERE user_id = %(old_user)s"):
            state["rowcount"] = overrides.get("DELETE_USERS", 1)
        elif q.startswith("ALTER TABLE "):
            state["rowcount"] = 0
        elif "FROM workspaces WHERE user_id = %(new_user)s AND status <> 'deleted'" in q:
            state["all"] = [dict(r) for r in final_ws]
        elif q.startswith("SELECT ") and " FROM " in q:
            # Match target table from FROM <table_name>; once updated, nothing references the old id.
            after_from = q.split(" FROM ", 1)[1]
            tbl = after_from.split()[0]
            state["all"] = [] if tbl in updated_tables else [dict(r) for r in rows_map.get(tbl, [])]
        elif q.startswith("UPDATE "):
            tbl = q.split()[1]
            expected = len(rows_map.get(tbl, []))
            state["rowcount"] = overrides.get(tbl, expected)
            if tbl not in overrides.get("LEFTOVER_TABLES", ()):
                updated_tables.add(tbl)

        cur.rowcount = state["rowcount"]

    cur.execute.side_effect = _execute
    cur.fetchone.side_effect = lambda: state["one"]
    cur.fetchall.side_effect = lambda: list(state["all"])
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False

    def _tx_enter() -> _TxProto:
        tx_state["entered"] += 1
        return tx

    def _tx_exit(exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            tx_state["committed"] += 1
        else:
            tx_state["rolled_back"] += 1
        return False

    tx.__enter__.side_effect = _tx_enter
    tx.__exit__.side_effect = _tx_exit

    conn.cursor.return_value = cur
    conn.transaction.return_value = tx

    return conn, executed, tx_state


def test_dry_run_does_not_write_or_create_snapshot(tmp_path: Path) -> None:
    conn, executed, tx_state = _make_db_harness()

    report = mod.run_migration(
        conn,  # type: ignore[arg-type]
        apply=False,
        snapshot_dir=tmp_path,
    )

    assert report["mode"] == "dry_run"
    assert report["snapshot_path"] is None
    assert report["counts"]["users.user_id"] == 1
    assert report["counts"]["workspaces.user_id"] == 5
    assert report["counts"]["computers.user_id"] == 1
    assert report["counts"]["dalton_verdicts.user_id"] == 2
    assert report["counts"]["store.prefix"] == 1
    assert report["counts"]["checkpoints.metadata.user_id"] == 1

    # No transaction opened, no snapshot written, no mutating SQL executed
    conn.transaction.assert_not_called()
    assert tx_state["entered"] == 0
    assert list(tmp_path.iterdir()) == []
    assert not any(
        q.startswith(("INSERT ", "UPDATE ", "DELETE ", "ALTER "))
        for q, _ in executed
    )


def test_apply_writes_0600_snapshot_and_commits_single_transaction(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn, executed, tx_state = _make_db_harness()

    report = mod.run_migration(
        conn,  # type: ignore[arg-type]
        apply=True,
        snapshot_dir=tmp_path,
    )

    assert report["mode"] == "apply"
    assert report["snapshot_path"] is not None
    snap_file = Path(report["snapshot_path"])
    assert snap_file.is_file()
    assert stat.S_IMODE(snap_file.stat().st_mode) == 0o600

    snap_data = json.loads(snap_file.read_text(encoding="utf-8"))
    assert snap_data["old_user"] == mod.OLD_USER_ID
    assert snap_data["new_user"] == mod.NEW_USER_ID
    assert snap_data["owner_email"] == mod.OWNER_EMAIL
    assert "users.user_id" in snap_data["rows"]
    assert "workspaces.user_id" in snap_data["rows"]

    assert tx_state == {"entered": 1, "committed": 1, "rolled_back": 0}

    sql_list = [q for q, _ in executed]
    insert_idx = next(i for i, q in enumerate(sql_list) if q.startswith("INSERT INTO users"))
    update_ws_idx = next(i for i, q in enumerate(sql_list) if q.startswith("UPDATE workspaces"))
    disable_verdict_idx = next(
        i
        for i, q in enumerate(sql_list)
        if q == "ALTER TABLE dalton_verdicts DISABLE TRIGGER trg_dalton_verdicts_immutable"
    )
    update_verdict_idx = next(
        i for i, q in enumerate(sql_list) if q.startswith("UPDATE dalton_verdicts")
    )
    enable_verdict_idx = next(
        i
        for i, q in enumerate(sql_list)
        if q == "ALTER TABLE dalton_verdicts ENABLE TRIGGER trg_dalton_verdicts_immutable"
    )
    delete_user_idx = next(
        i for i, q in enumerate(sql_list) if q.startswith("DELETE FROM users")
    )

    assert insert_idx < update_ws_idx < delete_user_idx
    assert disable_verdict_idx < update_verdict_idx < enable_verdict_idx < delete_user_idx
    assert report["post_check"]["home_workspace_id"] == mod.OLD_HOME_WORKSPACE_ID

    expected_alias_line = (
        "DALTON_HOME_WORKSPACE_ALIASES="
        "991a99ad-8ff2-4bd8-bf08-e5963a248798:6f18b8ab-3e7d-5ac8-a0e5-fbea3a0938ce"
    )
    assert report["required_env"] == expected_alias_line
    captured = capsys.readouterr().out
    assert expected_alias_line in captured


def test_row_count_mismatch_rolls_back_transaction(tmp_path: Path) -> None:
    conn, _executed, tx_state = _make_db_harness(
        update_rowcount_overrides={"workspaces": 4},  # dry-run saw 5, UPDATE affected 4
    )

    with pytest.raises(RuntimeError, match="Row count mismatch on workspaces.user_id"):
        mod.run_migration(
            conn,  # type: ignore[arg-type]
            apply=True,
            snapshot_dir=tmp_path,
        )

    assert tx_state == {"entered": 1, "committed": 0, "rolled_back": 1}


def test_refuses_when_target_user_already_exists(tmp_path: Path) -> None:
    conn, executed, tx_state = _make_db_harness(
        target_user={
            "user_id": mod.NEW_USER_ID,
            "email": mod.OWNER_EMAIL,
        },
    )

    with pytest.raises(RuntimeError, match="already exists in users table"):
        mod.run_migration(
            conn,  # type: ignore[arg-type]
            apply=True,
            snapshot_dir=tmp_path,
        )

    conn.transaction.assert_not_called()
    assert tx_state["entered"] == 0
    assert list(tmp_path.iterdir()) == []
    assert not any(
        q.startswith(("INSERT ", "UPDATE ", "DELETE "))
        for q, _ in executed
    )


def test_post_migration_missing_company_workspace_rolls_back(tmp_path: Path) -> None:
    incomplete_ws = [
        r
        for r in _default_company_and_home_workspaces(mod.NEW_USER_ID)
        if r["workspace_id"] != KNOWN_MIGRATED_WORKSPACES["MSFT"]
    ]
    conn, _executed, tx_state = _make_db_harness(
        post_check_workspaces=incomplete_ws,
    )

    with pytest.raises(RuntimeError, match="missing or unowned company workspaces"):
        mod.run_migration(
            conn,  # type: ignore[arg-type]
            apply=True,
            snapshot_dir=tmp_path,
        )

    assert tx_state == {"entered": 1, "committed": 0, "rolled_back": 1}


def test_verify_identity_pair_and_db_target_guards() -> None:
    with pytest.raises(RuntimeError, match="Identity pair mismatch"):
        mod.verify_identity_pair("other-user", mod.NEW_USER_ID, mod.OWNER_EMAIL)

    with pytest.raises(RuntimeError, match="Identity pair mismatch"):
        mod.verify_identity_pair(mod.OLD_USER_ID, "00000000-0000-0000-0000-000000000000")

    with pytest.raises(RuntimeError, match="Owner email mismatch"):
        mod.verify_identity_pair(mod.OLD_USER_ID, mod.NEW_USER_ID, "wrong@example.com")

    conn, _executed, _tx_state = _make_db_harness(db_name="langalpha")

    with pytest.raises(RuntimeError, match="Refusing --apply without explicit"):
        mod.verify_db_target(
            conn,  # type: ignore[arg-type]
            configured_host="localhost",
            expected_host=None,
            expected_db="langalpha",
            require_explicit=True,
        )

    with pytest.raises(RuntimeError, match="DB host mismatch"):
        mod.verify_db_target(
            conn,  # type: ignore[arg-type]
            configured_host="prod-db",
            expected_host="localhost",
            expected_db="langalpha",
            require_explicit=True,
        )

    with pytest.raises(RuntimeError, match="DB name mismatch"):
        mod.verify_db_target(
            conn,  # type: ignore[arg-type]
            configured_host="localhost",
            expected_host="localhost",
            expected_db="wrong_db",
            require_explicit=True,
        )


def test_post_migration_wrong_home_id_or_duplicate_home_rolls_back(tmp_path: Path) -> None:
    # 1. Single Home row but with wrong ID (e.g. derived fe5ee1cc-... instead of 6f18b8ab-...)
    wrong_id_ws = [
        {
            **r,
            "workspace_id": (
                mod.NEW_HOME_WORKSPACE_ID
                if r["workspace_id"] == mod.OLD_HOME_WORKSPACE_ID
                else r["workspace_id"]
            ),
        }
        for r in _default_company_and_home_workspaces(mod.NEW_USER_ID)
    ]
    conn_wrong, _executed, tx_wrong = _make_db_harness(post_check_workspaces=wrong_id_ws)
    with pytest.raises(
        RuntimeError,
        match=f"expected Home workspace for {mod.NEW_USER_ID} to have workspace_id={mod.OLD_HOME_WORKSPACE_ID!r}",
    ):
        mod.run_migration(
            conn_wrong,  # type: ignore[arg-type]
            apply=True,
            snapshot_dir=tmp_path,
        )
    assert tx_wrong == {"entered": 1, "committed": 0, "rolled_back": 1}

    # 2. Duplicate Home rows (both 6f18b8ab-... and fe5ee1cc-...)
    dup_ws = _default_company_and_home_workspaces(mod.NEW_USER_ID) + [
        {
            "workspace_id": mod.NEW_HOME_WORKSPACE_ID,
            "user_id": mod.NEW_USER_ID,
            "name": "Home",
            "dir_name": None,
            "status": "unbound",
        }
    ]
    conn_dup, _executed, tx_dup = _make_db_harness(post_check_workspaces=dup_ws)
    with pytest.raises(RuntimeError, match="expected exactly 1 live Home workspace"):
        mod.run_migration(
            conn_dup,  # type: ignore[arg-type]
            apply=True,
            snapshot_dir=tmp_path,
        )
    assert tx_dup == {"entered": 1, "committed": 0, "rolled_back": 1}


def test_home_workspace_aliases_hit_miss_and_is_home_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_user = "user-other-2"
    second_alias_ws = "11111111-2222-3333-4444-555555555555"

    # Unset -> standard uuid5 derivation
    monkeypatch.delenv("DALTON_HOME_WORKSPACE_ALIASES", raising=False)
    get_home_workspace_aliases.cache_clear()
    assert get_home_workspace_id(mod.NEW_USER_ID) == mod.NEW_HOME_WORKSPACE_ID
    assert is_home_id(mod.NEW_USER_ID, mod.OLD_HOME_WORKSPACE_ID) is False
    assert is_home_id(mod.NEW_USER_ID, mod.NEW_HOME_WORKSPACE_ID) is True

    # Set alias -> hit returns mapped old ID (normalized lowercase), miss falls back to uuid5
    monkeypatch.setenv(
        "DALTON_HOME_WORKSPACE_ALIASES",
        f" {mod.NEW_USER_ID} : {mod.OLD_HOME_WORKSPACE_ID.upper()} , {second_user}:{second_alias_ws} ",
    )
    get_home_workspace_aliases.cache_clear()

    assert get_home_workspace_id(mod.NEW_USER_ID) == mod.OLD_HOME_WORKSPACE_ID
    assert get_home_workspace_id(second_user) == second_alias_ws

    unmapped_user = "unmapped-user-99"
    assert get_home_workspace_id(unmapped_user) == str(
        uuid.uuid5(HOME_WORKSPACE_NAMESPACE, unmapped_user)
    )

    # is_home_id and is_home_row honor the alias
    assert is_home_id(mod.NEW_USER_ID, mod.OLD_HOME_WORKSPACE_ID) is True
    assert is_home_id(mod.NEW_USER_ID, mod.OLD_HOME_WORKSPACE_ID.upper()) is True
    assert is_home_id(mod.NEW_USER_ID, uuid.UUID(mod.OLD_HOME_WORKSPACE_ID)) is True
    assert is_home_id(mod.NEW_USER_ID, mod.NEW_HOME_WORKSPACE_ID) is False
    assert (
        is_home_row(
            {
                "workspace_id": mod.OLD_HOME_WORKSPACE_ID,
                "user_id": mod.NEW_USER_ID,
                "status": "stopped",
            }
        )
        is True
    )
    assert (
        is_home_row(
            {
                "workspace_id": mod.NEW_HOME_WORKSPACE_ID,
                "user_id": mod.NEW_USER_ID,
                "status": "stopped",
            }
        )
        is False
    )

    # realrun provenance helper also goes through the hook
    monkeypatch.setenv(
        "DALTON_HOME_WORKSPACE_ALIASES",
        f"realrun-aliased:{mod.OLD_HOME_WORKSPACE_ID}",
    )
    get_home_workspace_aliases.cache_clear()
    ok, reason = is_valid_realrun_workspace_row(
        {
            "workspace_id": mod.OLD_HOME_WORKSPACE_ID,
            "user_id": "realrun-aliased",
            "name": "Home",
            "config": {},
        }
    )
    assert (ok, reason) == (True, "deterministic_user_home")


def test_home_workspace_aliases_malformed_entries_logged_and_ignored(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(
        "DALTON_HOME_WORKSPACE_ALIASES",
        (
            "missing-colon-entry, "
            "too:many:colons, "
            ":6f18b8ab-3e7d-5ac8-a0e5-fbea3a0938ce, "
            "empty-ws:, "
            "bad-uuid-user:not-a-valid-uuid, "
            f"{mod.NEW_USER_ID}:{mod.OLD_HOME_WORKSPACE_ID}"
        ),
    )
    get_home_workspace_aliases.cache_clear()

    with caplog.at_level(logging.WARNING, logger="src.dalton.settings"):
        aliases = get_home_workspace_aliases()

    # Valid entry still succeeds while all 5 malformed entries were ignored with warnings
    assert aliases == {mod.NEW_USER_ID: mod.OLD_HOME_WORKSPACE_ID}
    assert get_home_workspace_id(mod.NEW_USER_ID) == mod.OLD_HOME_WORKSPACE_ID
    warning_messages = [
        rec.getMessage() for rec in caplog.records if rec.levelno == logging.WARNING
    ]
    assert len(warning_messages) == 5
    assert all("DALTON_HOME_WORKSPACE_ALIASES" in msg for msg in warning_messages)


def test_rows_still_carrying_old_identity_after_update_roll_back(tmp_path: Path) -> None:
    # A row written between the count and the update stays on the old id: refuse to commit.
    conn, _executed, tx_state = _make_db_harness(
        update_rowcount_overrides={"LEFTOVER_TABLES": ("workspaces",)},  # type: ignore[dict-item]
    )

    with pytest.raises(RuntimeError, match="still reference local-dev-user"):
        mod.run_migration(
            conn,  # type: ignore[arg-type]
            apply=True,
            snapshot_dir=tmp_path,
        )

    assert tx_state == {"entered": 1, "committed": 0, "rolled_back": 1}
