"""Bind the owner's four migrated company workspaces onto the primary computer.

Upstream keeps every workspace of a user on one machine (``duplicate_workspace`` lands
copies there, and Home's Chief of Staff reads sibling folders on it). The early Dalton
migration gave GOOGL and AMZN machines of their own and left META and MSFT unbound, so
Home cannot see them, and an unbound workspace would get yet another machine on first
start (``_adopt_workspace_onto_computer``). Duplicating is not an option: Dalton's
knowledge versions, verdicts and coverage are keyed by the original workspace ids.

Reuses upstream binding and folder-allocation primitives rather than hand-written SQL:
- ``get_or_create_pool`` / ``get_db_connection`` for pooled async connections
- ``get_computers_for_user`` / ``get_primary_computer`` / ``get_computer`` for computer state
- ``computer_run_in_progress`` / ``workspace_has_live_runs`` for live run + subagent_run checks
- ``folder_allocation`` / ``takes_name_folders`` / ``get_workspace_dir_names_for_computer`` /
  ``checked_workspace_name`` / ``workspace_folder_name`` / ``candidate_dir_names`` for folder locks,
  canonical folder spelling, and casefold collision checks
- ``bind_workspace_to_computer`` (which syncs shadow columns, strips ``config.folder_landings``,
  calls ``drop_workspace_links``, and calls ``release_former_folder``)
- ``set_files_restore_incomplete`` so a sibling backup on the primary machine cannot prune
  mirrored ``workspace_files`` rows before first restore
- ``count_live_workspaces_by_computer`` / ``get_live_workspace_ids_for_computer`` to assert in
  the same transaction that vacated computers have zero live workspaces remaining

Default is a dry run. ``--apply`` writes a uniquely named JSON snapshot before mutating,
rebinds in one transaction, verifies old computers are empty, and prints ``APPLIED`` only
after the transaction commits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.realrun._common import (
    KNOWN_MIGRATED_WORKSPACES,
    load_repo_dotenv,
    resolve_local_dev_user_id,
)
from src.dalton.database.knowledge import list_all_latest_for_workspace
from src.server.database.computer import (
    get_computer,
    get_computers_for_user,
    get_primary_computer,
)
from src.server.database.home_workspace import is_home_id, is_home_row
from src.server.database.pool import get_db_connection, get_or_create_pool
from src.server.database.runs.lifecycle import workspace_has_live_runs
from src.server.database.workspace import (
    WorkspaceDirNameTaken,
    bind_workspace_to_computer,
    count_live_workspaces_by_computer,
    get_live_workspace_ids_for_computer,
    get_workspace,
    get_workspace_dir_names_for_computer,
    set_files_restore_incomplete,
)
from src.server.database.workspace_file import get_files_for_workspace
from src.server.database.workspace_folders import (
    computer_run_in_progress,
    folder_allocation,
    takes_name_folders,
)
from src.server.database.workspace_names import (
    HOME_FOLDER,
    WorkspaceNameInvalid,
    candidate_dir_names,
    checked_workspace_name,
    workspace_folder_name,
)
from src.server.utils.pg_sanitize import normalize_uuid

OWNER_USER_ID = "local-dev-user"
PRIMARY_COMPUTER_ID = "afb3d862-09b1-4ca5-af04-d4ce2469d962"
WHITELISTED_WORKSPACE_IDS: tuple[str, ...] = tuple(
    str(normalize_uuid(ws_id)) for ws_id in KNOWN_MIGRATED_WORKSPACES.values()
)


def _refuse(msg: str) -> None:
    raise SystemExit(f"REFUSED: {msg}")


def _spec_change_in_progress(spec_change: Any) -> bool:
    """A settled change keeps its record with state succeeded/failed; only in_progress blocks."""
    if spec_change is None:
        return False
    if isinstance(spec_change, str):
        try:
            spec_change = json.loads(spec_change)
        except ValueError:
            return True
    if not isinstance(spec_change, dict):
        return True
    return spec_change.get("state") not in ("succeeded", "failed")


def _resolve_and_prepare_target_db(
    *,
    db_host: str | None = None,
    db_name: str | None = None,
) -> dict[str, Any]:
    """Resolve and validate target Postgres host/dbname from explicit args or environment."""
    env_url = (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or "").strip()
    parsed_host = ""
    parsed_port = ""
    parsed_db = ""
    if env_url:
        parsed = urlparse(env_url)
        parsed_host = (parsed.hostname or "").strip()
        parsed_port = str(parsed.port or "")
        parsed_db = (parsed.path or "").lstrip("/").strip()

    raw_host = (db_host or os.getenv("DB_HOST") or parsed_host or "").strip()
    raw_name = (db_name or os.getenv("DB_NAME") or parsed_db or "").strip()
    raw_port = (os.getenv("DB_PORT") or parsed_port or "5432").strip()

    if not raw_host or not raw_name:
        _refuse(
            "target database host/dbname must be explicitly configured "
            "(set DB_HOST and DB_NAME, or pass --db-host and --db-name)"
        )

    effective_host = raw_host
    if effective_host in ("postgres", "db", "host.docker.internal") and not os.path.exists(
        "/.dockerenv"
    ):
        effective_host = "127.0.0.1"
        os.environ["DB_HOST"] = effective_host
    else:
        os.environ.setdefault("DB_HOST", effective_host)
    os.environ.setdefault("DB_NAME", raw_name)
    os.environ.setdefault("DB_PORT", raw_port)

    return {
        "configured_host": raw_host,
        "host": effective_host,
        "port": int(raw_port) if raw_port.isdigit() else 5432,
        "dbname": raw_name,
    }


def _validate_inputs(
    *,
    user: str,
    workspaces_arg: str,
) -> list[str]:
    """Enforce owner identity and exact four-workspace whitelist."""
    cleaned_user = (user or "").strip()
    resolved_owner = resolve_local_dev_user_id()
    if cleaned_user != OWNER_USER_ID or resolved_owner != OWNER_USER_ID:
        _refuse(
            f"user must be {OWNER_USER_ID!r} (got --user={cleaned_user!r}, "
            f"AUTH_USER_ID={resolved_owner!r})"
        )

    raw_items = [w.strip() for w in (workspaces_arg or "").split(",") if w.strip()]
    if not raw_items:
        _refuse("no workspaces given")

    normalized: list[str] = []
    for item in raw_items:
        norm = normalize_uuid(item)
        if norm is None:
            _refuse(f"invalid workspace UUID: {item!r}")
        normalized.append(norm)

    if len(normalized) != len(set(normalized)):
        _refuse(f"duplicate workspace ids given: {normalized}")

    expected_set = set(WHITELISTED_WORKSPACE_IDS)
    given_set = set(normalized)
    if given_set != expected_set:
        extra = sorted(given_set - expected_set)
        missing = sorted(expected_set - given_set)
        _refuse(
            f"workspaces must be the exact 4 KNOWN_MIGRATED_WORKSPACES "
            f"(extra={extra}, missing={missing})"
        )

    return normalized


async def _verify_connected_database(
    cur: Any, expected_db: dict[str, Any]
) -> dict[str, Any]:
    """Verify the live connection's database name and host match the expected target."""
    await cur.execute(
        "SELECT current_database() AS dbname, inet_server_port() AS port"
    )
    row = await cur.fetchone() or {}
    actual_dbname = str(row.get("dbname") or "")
    conn_info = getattr(getattr(cur, "connection", None), "info", None)
    actual_host = str(getattr(conn_info, "host", "") or "").strip()
    if not actual_host:
        _refuse("cannot read the connected host from the live connection")
    actual_port = int(
        getattr(conn_info, "port", 0)
        or row.get("port")
        or expected_db["port"]
    )

    if actual_dbname != expected_db["dbname"]:
        _refuse(
            f"connected database {actual_dbname!r} does not match expected "
            f"dbname {expected_db['dbname']!r}"
        )

    local_aliases = {"127.0.0.1", "localhost", "::1"}
    expected_host = expected_db["host"]
    if expected_host in local_aliases:
        host_ok = actual_host in local_aliases
    else:
        host_ok = actual_host == expected_host
    if not host_ok:
        _refuse(
            f"connected host {actual_host!r} does not match expected "
            f"host {expected_host!r}"
        )

    return {
        "host": actual_host,
        "configured_host": expected_db["configured_host"],
        "port": actual_port,
        "dbname": actual_dbname,
    }


async def rebind_workspaces_to_primary(
    *,
    user: str = OWNER_USER_ID,
    workspaces_arg: str = ",".join(WHITELISTED_WORKSPACE_IDS),
    snapshot_dir: str | Path | None = None,
    apply: bool = False,
    expected_primary_computer_id: str | None = PRIMARY_COMPUTER_ID,
    db_host: str | None = None,
    db_name: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Validate guards and (when ``apply=True``) rebind the 4 company workspaces onto primary."""
    ws_ids = _validate_inputs(user=user, workspaces_arg=workspaces_arg)
    ws_id_set = set(ws_ids)
    expected_db = _resolve_and_prepare_target_db(db_host=db_host, db_name=db_name)

    snap_path: Path | None = None
    bound_rows: list[dict[str, Any]] = []

    try:
        async with get_db_connection(conn) as owned, owned.transaction():
            async with owned.cursor(row_factory=dict_row) as cur:
                db_info = await _verify_connected_database(cur, expected_db)

                user_computers = await get_computers_for_user(user, conn=owned)
                primaries = [c for c in user_computers if c.get("is_primary")]
                if len(primaries) != 1:
                    _refuse(
                        f"expected_primary_count=1 for {user}, found {len(primaries)}"
                    )

                # Lock computers first (ordered by computer_id), then workspaces (ordered by workspace_id).
                lock_computer_ids = sorted(
                    {str(c["computer_id"]) for c in user_computers if c.get("computer_id")}
                )
                if lock_computer_ids:
                    await cur.execute(
                        "SELECT computer_id FROM computers "
                        "WHERE computer_id = ANY(%s::uuid[]) "
                        "ORDER BY computer_id FOR UPDATE",
                        (lock_computer_ids,),
                    )
                # The targets plus every live sibling on those computers, so no turn can be
                # admitted on a sibling (admission locks the workspace row) after the run checks.
                await cur.execute(
                    "SELECT workspace_id FROM workspaces "
                    "WHERE (workspace_id = ANY(%s::uuid[]) "
                    "OR (computer_id = ANY(%s::uuid[]) AND status <> 'deleted')) "
                    "ORDER BY workspace_id FOR UPDATE",
                    (sorted(ws_ids), lock_computer_ids),
                )

                primary = await get_primary_computer(user, conn=owned)
                if primary is None:
                    _refuse(f"no primary computer found for {user}")
                primary_id = str(primary["computer_id"])

                if (
                    expected_primary_computer_id is not None
                    and primary_id != normalize_uuid(expected_primary_computer_id)
                ):
                    _refuse(
                        f"primary computer {primary_id} does not match expected "
                        f"{expected_primary_computer_id}"
                    )
                if primary.get("status") != "stopped":
                    _refuse(
                        f"primary computer {primary_id} must be 'stopped', "
                        f"got {primary.get('status')!r}"
                    )
                if not primary.get("provider_ref"):
                    _refuse(f"primary computer {primary_id} has empty provider_ref")
                if _spec_change_in_progress(primary.get("spec_change")):
                    _refuse(
                        f"primary computer {primary_id} has an in-progress "
                        f"spec_change: {primary.get('spec_change')}"
                    )
                if not takes_name_folders(primary):
                    _refuse(
                        f"primary computer {primary_id} (layout_version={primary.get('layout_version')!r}) "
                        "does not accept named workspace folders"
                    )

                rows: list[dict[str, Any]] = []
                for wid in ws_ids:
                    w_row = await get_workspace(wid, conn=owned)
                    if w_row is None:
                        _refuse(f"workspace {wid} not found or deleted")
                    rows.append(w_row)

                for r in rows:
                    wid = str(r["workspace_id"])
                    if str(r.get("user_id")) != user:
                        _refuse(f"workspace {wid} is not owned by {user}")
                    if (
                        is_home_id(user, wid)
                        or is_home_row(r)
                        or r.get("name") == HOME_FOLDER
                    ):
                        _refuse("Home is bound through ensure_home_bound, not this script")
                    if r.get("status") in ("deleted", "unbound"):
                        _refuse(f"workspace {wid} has invalid status {r.get('status')!r}")
                    if str(r.get("computer_id") or "") == primary_id:
                        _refuse(
                            f"workspace {wid} is already bound to primary computer {primary_id}"
                        )

                old_computers = sorted(
                    {
                        str(r["computer_id"])
                        for r in rows
                        if r.get("computer_id") and str(r["computer_id"]) != primary_id
                    }
                )
                old_computer_rows: dict[str, dict[str, Any]] = {}
                for old_cid in old_computers:
                    old_comp = await get_computer(old_cid, conn=owned)
                    if old_comp is None:
                        _refuse(f"old computer {old_cid} not found or deleted")
                    if str(old_comp.get("user_id")) != user:
                        _refuse(f"old computer {old_cid} is not owned by {user}")
                    if old_comp.get("status") != "stopped":
                        _refuse(
                            f"old computer {old_cid} must be 'stopped', "
                            f"got {old_comp.get('status')!r}"
                        )
                    if _spec_change_in_progress(old_comp.get("spec_change")):
                        _refuse(
                            f"old computer {old_cid} has an in-progress "
                            f"spec_change: {old_comp.get('spec_change')}"
                        )
                    old_computer_rows[old_cid] = old_comp

                # Check live runs across the primary computer, all old computers,
                # all live workspaces on the primary computer, and the 4 target workspaces.
                for cid in (primary_id, *old_computers):
                    if await computer_run_in_progress(cur, cid):
                        _refuse(
                            f"computer {cid} has a run or subagent_run in progress"
                        )

                primary_live_ws_ids = await get_live_workspace_ids_for_computer(
                    primary_id, conn=owned
                )
                all_checked_ws_ids = [
                    *ws_ids,
                    *(w for w in primary_live_ws_ids if w not in ws_id_set),
                ]
                for wid in all_checked_ws_ids:
                    if await workspace_has_live_runs(cur, wid):
                        _refuse(
                            f"workspace {wid} has a run or subagent_run in progress"
                        )

                # Pre-check that old computers do not carry any other live workspaces.
                for old_cid in old_computers:
                    live_on_old = await get_live_workspace_ids_for_computer(
                        old_cid, conn=owned
                    )
                    unexpected_survivors = [
                        w for w in live_on_old if w not in ws_id_set
                    ]
                    if unexpected_survivors:
                        _refuse(
                            f"old computer {old_cid} carries other live workspace(s) "
                            f"that would remain after rebind: {unexpected_survivors}"
                        )

            # Acquire the shared folder allocation lock on the primary computer.
            async with folder_allocation(primary_id, conn=owned) as alloc_conn:
                held_dirs = list(
                    await get_workspace_dir_names_for_computer(
                        primary_id, conn=alloc_conn
                    )
                )
                taken_folded = {d.casefold() for d in held_dirs if d}
                target_dirs: dict[str, str] = {}
                clashes: list[str] = []

                for r in rows:
                    wid = str(r["workspace_id"])
                    try:
                        clean_name = checked_workspace_name(r.get("name"))
                        folder = workspace_folder_name(clean_name)
                    except WorkspaceNameInvalid as exc:
                        _refuse(
                            f"workspace {wid} name {r.get('name')!r} is not a valid "
                            f"folder name: {exc}"
                        )
                    candidates = candidate_dir_names(
                        clean_name,
                        wid,
                        held_dirs,
                        own_folder=takes_name_folders(primary),
                    )
                    if (
                        folder.casefold() in taken_folded
                        or not candidates
                        or candidates[0] != folder
                    ):
                        clashes.append(folder)
                    taken_folded.add(folder.casefold())
                    held_dirs.append(folder)
                    target_dirs[wid] = folder

                if clashes:
                    _refuse(
                        f"folder names already taken on primary computer (casefold): {clashes}"
                    )

                # Read-only checks on workspace_files and dalton_knowledge_versions.files_written_at
                files_by_ws: dict[str, list[dict[str, Any]]] = {}
                knowledge_warnings: list[str] = []
                for r in rows:
                    wid = str(r["workspace_id"])
                    ws_files = await get_files_for_workspace(
                        wid,
                        include_content=False,
                        all_kinds=True,
                        conn=alloc_conn,
                    )
                    files_by_ws[wid] = ws_files
                    # A workspace that has had a sandbox but no mirror would arrive empty:
                    # the primary restores only from workspace_files.
                    if r.get("sandbox_id") and not ws_files:
                        _refuse(
                            f"workspace {wid} ({r.get('name')}) has sandbox "
                            f"{r.get('sandbox_id')!r} but an empty file mirror; back it up first"
                        )

                    latest_kv = await list_all_latest_for_workspace(
                        wid, valid_only=False
                    )
                    for doc_key, kv_row in sorted(latest_kv.items()):
                        written_at = (kv_row or {}).get("files_written_at")
                        if written_at is not None:
                            knowledge_warnings.append(
                                f"WARNING: workspace {wid} ({r.get('name')}) "
                                f"doc_key={doc_key!r} v{kv_row.get('version_no')} has "
                                f"files_written_at={written_at}; apply_pending skips "
                                "versions with files_written_at set, so its sandbox "
                                "files on the new computer must be rewritten."
                            )

                plan: dict[str, Any] = {
                    "database": db_info,
                    "user": user,
                    "primary_computer": {
                        "computer_id": primary_id,
                        "name": primary.get("name"),
                        "status": primary.get("status"),
                        "provider_ref": primary.get("provider_ref"),
                        "resource_tier": primary.get("resource_tier"),
                        "is_always_on": primary.get("is_always_on"),
                        "platform_secret_version": primary.get("platform_secret_version"),
                        "layout_version": primary.get("layout_version"),
                    },
                    "workspaces": [
                        {
                            "workspace_id": str(r["workspace_id"]),
                            "name": r.get("name"),
                            "from": {
                                "computer_id": str(r["computer_id"])
                                if r.get("computer_id")
                                else None,
                                "status": r.get("status"),
                                "sandbox_id": r.get("sandbox_id"),
                                "dir_name": r.get("dir_name"),
                                "resource_tier": r.get("resource_tier"),
                                "is_always_on": r.get("is_always_on"),
                                "platform_secret_version": r.get(
                                    "platform_secret_version"
                                ),
                                "files_restore_incomplete": bool(
                                    r.get("files_restore_incomplete")
                                ),
                            },
                            "to": {
                                "computer_id": primary_id,
                                "status": primary.get("status"),
                                "sandbox_id": primary.get("provider_ref"),
                                "dir_name": target_dirs[str(r["workspace_id"])],
                                "resource_tier": primary.get("resource_tier"),
                                "is_always_on": primary.get("is_always_on"),
                                "platform_secret_version": primary.get(
                                    "platform_secret_version"
                                ),
                                "files_restore_incomplete": True,
                            },
                            "mirrored_file_count": len(
                                files_by_ws[str(r["workspace_id"])]
                            ),
                            "non_agent_md_files": [
                                f["file_path"]
                                for f in files_by_ws[str(r["workspace_id"])]
                                if f.get("file_path") != "agent.md"
                            ],
                        }
                        for r in rows
                    ],
                    "computers_to_vacate": old_computers,
                    "knowledge_files_written_warnings": knowledge_warnings,
                }
                print(json.dumps(plan, ensure_ascii=False, indent=2))
                for warning in knowledge_warnings:
                    print(warning)

                if not apply:
                    print("DRY RUN: nothing written. Re-run with --apply.")
                    return plan

                snap_dir = Path(snapshot_dir) if snapshot_dir else Path.home() / "Backups" / "dalton"
                snap_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
                uid8 = uuid.uuid4().hex[:8]
                snap_path = snap_dir / f"rebind-workspaces-{stamp}-{uid8}.json"
                async with alloc_conn.cursor(row_factory=dict_row) as link_cur:
                    await link_cur.execute(
                        "SELECT * FROM livefs_links "
                        "WHERE workspace_id = ANY(%s::uuid[]) OR computer_id = ANY(%s::uuid[]) "
                        "ORDER BY computer_id, workspace_id",
                        (sorted(ws_ids), [primary_id, *old_computers]),
                    )
                    livefs_links_before = await link_cur.fetchall()
                snap_payload = {
                    "database": db_info,
                    "livefs_links": livefs_links_before,
                    "primary_computer": primary,
                    "old_computers": old_computer_rows,
                    "before": rows,
                    "workspace_files": files_by_ws,
                    "knowledge_warnings": knowledge_warnings,
                    "plan": plan,
                }
                snap_path.write_text(
                    json.dumps(
                        snap_payload, default=str, ensure_ascii=False, indent=2
                    ),
                    encoding="utf-8",
                )
                snap_path.chmod(0o600)

                for r in rows:
                    wid = str(r["workspace_id"])
                    expected_cid = (
                        str(r["computer_id"]) if r.get("computer_id") else None
                    )
                    target_dir = target_dirs[wid]
                    bound = await bind_workspace_to_computer(
                        wid,
                        primary_id,
                        expected_computer_id=expected_cid,
                        dir_name=target_dir,
                        conn=alloc_conn,
                    )
                    if bound is None:
                        _refuse(
                            f"bind_workspace_to_computer failed for workspace {wid} "
                            f"(expected_computer_id={expected_cid!r})"
                        )
                    flagged = await set_files_restore_incomplete(
                        wid,
                        True,
                        conn=alloc_conn,
                        sandbox_id=primary["provider_ref"],
                    )
                    if not flagged:
                        _refuse(
                            f"set_files_restore_incomplete failed for workspace {wid}"
                        )
                    bound_rows.append(bound)

                if len(bound_rows) != len(ws_ids):
                    _refuse(
                        f"rebound {len(bound_rows)} workspaces, expected {len(ws_ids)}"
                    )

                # In the same transaction, assert that vacated old computers now have 0 live workspaces.
                if old_computers:
                    remaining_counts = await count_live_workspaces_by_computer(
                        old_computers, conn=alloc_conn
                    )
                    non_empty: dict[str, Any] = {
                        cid: cnt
                        for cid, cnt in remaining_counts.items()
                        if int(cnt or 0) > 0
                    }
                    for old_cid in old_computers:
                        remaining_ids = await get_live_workspace_ids_for_computer(
                            old_cid, conn=alloc_conn
                        )
                        if remaining_ids:
                            non_empty[old_cid] = remaining_ids
                    if non_empty:
                        _refuse(
                            "vacated computer(s) still have live workspaces after "
                            f"rebind: {non_empty}; rolled back"
                        )
    except (WorkspaceDirNameTaken, psycopg.errors.UniqueViolation) as exc:
        _refuse(f"unique constraint conflict during rebind: {exc}")

    print(
        f"APPLIED: {len(bound_rows)} workspaces rebound onto {primary_id}; "
        f"vacated_computers_confirmed_empty={old_computers}; snapshot {snap_path}"
    )
    return plan


async def _async_main(args: argparse.Namespace) -> int:
    _resolve_and_prepare_target_db(db_host=args.db_host, db_name=args.db_name)
    db_pool = get_or_create_pool()
    await db_pool.open(wait=True)
    try:
        await rebind_workspaces_to_primary(
            user=args.user,
            workspaces_arg=args.workspaces,
            snapshot_dir=args.snapshot_dir,
            apply=args.apply,
            expected_primary_computer_id=args.primary_computer,
            db_host=args.db_host,
            db_name=args.db_name,
        )
    finally:
        await db_pool.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    load_repo_dotenv()
    ap = argparse.ArgumentParser(
        description="Bind the owner's four migrated company workspaces onto the primary computer"
    )
    ap.add_argument(
        "--user",
        default=OWNER_USER_ID,
        help=f"Target owner user_id (must be {OWNER_USER_ID!r})",
    )
    ap.add_argument(
        "--workspaces",
        default=",".join(WHITELISTED_WORKSPACE_IDS),
        help="Comma-separated workspace UUIDs (must match the 4 KNOWN_MIGRATED_WORKSPACES)",
    )
    ap.add_argument(
        "--primary-computer",
        default=PRIMARY_COMPUTER_ID,
        help=f"Expected primary computer UUID (default: {PRIMARY_COMPUTER_ID})",
    )
    ap.add_argument(
        "--db-host",
        default=None,
        help="Expected Postgres host override (defaults to DB_HOST in .env)",
    )
    ap.add_argument(
        "--db-name",
        default=None,
        help="Expected Postgres database name override (defaults to DB_NAME in .env)",
    )
    ap.add_argument(
        "--snapshot-dir",
        default=str(Path.home() / "Backups" / "dalton"),
        help="Directory for pre-apply JSON snapshots",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Execute the rebind inside a guarded transaction (default is dry-run)",
    )
    args = ap.parse_args(argv)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
