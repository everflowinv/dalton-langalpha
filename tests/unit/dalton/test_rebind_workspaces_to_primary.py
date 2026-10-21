"""Unit tests for `scripts/dalton/rebind_workspaces_to_primary.py` guard and rebind contracts."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

from scripts.dalton import rebind_workspaces_to_primary as rebind_mod
from scripts.dalton.realrun._common import KNOWN_MIGRATED_WORKSPACES
from src.server.database.workspace import WorkspaceDirNameTaken

GOOGL_Computer_ID = "11111111-1111-4111-8111-111111111111"
AMZN_COMPUTER_ID = "22222222-2222-4222-8222-222222222222"
HOME_WORKSPACE_ID = "f9f26e95-5e6c-552e-9902-7723e90ca302"

_ORIG_FUNCS: dict[str, Any] = {
    name: getattr(rebind_mod, name)
    for name in (
        "get_db_connection",
        "folder_allocation",
        "get_computers_for_user",
        "get_primary_computer",
        "get_computer",
        "get_workspace",
        "computer_run_in_progress",
        "workspace_has_live_runs",
        "get_live_workspace_ids_for_computer",
        "get_workspace_dir_names_for_computer",
        "bind_workspace_to_computer",
        "set_files_restore_incomplete",
        "count_live_workspaces_by_computer",
        "get_files_for_workspace",
        "list_all_latest_for_workspace",
    )
}


class _ConnInfoProto:
    host: str
    port: int
    dbname: str


class _CursorProto:
    connection: Any

    async def __aenter__(self) -> Any: ...
    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> Any: ...
    async def execute(self, query: Any, params: Any = None) -> Any: ...
    async def fetchone(self) -> Any: ...
    async def fetchall(self) -> Any: ...


class _TxProto:
    async def __aenter__(self) -> Any: ...
    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> Any: ...


class _ConnProto:
    info: Any

    async def __aenter__(self) -> Any: ...
    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> Any: ...
    def cursor(self, *args: Any, **kwargs: Any) -> Any: ...
    def transaction(self) -> Any: ...


def _make_primary_computer(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "computer_id": rebind_mod.PRIMARY_COMPUTER_ID,
        "user_id": rebind_mod.OWNER_USER_ID,
        "kind": "daytona",
        "name": "META-P0H-Verify",
        "is_primary": True,
        "status": "stopped",
        "provider_ref": "sbx-primary-001",
        "resource_tier": "standard",
        "is_always_on": False,
        "platform_secret_version": 3,
        "layout_version": 4,
        "spec_change": None,
        "spec_change_stale": False,
    }
    base.update(overrides)
    return base


def _make_old_computer(computer_id: str, name: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "computer_id": computer_id,
        "user_id": rebind_mod.OWNER_USER_ID,
        "kind": "daytona",
        "name": name,
        "is_primary": False,
        "status": "stopped",
        "provider_ref": f"sbx-{name.lower()}",
        "resource_tier": "standard",
        "is_always_on": False,
        "platform_secret_version": 1,
        "layout_version": 4,
        "spec_change": None,
        "spec_change_stale": False,
    }
    base.update(overrides)
    return base


def _make_workspaces_map() -> dict[str, dict[str, Any]]:
    meta_id = KNOWN_MIGRATED_WORKSPACES["META"]
    amzn_id = KNOWN_MIGRATED_WORKSPACES["AMZN"]
    googl_id = KNOWN_MIGRATED_WORKSPACES["GOOGL"]
    msft_id = KNOWN_MIGRATED_WORKSPACES["MSFT"]
    return {
        meta_id: {
            "workspace_id": meta_id,
            "user_id": rebind_mod.OWNER_USER_ID,
            "name": "META",
            "computer_id": None,
            "sandbox_id": None,
            "dir_name": None,
            "status": "stopped",
            "resource_tier": "standard",
            "is_always_on": False,
            "platform_secret_version": 0,
            "files_restore_incomplete": False,
            "config": {},
        },
        amzn_id: {
            "workspace_id": amzn_id,
            "user_id": rebind_mod.OWNER_USER_ID,
            "name": "AMZN",
            "computer_id": AMZN_COMPUTER_ID,
            "sandbox_id": "sbx-amzn",
            "dir_name": "AMZN",
            "status": "stopped",
            "resource_tier": "standard",
            "is_always_on": False,
            "platform_secret_version": 1,
            "files_restore_incomplete": False,
            "config": {},
        },
        googl_id: {
            "workspace_id": googl_id,
            "user_id": rebind_mod.OWNER_USER_ID,
            "name": "GOOGL",
            "computer_id": GOOGL_Computer_ID,
            "sandbox_id": "sbx-googl",
            "dir_name": "GOOGL",
            "status": "stopped",
            "resource_tier": "standard",
            "is_always_on": False,
            "platform_secret_version": 1,
            "files_restore_incomplete": False,
            "config": {},
        },
        msft_id: {
            "workspace_id": msft_id,
            "user_id": rebind_mod.OWNER_USER_ID,
            "name": "MSFT",
            "computer_id": None,
            "sandbox_id": None,
            "dir_name": None,
            "status": "stopped",
            "resource_tier": "standard",
            "is_always_on": False,
            "platform_secret_version": 0,
            "files_restore_incomplete": False,
            "config": {},
        },
    }


def _install_autospec_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    primary: dict[str, Any] | None = None,
    old_computers: dict[str, dict[str, Any]] | None = None,
    workspaces_map: dict[str, dict[str, Any]] | None = None,
    held_dirs_on_primary: tuple[str, ...] = ("Home", "early-test-1", "early-test-2"),
    live_ws_by_computer_before: dict[str, list[str]] | None = None,
    live_ws_by_computer_after: dict[str, list[str]] | None = None,
    remaining_counts_after: dict[str, int] | None = None,
    computer_busy_ids: set[str] | None = None,
    workspace_busy_ids: set[str] | None = None,
    knowledge_by_ws: dict[str, dict[str, dict[str, Any]]] | None = None,
    db_name_on_conn: str = "langalpha",
    db_host_on_conn: str = "127.0.0.1",
) -> dict[str, Any]:
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_NAME", "langalpha")
    monkeypatch.setenv("AUTH_USER_ID", rebind_mod.OWNER_USER_ID)

    primary_row = primary if primary is not None else _make_primary_computer()
    old_comp_map = (
        old_computers
        if old_computers is not None
        else {
            GOOGL_Computer_ID: _make_old_computer(GOOGL_Computer_ID, "GOOGL"),
            AMZN_COMPUTER_ID: _make_old_computer(AMZN_COMPUTER_ID, "AMZN"),
        }
    )
    ws_map = workspaces_map if workspaces_map is not None else _make_workspaces_map()

    default_before = {
        rebind_mod.PRIMARY_COMPUTER_ID: [HOME_WORKSPACE_ID],
        GOOGL_Computer_ID: [KNOWN_MIGRATED_WORKSPACES["GOOGL"]],
        AMZN_COMPUTER_ID: [KNOWN_MIGRATED_WORKSPACES["AMZN"]],
    }
    live_before = (
        live_ws_by_computer_before
        if live_ws_by_computer_before is not None
        else default_before
    )
    live_after = (
        live_ws_by_computer_after
        if live_ws_by_computer_after is not None
        else {
            rebind_mod.PRIMARY_COMPUTER_ID: [
                HOME_WORKSPACE_ID,
                *KNOWN_MIGRATED_WORKSPACES.values(),
            ],
            GOOGL_Computer_ID: [],
            AMZN_COMPUTER_ID: [],
        }
    )
    counts_after = (
        remaining_counts_after if remaining_counts_after is not None else {}
    )

    info = create_autospec(_ConnInfoProto, instance=True)
    info.host = db_host_on_conn
    info.port = 5432
    info.dbname = db_name_on_conn

    cur = create_autospec(_CursorProto, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None

    conn = create_autospec(_ConnProto, instance=True)
    conn.info = info
    conn.__aenter__.return_value = conn
    conn.__aexit__.return_value = None
    cur.connection = conn

    tx_state = {"committed": False, "rolled_back": False, "rebound_done": False}

    @asynccontextmanager
    async def _tx_cm():
        try:
            yield
        except BaseException:
            tx_state["rolled_back"] = True
            raise
        else:
            tx_state["committed"] = True

    conn.transaction.side_effect = _tx_cm
    conn.cursor.return_value = cur

    async def _cur_execute(sql: Any, params: Any = None) -> Any:
        return cur

    async def _cur_fetchone() -> dict[str, Any]:
        return {"dbname": db_name_on_conn, "port": 5432}

    cur.execute.side_effect = _cur_execute
    cur.fetchone.side_effect = _cur_fetchone

    @asynccontextmanager
    async def _fake_get_db_connection(c: Any = None):
        yield c if c is not None else conn

    @asynccontextmanager
    async def _fake_folder_allocation(computer_id: str, *, conn: Any = None):
        yield conn

    mock_get_db_connection = create_autospec(
        _ORIG_FUNCS["get_db_connection"], side_effect=_fake_get_db_connection
    )
    mock_folder_allocation = create_autospec(
        _ORIG_FUNCS["folder_allocation"], side_effect=_fake_folder_allocation
    )

    all_user_computers = [primary_row, *old_comp_map.values()]
    mock_get_computers_for_user = create_autospec(
        _ORIG_FUNCS["get_computers_for_user"],
        side_effect=lambda user_id, *, include_deleted=False, conn=None: list(
            all_user_computers
        ),
    )
    mock_get_primary_computer = create_autospec(
        _ORIG_FUNCS["get_primary_computer"],
        side_effect=lambda user_id, *, conn=None: primary_row,
    )
    mock_get_computer = create_autospec(
        _ORIG_FUNCS["get_computer"],
        side_effect=lambda cid, *, conn=None: old_comp_map.get(str(cid)),
    )
    mock_get_workspace = create_autospec(
        _ORIG_FUNCS["get_workspace"],
        side_effect=lambda wid, conn=None: ws_map.get(str(wid)),
    )

    busy_comps = computer_busy_ids or set()
    mock_computer_run_in_progress = create_autospec(
        _ORIG_FUNCS["computer_run_in_progress"],
        side_effect=lambda db, cid, *, own_run_id=None: str(cid) in busy_comps,
    )

    busy_ws = workspace_busy_ids or set()
    mock_workspace_has_live_runs = create_autospec(
        _ORIG_FUNCS["workspace_has_live_runs"],
        side_effect=lambda c, wid: str(wid) in busy_ws,
    )

    async def _live_ws_for_computer(cid: str, *, conn: Any = None) -> list[str]:
        table = live_after if tx_state["rebound_done"] else live_before
        return list(table.get(str(cid), []))

    mock_get_live_workspace_ids = create_autospec(
        _ORIG_FUNCS["get_live_workspace_ids_for_computer"],
        side_effect=_live_ws_for_computer,
    )
    mock_get_workspace_dir_names = create_autospec(
        _ORIG_FUNCS["get_workspace_dir_names_for_computer"],
        side_effect=lambda cid, *, conn=None: tuple(held_dirs_on_primary),
    )

    async def _bind_ws(
        workspace_id: str,
        computer_id: str,
        *,
        expected_computer_id: str | None,
        dir_name: str | None = None,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        row = dict(ws_map[workspace_id])
        row.update(
            {
                "computer_id": computer_id,
                "dir_name": dir_name,
                "status": primary_row["status"],
                "sandbox_id": primary_row["provider_ref"],
                "resource_tier": primary_row["resource_tier"],
                "is_always_on": primary_row["is_always_on"],
                "platform_secret_version": primary_row["platform_secret_version"],
            }
        )
        if len(mock_bind.call_args_list) >= len(ws_map):
            tx_state["rebound_done"] = True
        return row

    mock_bind = create_autospec(
        _ORIG_FUNCS["bind_workspace_to_computer"],
        side_effect=_bind_ws,
    )
    mock_set_restore_incomplete = create_autospec(
        _ORIG_FUNCS["set_files_restore_incomplete"],
        return_value=True,
    )
    mock_count_live = create_autospec(
        _ORIG_FUNCS["count_live_workspaces_by_computer"],
        side_effect=lambda cids, *, conn=None: dict(counts_after),
    )
    mock_get_files = create_autospec(
        _ORIG_FUNCS["get_files_for_workspace"],
        side_effect=lambda wid, **kwargs: [{"file_path": "agent.md", "kind": "file"}],
    )
    kv_map = knowledge_by_ws or {}
    mock_list_latest_kv = create_autospec(
        _ORIG_FUNCS["list_all_latest_for_workspace"],
        side_effect=lambda wid, *, valid_only=True: dict(kv_map.get(str(wid), {})),
    )

    monkeypatch.setattr(rebind_mod, "get_db_connection", mock_get_db_connection)
    monkeypatch.setattr(rebind_mod, "folder_allocation", mock_folder_allocation)
    monkeypatch.setattr(
        rebind_mod, "get_computers_for_user", mock_get_computers_for_user
    )
    monkeypatch.setattr(rebind_mod, "get_primary_computer", mock_get_primary_computer)
    monkeypatch.setattr(rebind_mod, "get_computer", mock_get_computer)
    monkeypatch.setattr(rebind_mod, "get_workspace", mock_get_workspace)
    monkeypatch.setattr(
        rebind_mod, "computer_run_in_progress", mock_computer_run_in_progress
    )
    monkeypatch.setattr(
        rebind_mod, "workspace_has_live_runs", mock_workspace_has_live_runs
    )
    monkeypatch.setattr(
        rebind_mod,
        "get_live_workspace_ids_for_computer",
        mock_get_live_workspace_ids,
    )
    monkeypatch.setattr(
        rebind_mod,
        "get_workspace_dir_names_for_computer",
        mock_get_workspace_dir_names,
    )
    monkeypatch.setattr(rebind_mod, "bind_workspace_to_computer", mock_bind)
    monkeypatch.setattr(
        rebind_mod, "set_files_restore_incomplete", mock_set_restore_incomplete
    )
    monkeypatch.setattr(
        rebind_mod, "count_live_workspaces_by_computer", mock_count_live
    )
    monkeypatch.setattr(rebind_mod, "get_files_for_workspace", mock_get_files)
    monkeypatch.setattr(
        rebind_mod, "list_all_latest_for_workspace", mock_list_latest_kv
    )

    return {
        "tx_state": tx_state,
        "mock_bind": mock_bind,
        "mock_set_restore_incomplete": mock_set_restore_incomplete,
        "mock_count_live": mock_count_live,
    }


@pytest.mark.asyncio
async def test_rebind_refuses_non_whitelisted_ids_and_wrong_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard 1: Refuse any non-whitelisted workspace ID, partial list, extra ID, wrong user, or mismatched DB."""
    _install_autospec_harness(monkeypatch)

    # Replace one known ID with a random non-whitelisted UUID
    bad_ids = [
        KNOWN_MIGRATED_WORKSPACES["META"],
        KNOWN_MIGRATED_WORKSPACES["AMZN"],
        KNOWN_MIGRATED_WORKSPACES["GOOGL"],
        "99999999-9999-4999-8999-999999999999",
    ]
    with pytest.raises(SystemExit, match=r"REFUSED:.*KNOWN_MIGRATED_WORKSPACES"):
        await rebind_mod.rebind_workspaces_to_primary(
            workspaces_arg=",".join(bad_ids),
        )

    # Missing one of the 4 IDs
    three_ids = list(KNOWN_MIGRATED_WORKSPACES.values())[:3]
    with pytest.raises(SystemExit, match=r"REFUSED:.*KNOWN_MIGRATED_WORKSPACES"):
        await rebind_mod.rebind_workspaces_to_primary(
            workspaces_arg=",".join(three_ids),
        )

    # Extra 5th ID
    five_ids = [
        *KNOWN_MIGRATED_WORKSPACES.values(),
        "99999999-9999-4999-8999-999999999999",
    ]
    with pytest.raises(SystemExit, match=r"REFUSED:.*KNOWN_MIGRATED_WORKSPACES"):
        await rebind_mod.rebind_workspaces_to_primary(
            workspaces_arg=",".join(five_ids),
        )

    # Non-owner user
    with pytest.raises(SystemExit, match=r"REFUSED:.*user must be 'local-dev-user'"):
        await rebind_mod.rebind_workspaces_to_primary(user="another-user")

    # Connected database mismatch
    _install_autospec_harness(monkeypatch, db_name_on_conn="wrong_db")
    with pytest.raises(
        SystemExit, match=r"REFUSED:.*connected database 'wrong_db' does not match"
    ):
        await rebind_mod.rebind_workspaces_to_primary()


@pytest.mark.asyncio
async def test_rebind_refuses_when_computers_not_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard 2: Refuse when primary computer or any old computer is not 'stopped' or has active spec_change."""
    # Primary computer running
    _install_autospec_harness(
        monkeypatch,
        primary=_make_primary_computer(status="running"),
    )
    with pytest.raises(
        SystemExit, match=r"REFUSED:.*primary computer .* must be 'stopped'"
    ):
        await rebind_mod.rebind_workspaces_to_primary(apply=True)

    # Primary computer has active spec_change
    _install_autospec_harness(
        monkeypatch,
        primary=_make_primary_computer(spec_change={"state": "in_progress"}),
    )
    with pytest.raises(SystemExit, match=r"REFUSED:.*spec_change"):
        await rebind_mod.rebind_workspaces_to_primary(apply=True)

    # Old computer (GOOGL) is running
    _install_autospec_harness(
        monkeypatch,
        old_computers={
            GOOGL_Computer_ID: _make_old_computer(
                GOOGL_Computer_ID, "GOOGL", status="running"
            ),
            AMZN_COMPUTER_ID: _make_old_computer(AMZN_COMPUTER_ID, "AMZN"),
        },
    )
    with pytest.raises(
        SystemExit, match=r"REFUSED:.*old computer .* must be 'stopped'"
    ):
        await rebind_mod.rebind_workspaces_to_primary(apply=True)


@pytest.mark.asyncio
async def test_rebind_refuses_when_runs_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard 3: Refuse when computer_run_in_progress or workspace_has_live_runs reports active work."""
    # Active run / subagent_run on primary computer
    _install_autospec_harness(
        monkeypatch,
        computer_busy_ids={rebind_mod.PRIMARY_COMPUTER_ID},
    )
    with pytest.raises(
        SystemExit, match=r"REFUSED:.*computer .* has a run or subagent_run in progress"
    ):
        await rebind_mod.rebind_workspaces_to_primary(apply=True)

    # Active run / subagent_run on old computer about to be vacated
    _install_autospec_harness(
        monkeypatch,
        computer_busy_ids={GOOGL_Computer_ID},
    )
    with pytest.raises(
        SystemExit, match=r"REFUSED:.*computer .* has a run or subagent_run in progress"
    ):
        await rebind_mod.rebind_workspaces_to_primary(apply=True)

    # Active run / subagent_run on an unbound target workspace (META)
    _install_autospec_harness(
        monkeypatch,
        workspace_busy_ids={KNOWN_MIGRATED_WORKSPACES["META"]},
    )
    with pytest.raises(
        SystemExit,
        match=r"REFUSED:.*workspace .* has a run or subagent_run in progress",
    ):
        await rebind_mod.rebind_workspaces_to_primary(apply=True)

    # Active run / subagent_run on a sibling workspace on primary (Home)
    _install_autospec_harness(
        monkeypatch,
        workspace_busy_ids={HOME_WORKSPACE_ID},
    )
    with pytest.raises(
        SystemExit,
        match=r"REFUSED:.*workspace .* has a run or subagent_run in progress",
    ):
        await rebind_mod.rebind_workspaces_to_primary(apply=True)


@pytest.mark.asyncio
async def test_rebind_refuses_on_folder_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Guard 4: Refuse on casefold folder name collision or UniqueViolation / WorkspaceDirNameTaken."""
    # Case-insensitive ('meta' vs 'META') folder clash on primary computer
    harness = _install_autospec_harness(
        monkeypatch,
        held_dirs_on_primary=("Home", "meta"),
    )
    with pytest.raises(
        SystemExit,
        match=r"REFUSED:.*folder names already taken on primary computer \(casefold\): \['META'\]",
    ):
        await rebind_mod.rebind_workspaces_to_primary(
            snapshot_dir=tmp_path, apply=True
        )
    assert harness["mock_bind"].call_count == 0
    assert harness["tx_state"]["rolled_back"] is True

    # Unique constraint conflict raised by bind_workspace_to_computer converted to REFUSED
    harness2 = _install_autospec_harness(monkeypatch)
    harness2["mock_bind"].side_effect = WorkspaceDirNameTaken(
        KNOWN_MIGRATED_WORKSPACES["META"], rebind_mod.PRIMARY_COMPUTER_ID, "META"
    )
    with pytest.raises(
        SystemExit, match=r"REFUSED:.*unique constraint conflict during rebind"
    ):
        await rebind_mod.rebind_workspaces_to_primary(
            snapshot_dir=tmp_path, apply=True
        )
    assert harness2["tx_state"]["rolled_back"] is True
    assert harness2["tx_state"]["committed"] is False


@pytest.mark.asyncio
async def test_rebind_refuses_when_old_computer_not_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Guard 5: Refuse and roll back if an old computer is not empty before or after rebind."""
    # Case A: Pre-check detects another live workspace on GOOGL's old computer
    harness_pre = _install_autospec_harness(
        monkeypatch,
        live_ws_by_computer_before={
            rebind_mod.PRIMARY_COMPUTER_ID: [HOME_WORKSPACE_ID],
            GOOGL_Computer_ID: [
                KNOWN_MIGRATED_WORKSPACES["GOOGL"],
                "77777777-7777-4777-8777-777777777777",
            ],
            AMZN_COMPUTER_ID: [KNOWN_MIGRATED_WORKSPACES["AMZN"]],
        },
    )
    with pytest.raises(
        SystemExit,
        match=r"REFUSED:.*old computer .* carries other live workspace\(s\)",
    ):
        await rebind_mod.rebind_workspaces_to_primary(
            snapshot_dir=tmp_path, apply=True
        )
    assert harness_pre["mock_bind"].call_count == 0
    assert harness_pre["tx_state"]["rolled_back"] is True

    # Case B: Post-update check inside transaction finds remaining live workspace on old computer
    harness_post = _install_autospec_harness(
        monkeypatch,
        remaining_counts_after={GOOGL_Computer_ID: 1},
    )
    with pytest.raises(
        SystemExit,
        match=r"REFUSED:.*vacated computer\(s\) still have live workspaces after rebind",
    ):
        await rebind_mod.rebind_workspaces_to_primary(
            snapshot_dir=tmp_path, apply=True
        )
    assert harness_post["tx_state"]["rolled_back"] is True
    assert harness_post["tx_state"]["committed"] is False
    out = capsys.readouterr().out
    assert "APPLIED:" not in out


@pytest.mark.asyncio
async def test_rebind_dry_run_and_apply_with_knowledge_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify dry-run vs --apply behavior, knowledge files_written_at warning, snapshot suffix, and post-commit APPLIED."""
    meta_id = KNOWN_MIGRATED_WORKSPACES["META"]
    harness = _install_autospec_harness(
        monkeypatch,
        knowledge_by_ws={
            meta_id: {
                "thesis": {
                    "doc_key": "thesis",
                    "version_no": 2,
                    "files_written_at": "2026-10-20T12:00:00Z",
                }
            }
        },
    )

    # 1. Dry-run: prints plan + warning, does not call bind_workspace_to_computer
    plan = await rebind_mod.rebind_workspaces_to_primary(
        snapshot_dir=tmp_path,
        apply=False,
    )
    out_dry = capsys.readouterr().out
    assert "DRY RUN: nothing written." in out_dry
    assert "WARNING: workspace" in out_dry
    assert "apply_pending skips versions with files_written_at set" in out_dry
    assert len(plan["knowledge_files_written_warnings"]) == 1
    assert plan["database"]["dbname"] == "langalpha"
    assert harness["mock_bind"].call_count == 0
    assert list(tmp_path.glob("rebind-workspaces-*.json")) == []

    # 2. Apply: writes snapshot with unique suffix, calls bind + set_files_restore_incomplete, commits, prints APPLIED
    harness_apply = _install_autospec_harness(monkeypatch)
    await rebind_mod.rebind_workspaces_to_primary(
        snapshot_dir=tmp_path,
        apply=True,
    )
    out_apply = capsys.readouterr().out
    assert "APPLIED: 4 workspaces rebound" in out_apply
    assert harness_apply["mock_bind"].call_count == 4
    assert harness_apply["mock_set_restore_incomplete"].call_count == 4
    assert harness_apply["mock_count_live"].call_count == 1
    assert harness_apply["tx_state"]["committed"] is True
    assert harness_apply["tx_state"]["rolled_back"] is False

    snaps = list(tmp_path.glob("rebind-workspaces-*.json"))
    assert len(snaps) == 1
    snap_data = json.loads(snaps[0].read_text(encoding="utf-8"))
    assert snap_data["database"]["dbname"] == "langalpha"
    assert len(snap_data["before"]) == 4
