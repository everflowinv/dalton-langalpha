"""Migrate single-user era data from ``local-dev-user`` to the owner's Supabase account.

Default mode is **dry-run** (read-only): inspects the source/target rows in ``users``,
counts how many rows in every referencing table/JSONB/namespace location will change,
and prints the migration plan without writing to the database or creating a snapshot.

Pass ``--apply`` to execute the migration:
1. Exports a ``0600`` JSON snapshot of the source ``users`` row and every row to be
   modified before opening the write transaction.
2. Executes the entire migration inside a single database transaction:
   - Locks and re-verifies ``users`` (refuses if target user already exists).
   - Inserts the new ``users`` row (copying fields from ``local-dev-user``, setting
     ``user_id`` to the owner's Supabase UUID and ``email`` to the owner's email).
   - Updates all child/referencing tables, JSONB fields, and LangGraph ``store`` /
     ``checkpoints`` rows (temporarily disabling immutability/timestamp triggers where
     required inside the transaction), asserting after each ``UPDATE`` that
     ``cur.rowcount`` equals the dry-run count.
   - Deletes the old ``local-dev-user`` row from ``users`` and asserts ``rowcount == 1``.
   - Runs post-migration self-checks inside the transaction: verifies that all 4
     ``KNOWN_MIGRATED_WORKSPACES`` (META, AMZN, GOOGL, MSFT) and the Home workspace are
     live and owned by the new owner UUID. Any mismatch raises ``RuntimeError`` and
     rolls back the transaction.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import psycopg
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.realrun._common import KNOWN_MIGRATED_WORKSPACES
from src.server.database.home_workspace import (
    HOME_WORKSPACE_NAMESPACE,
    is_home_id,
    is_home_row,
)

OLD_USER_ID = "local-dev-user"
NEW_USER_ID = "991a99ad-8ff2-4bd8-bf08-e5963a248798"
OWNER_EMAIL = "richard.lu@everflowinv.com"

OLD_HOME_WORKSPACE_ID = str(uuid.uuid5(HOME_WORKSPACE_NAMESPACE, OLD_USER_ID))
NEW_HOME_WORKSPACE_ID = str(uuid.uuid5(HOME_WORKSPACE_NAMESPACE, NEW_USER_ID))

DEFAULT_SNAPSHOT_DIR = REPO_ROOT / "backups"
_SAFE_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass(frozen=True)
class MigrationTarget:
    """One table or column/JSONB location referencing the user identity."""

    name: str
    table: str
    select_sql: str
    update_sql: str
    disable_triggers: tuple[str, ...] = ()


def _simple_user_id_target(
    table: str,
    *,
    col: str = "user_id",
    disable_triggers: tuple[str, ...] = (),
) -> MigrationTarget:
    return MigrationTarget(
        name=f"{table}.{col}",
        table=table,
        select_sql=f"SELECT * FROM {table} WHERE {col} = %(old_user)s",
        update_sql=f"UPDATE {table} SET {col} = %(new_user)s WHERE {col} = %(old_user)s",
        disable_triggers=disable_triggers,
    )


MIGRATION_TARGETS: tuple[MigrationTarget, ...] = (
    # Core FK-backed tables referencing users(user_id)
    _simple_user_id_target(
        "computers",
    ),
    _simple_user_id_target(
        "workspaces",
    ),
    _simple_user_id_target("user_preferences"),
    _simple_user_id_target("user_api_keys"),
    _simple_user_id_target("user_oauth_tokens"),
    _simple_user_id_target("watchlists"),
    _simple_user_id_target("watchlist_items"),
    _simple_user_id_target("user_portfolios"),
    _simple_user_id_target("automations"),
    _simple_user_id_target("trading_permissions"),
    _simple_user_id_target("trading_permission_events"),
    # Conversation, run ledger, transcript & blob tables
    _simple_user_id_target("conversation_responses"),
    _simple_user_id_target("subagent_runs"),
    _simple_user_id_target("conversation_usages"),
    _simple_user_id_target("conversation_feedback"),
    _simple_user_id_target("thread_transcript_files"),
    _simple_user_id_target("workspace_file_blobs"),
    _simple_user_id_target("livefs_tokens"),
    _simple_user_id_target("market_insights"),
    _simple_user_id_target("order_attempts"),
    # Plugins, MCP catalog, vault secrets, skills & subagents
    _simple_user_id_target("user_plugins"),
    _simple_user_id_target("user_mcp_servers"),
    _simple_user_id_target("user_mcp_oauth_connections"),
    _simple_user_id_target("user_mcp_tool_schemas"),
    _simple_user_id_target("user_mcp_builtin_disables"),
    _simple_user_id_target("mcp_oauth_pending"),
    _simple_user_id_target("user_vault_secrets"),
    _simple_user_id_target("sandbox_egress_grants"),
    _simple_user_id_target("user_skills"),
    _simple_user_id_target("user_subagents"),
    _simple_user_id_target("plugin_subagents"),
    # Outbox JSONB payload
    MigrationTarget(
        name="hook_outbox.payload.user_id",
        table="hook_outbox",
        select_sql=(
            "SELECT * FROM hook_outbox WHERE payload->>'user_id' = %(old_user)s"
        ),
        update_sql=(
            "UPDATE hook_outbox "
            "SET payload = jsonb_set(payload, '{user_id}', to_jsonb(%(new_user)s::text), true) "
            "WHERE payload->>'user_id' = %(old_user)s"
        ),
    ),
    MigrationTarget(
        name="conversation_responses.metadata.user_id",
        table="conversation_responses",
        select_sql=(
            "SELECT * FROM conversation_responses WHERE metadata->>'user_id' = %(old_user)s"
        ),
        update_sql=(
            "UPDATE conversation_responses "
            "SET metadata = jsonb_set(metadata, '{user_id}', to_jsonb(%(new_user)s::text), true) "
            "WHERE metadata->>'user_id' = %(old_user)s"
        ),
    ),
    # LangGraph Store (long-term memory, workspace memory, memos, workflows)
    MigrationTarget(
        name="store.prefix",
        table="store",
        select_sql=(
            "SELECT * FROM store "
            "WHERE prefix = %(old_user)s OR prefix LIKE (%(old_user)s || '.%%')"
        ),
        update_sql=(
            "UPDATE store "
            "SET prefix = %(new_user)s || substr(prefix, char_length(%(old_user)s) + 1) "
            "WHERE prefix = %(old_user)s OR prefix LIKE (%(old_user)s || '.%%')"
        ),
    ),
    # LangGraph Checkpointer metadata JSONB
    MigrationTarget(
        name="checkpoints.metadata.user_id",
        table="checkpoints",
        select_sql=(
            "SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, metadata "
            "FROM checkpoints WHERE metadata->>'user_id' = %(old_user)s"
        ),
        update_sql=(
            "UPDATE checkpoints "
            "SET metadata = jsonb_set(metadata, '{user_id}', to_jsonb(%(new_user)s::text), true) "
            "WHERE metadata->>'user_id' = %(old_user)s"
        ),
    ),
    # Dalton tables (d001..d008)
    _simple_user_id_target("dalton_budget_config"),
    _simple_user_id_target("dalton_budget_task_caps"),
    _simple_user_id_target("dalton_budget_reservations"),
    _simple_user_id_target("dalton_openclaw_sync_runs", col="started_by"),
    _simple_user_id_target(
        "dalton_knowledge_versions",
        disable_triggers=("trg_dalton_kv_guard",),
    ),
    _simple_user_id_target(
        "dalton_verdicts",
        disable_triggers=("trg_dalton_verdicts_immutable",),
    ),
    MigrationTarget(
        name="dalton_publish_requests.user_id_or_decided_by",
        table="dalton_publish_requests",
        select_sql=(
            "SELECT * FROM dalton_publish_requests "
            "WHERE user_id = %(old_user)s OR decided_by = %(old_user)s"
        ),
        update_sql=(
            "UPDATE dalton_publish_requests "
            "SET user_id = CASE WHEN user_id = %(old_user)s THEN %(new_user)s ELSE user_id END, "
            "    decided_by = CASE WHEN decided_by = %(old_user)s THEN %(new_user)s ELSE decided_by END "
            "WHERE user_id = %(old_user)s OR decided_by = %(old_user)s"
        ),
    ),
    MigrationTarget(
        name="dalton_publications.user_id_or_approver_user_id",
        table="dalton_publications",
        select_sql=(
            "SELECT * FROM dalton_publications "
            "WHERE user_id = %(old_user)s OR approver_user_id = %(old_user)s"
        ),
        update_sql=(
            "UPDATE dalton_publications "
            "SET user_id = CASE WHEN user_id = %(old_user)s THEN %(new_user)s ELSE user_id END, "
            "    approver_user_id = CASE WHEN approver_user_id = %(old_user)s THEN %(new_user)s ELSE approver_user_id END "
            "WHERE user_id = %(old_user)s OR approver_user_id = %(old_user)s"
        ),
        disable_triggers=("trg_dalton_pub_immutable",),
    ),
    MigrationTarget(
        name="dalton_daily_plans.user_id_and_board_snapshot",
        table="dalton_daily_plans",
        select_sql=(
            "SELECT * FROM dalton_daily_plans "
            "WHERE user_id = %(old_user)s OR board_snapshot->>'user_id' = %(old_user)s"
        ),
        update_sql=(
            "UPDATE dalton_daily_plans "
            "SET user_id = CASE WHEN user_id = %(old_user)s THEN %(new_user)s ELSE user_id END, "
            "    board_snapshot = CASE "
            "        WHEN board_snapshot->>'user_id' = %(old_user)s "
            "        THEN jsonb_set(board_snapshot, '{user_id}', to_jsonb(%(new_user)s::text), true) "
            "        ELSE board_snapshot "
            "    END "
            "WHERE user_id = %(old_user)s OR board_snapshot->>'user_id' = %(old_user)s"
        ),
    ),
    _simple_user_id_target("dalton_llm_journal"),
    _simple_user_id_target("dalton_provider_events"),
)


def parse_dotenv_db_vars(env_path: Path) -> dict[str, str]:
    """Read only ``DB_*`` keys from ``.env`` without mutating ``os.environ``."""
    out: dict[str, str] = {}
    if not env_path.is_file():
        return out
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key.startswith("DB_"):
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
    return out


def resolve_db_params(env_file: Path | None = None) -> dict[str, str]:
    """Resolve DB connection parameters from process env first, then ``.env`` file."""
    dotenv_vars = parse_dotenv_db_vars(env_file or (REPO_ROOT / ".env"))

    def _get(key: str, default: str) -> str:
        return os.environ.get(key) or dotenv_vars.get(key) or default

    return {
        "host": _get("DB_HOST", "localhost"),
        "port": _get("DB_PORT", "5432"),
        "dbname": _get("DB_NAME", "langalpha"),
        "user": _get("DB_USER", "postgres"),
        "password": _get("DB_PASSWORD", ""),
        "sslmode": _get("DB_SSLMODE", "prefer"),
    }


def build_conninfo(params: dict[str, str]) -> str:
    return (
        f"postgresql://{quote_plus(params['user'])}:{quote_plus(params['password'])}"
        f"@{params['host']}:{params['port']}/{params['dbname']}"
        f"?sslmode={quote_plus(params['sslmode'])}"
    )


def verify_identity_pair(
    old_user: str,
    new_user: str,
    owner_email: str = OWNER_EMAIL,
) -> None:
    """Enforce the exact hardcoded migration identity pair."""
    if old_user != OLD_USER_ID or new_user != NEW_USER_ID:
        raise RuntimeError(
            f"Identity pair mismatch: only {OLD_USER_ID!r} -> {NEW_USER_ID!r} is allowed, "
            f"got {old_user!r} -> {new_user!r}"
        )
    if owner_email.strip().lower() != OWNER_EMAIL:
        raise RuntimeError(
            f"Owner email mismatch: only {OWNER_EMAIL!r} is allowed, got {owner_email!r}"
        )


def verify_db_target(
    conn: psycopg.Connection[Any],
    *,
    configured_host: str,
    expected_host: str | None,
    expected_db: str | None,
    require_explicit: bool,
) -> tuple[str, str]:
    """Verify the connected Postgres database name and configured host."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT current_database() AS db_name")
        row = cur.fetchone()
    actual_db = str((row or {}).get("db_name") or "")
    actual_host = configured_host.strip()

    if require_explicit and (not expected_host or not expected_db):
        raise RuntimeError(
            "Refusing --apply without explicit --expected-db-host and --expected-db-name "
            "(or MIGRATE_EXPECTED_DB_HOST / MIGRATE_EXPECTED_DB_NAME)."
        )
    if expected_host and actual_host != expected_host.strip():
        raise RuntimeError(
            f"DB host mismatch: connected host={actual_host!r}, expected={expected_host!r}"
        )
    if expected_db and actual_db != expected_db.strip():
        raise RuntimeError(
            f"DB name mismatch: current_database()={actual_db!r}, expected={expected_db!r}"
        )
    return actual_host, actual_db


def write_snapshot(snapshot_dir: Path, payload: dict[str, Any]) -> Path:
    """Write pre-migration snapshot JSON with mode 0600."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = snapshot_dir / f"migrate-owner-identity-{ts}.json"
    encoded = json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8")
    fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)
    os.chmod(out_path, 0o600)
    return out_path


def _verify_users_preconditions(
    cur: psycopg.Cursor[Any],
    *,
    old_user: str,
    new_user: str,
    owner_email: str,
    for_update: bool = False,
) -> dict[str, Any]:
    """Verify source user exists and target user / email do not conflict."""
    lock_clause = " FOR UPDATE" if for_update else ""
    cur.execute(
        f"SELECT * FROM users WHERE user_id = %(new_user)s{lock_clause}",
        {"new_user": new_user},
    )
    existing_target = cur.fetchone()
    if existing_target is not None:
        raise RuntimeError(
            f"Target user {new_user!r} already exists in users table; "
            "refusing to overwrite or merge automatically."
        )

    cur.execute(
        f"SELECT * FROM users WHERE user_id = %(old_user)s{lock_clause}",
        {"old_user": old_user},
    )
    source_user = cur.fetchone()
    if source_user is None:
        raise RuntimeError(
            f"Source user {old_user!r} does not exist in users table."
        )

    cur.execute(
        f"""
        SELECT user_id, email FROM users
        WHERE LOWER(email) = LOWER(%(owner_email)s)
          AND user_id <> %(old_user)s{lock_clause}
        """,
        {"owner_email": owner_email, "old_user": old_user},
    )
    email_conflict = cur.fetchone()
    if email_conflict is not None:
        raise RuntimeError(
            f"Owner email {owner_email!r} is already bound to user "
            f"{email_conflict['user_id']!r}."
        )

    return dict(source_user)


def _insert_new_user_from_old(
    cur: psycopg.Cursor[Any],
    *,
    old_user_row: dict[str, Any],
    new_user: str,
    owner_email: str,
) -> None:
    """Insert the new ``users`` row by copying all columns from ``old_user_row``."""
    new_row = dict(old_user_row)
    new_row["user_id"] = new_user
    new_row["email"] = owner_email

    cols = list(new_row.keys())
    for col in cols:
        if not _SAFE_IDENT_RE.match(col):
            raise RuntimeError(f"Unsafe column name in users row: {col!r}")

    col_list_sql = ", ".join(cols)
    val_list_sql = ", ".join(f"%({col})s" for col in cols)
    cur.execute(
        f"INSERT INTO users ({col_list_sql}) VALUES ({val_list_sql})",
        new_row,
    )
    if cur.rowcount != 1:
        raise RuntimeError(
            f"Expected INSERT INTO users to affect 1 row, got {cur.rowcount}"
        )


def _verify_post_migration_ownership(
    cur: psycopg.Cursor[Any],
    *,
    new_user: str,
) -> dict[str, Any]:
    """Verify inside the transaction that the 4 company workspaces + Home belong to ``new_user``."""
    cur.execute(
        """
        SELECT workspace_id::text AS workspace_id, user_id, name, dir_name, status
        FROM workspaces
        WHERE user_id = %(new_user)s AND status <> 'deleted'
        ORDER BY created_at ASC, workspace_id ASC
        """,
        {"new_user": new_user},
    )
    live_workspaces = [dict(r) for r in cur.fetchall()]
    by_id = {str(r["workspace_id"]).lower(): r for r in live_workspaces}

    expected_company_ids = {
        ticker: wid.lower()
        for ticker, wid in KNOWN_MIGRATED_WORKSPACES.items()
    }
    missing_companies = [
        f"{ticker} ({wid})"
        for ticker, wid in sorted(expected_company_ids.items())
        if wid not in by_id or str(by_id[wid].get("user_id")) != new_user
    ]
    if missing_companies:
        raise RuntimeError(
            "Post-migration check failed: missing or unowned company workspaces for "
            f"{new_user}: {', '.join(missing_companies)}"
        )

    company_id_set = set(expected_company_ids.values())
    home_candidates = [
        r
        for r in live_workspaces
        if str(r["workspace_id"]).lower() not in company_id_set
        and (
            str(r["workspace_id"]).lower() in {OLD_HOME_WORKSPACE_ID.lower(), NEW_HOME_WORKSPACE_ID.lower()}
            or is_home_id(OLD_USER_ID, r["workspace_id"])
            or is_home_id(new_user, r["workspace_id"])
            or is_home_row(r)
            or str(r.get("name") or "").strip().lower() == "home"
            or str(r.get("dir_name") or "").strip().lower() == "home"
        )
    ]
    if len(home_candidates) != 1:
        raise RuntimeError(
            f"Post-migration check failed: expected exactly 1 live Home workspace for "
            f"{new_user}, found {len(home_candidates)}: "
            f"{[r['workspace_id'] for r in home_candidates]}"
        )

    home_row = home_candidates[0]
    actual_home_id = str(home_row["workspace_id"]).lower()
    if str(home_row.get("user_id")) != new_user or actual_home_id != OLD_HOME_WORKSPACE_ID.lower():
        raise RuntimeError(
            f"Post-migration check failed: expected Home workspace for {new_user} to have "
            f"workspace_id={OLD_HOME_WORKSPACE_ID!r}, got {home_row['workspace_id']!r} "
            f"(user_id={home_row.get('user_id')!r})"
        )

    return {
        "company_workspace_ids": sorted(expected_company_ids.values()),
        "home_workspace_id": str(home_row["workspace_id"]),
        "total_live_workspaces": len(live_workspaces),
    }


def run_migration(
    conn: psycopg.Connection[Any],
    *,
    old_user: str = OLD_USER_ID,
    new_user: str = NEW_USER_ID,
    owner_email: str = OWNER_EMAIL,
    apply: bool = False,
    snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR,
    targets: tuple[MigrationTarget, ...] = MIGRATION_TARGETS,
) -> dict[str, Any]:
    """Inspect and optionally execute the owner identity migration."""
    verify_identity_pair(old_user, new_user, owner_email)

    params = {"old_user": old_user, "new_user": new_user, "owner_email": owner_email}
    counts: dict[str, int] = {}
    snapshot_rows: dict[str, list[dict[str, Any]]] = {}

    with conn.cursor(row_factory=dict_row) as cur:
        old_user_row = _verify_users_preconditions(
            cur,
            old_user=old_user,
            new_user=new_user,
            owner_email=owner_email,
            for_update=False,
        )
        counts["users.user_id"] = 1
        snapshot_rows["users.user_id"] = [old_user_row]

        for target in targets:
            cur.execute(target.select_sql, params)
            rows = [dict(r) for r in cur.fetchall()]
            counts[target.name] = len(rows)
            if rows:
                snapshot_rows[target.name] = rows

    total_rows_to_modify = sum(counts.values())
    print(f"=== Owner Identity Migration ({'APPLY' if apply else 'DRY-RUN'}) ===")
    print(f"Source user : {old_user}")
    print(f"Target user : {new_user} ({owner_email})")
    print("Planned row modifications by location:")
    for name, count in counts.items():
        if count > 0 or name in ("users.user_id", "workspaces.user_id", "computers.user_id"):
            print(f"  - {name:48s}: {count}")
    zero_targets = [name for name, count in counts.items() if count == 0]
    print(f"Zero-row locations checked: {len(zero_targets)} / {len(counts)}")
    print(f"Total rows to modify (including users PK): {total_rows_to_modify}")

    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "old_user": old_user,
        "new_user": new_user,
        "owner_email": owner_email,
        "counts": counts,
        "total_rows": total_rows_to_modify,
        "snapshot_path": None,
        "post_check": None,
    }

    if not apply:
        print("\nDRY-RUN ONLY — no changes written (pass --apply to execute).")
        return report

    snapshot_payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "old_user": old_user,
        "new_user": new_user,
        "owner_email": owner_email,
        "counts": counts,
        "rows": snapshot_rows,
    }
    snap_path = write_snapshot(snapshot_dir, snapshot_payload)
    report["snapshot_path"] = str(snap_path)
    print(f"\nWrote pre-migration snapshot (0600): {snap_path}")

    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        locked_user_row = _verify_users_preconditions(
            cur,
            old_user=old_user,
            new_user=new_user,
            owner_email=owner_email,
            for_update=True,
        )
        _insert_new_user_from_old(
            cur,
            old_user_row=locked_user_row,
            new_user=new_user,
            owner_email=owner_email,
        )

        for target in targets:
            expected_count = counts[target.name]
            for trig in target.disable_triggers if expected_count > 0 else ():
                cur.execute(
                    "SELECT 1 FROM pg_trigger WHERE tgrelid = %s::regclass AND tgname = %s",
                    (target.table, trig),
                )
                if cur.fetchone() is None:
                    # A guard we expect to bypass is gone: the schema changed under us; stop.
                    raise RuntimeError(f"Expected trigger {trig} on {target.table} not found; rolling back.")
            for trig in target.disable_triggers:
                if expected_count > 0:
                    cur.execute(f"ALTER TABLE {target.table} DISABLE TRIGGER {trig}")
            cur.execute(target.update_sql, params)
            actual_count = cur.rowcount
            for trig in target.disable_triggers:
                if expected_count > 0:
                    cur.execute(f"ALTER TABLE {target.table} ENABLE TRIGGER {trig}")
            if actual_count != expected_count:
                raise RuntimeError(
                    f"Row count mismatch on {target.name}: dry-run counted "
                    f"{expected_count}, UPDATE affected {actual_count}; rolling back."
                )

        # Nothing may still carry the old identity: a row written between the
        # count and the update (the backend must be stopped) would be orphaned.
        for target in targets:
            cur.execute(target.select_sql, params)
            leftover = cur.fetchall()
            if leftover:
                raise RuntimeError(
                    f"{len(leftover)} row(s) in {target.name} still reference {old_user} "
                    "after the update; rolling back. Stop the backend and retry."
                )

        cur.execute(
            "DELETE FROM users WHERE user_id = %(old_user)s",
            {"old_user": old_user},
        )
        if cur.rowcount != 1:
            raise RuntimeError(
                f"Expected DELETE FROM users for {old_user!r} to affect 1 row, "
                f"got {cur.rowcount}; rolling back."
            )

        post_check = _verify_post_migration_ownership(cur, new_user=new_user)
        report["post_check"] = post_check

    required_env = f"DALTON_HOME_WORKSPACE_ALIASES={new_user}:{OLD_HOME_WORKSPACE_ID}"
    report["required_env"] = required_env
    print(
        f"APPLIED: migrated {old_user} -> {new_user}; verified 4 company workspaces + "
        f"Home ({report['post_check']['home_workspace_id']}) belong to {new_user}."
    )
    print(f"Set in environment before restarting backend:\n  {required_env}")
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate single-user data from local-dev-user to owner Supabase UUID."
    )
    parser.add_argument(
        "--old-user",
        default=OLD_USER_ID,
        help=f"Source user_id (must be {OLD_USER_ID!r}).",
    )
    parser.add_argument(
        "--new-user",
        default=NEW_USER_ID,
        help=f"Target Supabase user_id (must be {NEW_USER_ID!r}).",
    )
    parser.add_argument(
        "--owner-email",
        default=OWNER_EMAIL,
        help=f"Target owner email (must be {OWNER_EMAIL!r}).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute writes inside a single transaction (default is dry-run).",
    )
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=DEFAULT_SNAPSHOT_DIR,
        help="Directory for pre-migration 0600 JSON snapshot (default: backups/).",
    )
    parser.add_argument(
        "--expected-db-host",
        default=os.environ.get("MIGRATE_EXPECTED_DB_HOST")
        or os.environ.get("REBIND_EXPECTED_DB_HOST"),
        help="Expected DB_HOST (required with --apply).",
    )
    parser.add_argument(
        "--expected-db-name",
        default=os.environ.get("MIGRATE_EXPECTED_DB_NAME")
        or os.environ.get("REBIND_EXPECTED_DB_NAME"),
        help="Expected DB_NAME / current_database() (required with --apply).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    verify_identity_pair(args.old_user, args.new_user, args.owner_email)
    db_params = resolve_db_params()
    conninfo = build_conninfo(db_params)
    with psycopg.connect(conninfo, autocommit=True) as conn:
        verify_db_target(
            conn,
            configured_host=db_params["host"],
            expected_host=args.expected_db_host,
            expected_db=args.expected_db_name,
            require_explicit=bool(args.apply),
        )
        run_migration(
            conn,
            old_user=args.old_user,
            new_user=args.new_user,
            owner_email=args.owner_email,
            apply=bool(args.apply),
            snapshot_dir=args.snapshot_dir,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
