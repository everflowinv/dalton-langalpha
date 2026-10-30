#!/usr/bin/env python3
"""Read-only coverage-identity inventory for the sector layer (P0).

Suggests a coverage identity for every workspace under the COMPATIBLE rules
only (company knowledge doc → implicit company; Home → no unit; anything else
→ unclassified), regardless of the `coverage_units.enabled` flag, and reports
what the registry actually holds beside it. Never writes: the session is put in
`default_transaction_read_only`, and the script issues SELECTs exclusively.

Connection follows the repo's script conventions: the app pool (DB_* env) by
default, or an explicit ``--dsn`` (e.g. a scratch database). Examples:

    uv run python scripts/dalton/coverage_inventory.py
    uv run python scripts/dalton/coverage_inventory.py --user-id local-dev-user
    uv run python scripts/dalton/coverage_inventory.py --dsn 'postgresql://…' --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from psycopg import AsyncConnection  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from src.dalton.coverage.identity import classify_workspace_coverage  # noqa: E402
from src.dalton.settings import is_coverage_units_enabled  # noqa: E402


async def _open_read_only_conn(dsn: str | None) -> Any:
    """A dedicated autocommit connection pinned read-only for the whole session."""
    from src.server.database import pool as pool_mod

    if dsn:
        conn = await AsyncConnection.connect(dsn, **pool_mod._CONNECTION_KWARGS)
        await pool_mod._configure_postgres_connection(conn)
    else:
        conn = await pool_mod.open_session_connection()
    await conn.set_autocommit(True)
    await conn.execute("SET default_transaction_read_only = on")
    return conn


async def _has_workspaces_metadata(cur: Any) -> bool:
    await cur.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'workspaces'
          AND column_name = 'metadata'
        """
    )
    return await cur.fetchone() is not None


async def _gather(conn: Any, user_id: str | None) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        if user_id:
            await cur.execute(
                """
                SELECT workspace_id::text AS workspace_id, user_id, name, status
                FROM workspaces
                WHERE status <> 'deleted' AND user_id = %s
                ORDER BY user_id, name
                """,
                (user_id,),
            )
        else:
            await cur.execute(
                """
                SELECT workspace_id::text AS workspace_id, user_id, name, status
                FROM workspaces
                WHERE status <> 'deleted'
                ORDER BY user_id, name
                """
            )
        workspaces = [dict(r) for r in await cur.fetchall()]

        await cur.execute(
            """
            SELECT DISTINCT ON (workspace_id, doc_key)
                   workspace_id::text AS workspace_id, doc_key, content
            FROM dalton_knowledge_versions
            WHERE schema_valid = TRUE AND doc_key IN ('company', 'thesis')
            ORDER BY workspace_id, doc_key, version_no DESC
            """
        )
        doc_rows = await cur.fetchall()
        docs_by_ws: dict[str, dict[str, Any]] = {}
        for r in doc_rows or []:
            ws_id = str(r["workspace_id"])
            entry = docs_by_ws.setdefault(ws_id, {"company": None, "thesis": None})
            content = r.get("content") if isinstance(r.get("content"), dict) else {}
            entry[str(r["doc_key"])] = content

        meta_tickers: dict[str, str] = {}
        if workspaces and await _has_workspaces_metadata(cur):
            ids = [str(w["workspace_id"]) for w in workspaces]
            await cur.execute(
                """
                SELECT workspace_id::text AS workspace_id, metadata->>'ticker' AS ticker
                FROM workspaces
                WHERE workspace_id = ANY(%s::uuid[])
                """,
                (ids,),
            )
            for r in await cur.fetchall() or []:
                raw = str(r.get("ticker") or "").strip().upper()
                if raw:
                    meta_tickers[str(r["workspace_id"])] = raw

        await cur.execute("SELECT to_regclass('public.dalton_coverage_units') AS reg")
        reg = await cur.fetchone()
        units: list[dict[str, Any]] = []
        units_table_present = bool(reg and reg.get("reg"))
        if units_table_present:
            await cur.execute(
                """
                SELECT unit_id::text AS unit_id, kind, owner_user_id, slug,
                       display_name, workspace_id::text AS workspace_id,
                       ticker, status, research_tz, created_at
                FROM dalton_coverage_units
                ORDER BY owner_user_id, kind, slug
                """
            )
            units = [dict(r) for r in await cur.fetchall()]

        # 兼容回退的最后一条：该用户已有的 dalton-* 自动化行（与
        # tracking/board 同一条规则，不过滤状态）。
        autos_by_user: dict[str, list[dict[str, Any]]] = {}
        await cur.execute(
            """
            SELECT user_id, workspace_id::text AS workspace_id, name, metadata
            FROM automations
            WHERE strpos(lower(name), 'dalton-') > 0
            ORDER BY file_name
            """
        )
        for ar in await cur.fetchall() or []:
            # The SQL only prefilters; tracking's own rule (Python strip(), which
            # also drops tabs and other whitespace btrim keeps) decides.
            if (
                isinstance(ar, dict)
                and ar.get("user_id")
                and str(ar.get("name") or "").strip().lower().startswith("dalton-")
            ):
                autos_by_user.setdefault(str(ar["user_id"]), []).append(dict(ar))

    return {
        "workspaces": workspaces,
        "docs_by_ws": docs_by_ws,
        "meta_tickers": meta_tickers,
        "units": units,
        "units_table_present": units_table_present,
        "autos_by_user": autos_by_user,
    }


def _classify_all(data: dict[str, Any]) -> list[dict[str, Any]]:
    """One inventory row per workspace: compatible suggestion + registered reality.

    建议身份与 tracking/看板/总监探针同一套兼容规则（round 2 起含
    metadata/文档/整串 ticker 名/自动化四步），不看开关。
    """
    units_by_ws = {
        str(u["workspace_id"]): u
        for u in data["units"]
        if u.get("workspace_id") and str(u.get("status") or "") == "active"
    }
    rows: list[dict[str, Any]] = []
    for ws in data["workspaces"]:
        ws_id = str(ws["workspace_id"])
        docs = data["docs_by_ws"].get(ws_id) or {"company": None, "thesis": None}
        identity = classify_workspace_coverage(
            ws,
            company_content=docs.get("company"),
            thesis_content=docs.get("thesis"),
            automations=data["autos_by_user"].get(str(ws.get("user_id") or ""), []),
        )
        unit = units_by_ws.get(ws_id)
        row = {
            "user_id": str(ws.get("user_id") or ""),
            "workspace_id": ws_id,
            "name": str(ws.get("name") or ""),
            "suggested": identity.kind,
            "suggested_source": identity.source,
            "suggested_ticker": identity.ticker or "",
            "suggested_ticker_source": identity.ticker_source or "",
            "metadata_ticker": data["meta_tickers"].get(ws_id, ""),
        }
        if unit:
            row["registered_unit"] = {
                "unit_id": str(unit.get("unit_id") or ""),
                "kind": str(unit.get("kind") or ""),
                "slug": str(unit.get("slug") or ""),
                "ticker": str(unit.get("ticker") or ""),
                "status": str(unit.get("status") or ""),
            }
        rows.append(row)
    return rows


def _find_conflicts(rows: list[dict[str, Any]], units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same owner + same ticker appearing through more than one workspace or unit."""
    conflicts: list[dict[str, Any]] = []

    occurrences: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        owner = row["user_id"]
        ws_id = row["workspace_id"]
        if row["suggested_ticker"]:
            occurrences.setdefault((owner, row["suggested_ticker"]), []).append(
                f"{ws_id} (company doc)"
            )
        if row.get("metadata_ticker"):
            occurrences.setdefault((owner, row["metadata_ticker"]), []).append(
                f"{ws_id} (workspace metadata)"
            )
    for (owner, ticker), sources in sorted(occurrences.items()):
        ws_sources = [s for s in sources]
        if len(ws_sources) > 1:
            conflicts.append(
                {
                    "type": "ticker_in_multiple_workspaces",
                    "user_id": owner,
                    "ticker": ticker,
                    "detail": f"ticker {ticker} claimed by {len(sources)} entries: " + ", ".join(sources),
                }
            )

    unit_occurrences: dict[tuple[str, str], list[str]] = {}
    for u in units:
        if str(u.get("status") or "") != "active" or not u.get("ticker"):
            continue
        unit_occurrences.setdefault(
            (str(u.get("owner_user_id") or ""), str(u["ticker"]).upper()), []
        ).append(f"unit {u.get('slug')} ({u.get('kind')})")
    for (owner, ticker), sources in sorted(unit_occurrences.items()):
        if len(sources) > 1:
            conflicts.append(
                {
                    "type": "ticker_in_multiple_units",
                    "user_id": owner,
                    "ticker": ticker,
                    "detail": f"ticker {ticker} registered by {len(sources)} active units: " + ", ".join(sources),
                }
            )

    implicit_by_owner_ticker: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        if row["suggested_ticker"]:
            implicit_by_owner_ticker.setdefault(
                (row["user_id"], row["suggested_ticker"]), []
            ).append(row)
    unit_rows = [
        u
        for u in units
        if str(u.get("status") or "") == "active" and u.get("ticker")
    ]
    for u in unit_rows:
        key = (str(u.get("owner_user_id") or ""), str(u["ticker"]).upper())
        for row in implicit_by_owner_ticker.get(key, []):
            if str(u.get("workspace_id") or "") == row["workspace_id"]:
                continue
            conflicts.append(
                {
                    "type": "ticker_registered_and_implicit_elsewhere",
                    "user_id": key[0],
                    "ticker": key[1],
                    "detail": (
                        f"ticker {key[1]}: active unit {u.get('slug')} vs implicit company doc in "
                        f"workspace {row['workspace_id']} ({row['name']}) — bind one or re-register"
                    ),
                }
            )

    for row in rows:
        unit = row.get("registered_unit")
        if not unit or unit.get("kind") != "company":
            continue
        unit_ticker = str(unit.get("ticker") or "").upper()
        if unit_ticker and row["suggested_ticker"] and row["suggested_ticker"] != unit_ticker:
            conflicts.append(
                {
                    "type": "unit_ticker_differs_from_doc",
                    "user_id": row["user_id"],
                    "ticker": unit_ticker,
                    "detail": (
                        f"workspace {row['workspace_id']} ({row['name']}): registered unit "
                        f"{unit.get('slug')} says {unit_ticker}, company doc says {row['suggested_ticker']}"
                    ),
                }
            )

    return conflicts


def _format_text(
    rows: list[dict[str, Any]],
    units: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    *,
    units_table_present: bool,
    flag_enabled: bool,
    user_filter: str | None,
) -> str:
    lines: list[str] = []
    lines.append("DALTON COVERAGE INVENTORY (read-only)")
    lines.append(
        f"generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}  "
        f"user filter: {user_filter or 'none'}  coverage_units.enabled: {flag_enabled}"
    )
    lines.append("")
    lines.append(f"Workspaces ({len(rows)}):")
    for row in rows:
        unit = row.get("registered_unit")
        unit_part = (
            f"  unit={unit['kind']}:{unit['slug']}({unit['status']})"
            if unit
            else "  unit=-"
        )
        ticker_part = row["suggested_ticker"] or row["metadata_ticker"] or "-"
        lines.append(
            f"  {row['suggested']:<16} user={row['user_id']}  ws={row['workspace_id']}  "
            f"name={row['name']!r}  ticker={ticker_part} ({row['suggested_ticker_source'] or '-'}){unit_part}"
        )
    lines.append("")
    if not units_table_present:
        lines.append("Registered units: table dalton_coverage_units not present (d023 not applied)")
    else:
        lines.append(f"Registered units ({len(units)}):")
        for u in units:
            lines.append(
                f"  {u['status']:<8} {u['kind']:<8} user={u['owner_user_id']}  slug={u['slug']}  "
                f"name={u['display_name']!r}  ws={u.get('workspace_id') or '-'}  "
                f"ticker={u.get('ticker') or '-'}  tz={u.get('research_tz') or '-'}"
            )
    lines.append("")
    lines.append(f"Conflicts ({len(conflicts)}):")
    if conflicts:
        for idx, c in enumerate(conflicts, start=1):
            lines.append(f"  {idx}. [{c['type']}] {c['detail']}")
    else:
        lines.append("  (none)")
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    conn = await _open_read_only_conn(args.dsn)
    try:
        data = await _gather(conn, args.user_id)
        rows = _classify_all(data)
        conflicts = _find_conflicts(rows, data["units"])
        if args.json:
            print(
                json.dumps(
                    {
                        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "user_filter": args.user_id,
                        "coverage_units_enabled": is_coverage_units_enabled(),
                        "units_table_present": data["units_table_present"],
                        "workspaces": rows,
                        "units": data["units"],
                        "conflicts": conflicts,
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
        else:
            print(
                _format_text(
                    rows,
                    data["units"],
                    conflicts,
                    units_table_present=data["units_table_present"],
                    flag_enabled=is_coverage_units_enabled(),
                    user_filter=args.user_id,
                )
            )
    finally:
        await conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dsn",
        default=os.getenv("DALTON_COVERAGE_INVENTORY_DSN"),
        help="Optional postgres DSN (default: the app pool from DB_* env)",
    )
    parser.add_argument("--user-id", default=None, help="Restrict to one user's workspaces")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
