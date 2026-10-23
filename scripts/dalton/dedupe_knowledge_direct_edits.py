"""List (and optionally delete) spurious ``direct_edit`` knowledge versions for one workspace.

Before the canonical-JSON fix, post-turn reconcile compared a hash of the in-memory key
order with a file written in jsonb key order, so it recorded ``data/<doc>.json`` direct
edits whose content equals the previous version, each followed by a ``<doc>.md``
``rendered_md_edited`` marker caused only by the bumped version number in the header.

A row is listed when it is
- a ``data/<doc>.json`` direct_edit whose content equals the previous version's content
  (compared as parsed JSON), or
- a ``<doc>.md`` marker whose immediate predecessor is such a row (the header bump);
and only when the listed rows are the tail of that doc's history, so deleting them never
leaves a later version pointing at a missing base. Docs whose spurious rows are followed
by real versions are reported and left alone.

Default is a read-only dry run (``default_transaction_read_only``). ``--apply`` needs
``--expect-count``; it writes a JSON snapshot of the rows first, then in one transaction
takes the same per-doc advisory locks as ``insert_version``, re-selects the candidates,
asserts the ids match the snapshot, deletes them with the immutability trigger disabled
for that transaction only, and asserts the deleted row count.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.migrate_owner_identity import (  # noqa: E402
    build_conninfo,
    resolve_db_params,
)

TRIGGER = "trg_dalton_kv_guard"

_SELECT = """
SELECT knowledge_version_id::text AS knowledge_version_id, user_id, workspace_id::text AS workspace_id,
       doc_key, version_no, source, content, content_sha256, ops, reason, evidence_refs,
       warnings, schema_valid, edited_path, conversation_thread_id::text AS conversation_thread_id,
       run_id::text AS run_id, tool_call_id, files_written_at, created_at
FROM dalton_knowledge_versions
WHERE workspace_id = %s
ORDER BY doc_key, version_no
"""


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def find_spurious(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``(deletable_rows, notes)`` from one workspace's rows ordered by doc, version."""
    by_doc: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_doc.setdefault(r["doc_key"], []).append(r)

    deletable: list[dict[str, Any]] = []
    notes: list[str] = []
    for doc_key, versions in sorted(by_doc.items()):
        spurious_ids: set[str] = set()
        for prev, cur in zip(versions, versions[1:]):
            if cur["source"] != "direct_edit":
                continue
            if cur["edited_path"] == f"data/{doc_key}.json":
                if _canon(cur["content"]) == _canon(prev["content"]):
                    spurious_ids.add(cur["knowledge_version_id"])
            elif (
                prev["knowledge_version_id"] in spurious_ids
                and cur["edited_path"] == f"{doc_key}.md"
                and any(
                    isinstance(w, dict) and w.get("code") == "rendered_md_edited"
                    for w in (cur["warnings"] or [])
                )
            ):
                spurious_ids.add(cur["knowledge_version_id"])
        if not spurious_ids:
            continue
        first = min(
            i for i, v in enumerate(versions) if v["knowledge_version_id"] in spurious_ids
        )
        tail = versions[first:]
        if all(v["knowledge_version_id"] in spurious_ids for v in tail):
            deletable.extend(tail)
        else:
            notes.append(
                f"{doc_key}: spurious rows are followed by real versions; left alone "
                f"({sorted(v['version_no'] for v in versions if v['knowledge_version_id'] in spurious_ids)})"
            )
    return deletable, notes


def _fmt(r: dict[str, Any]) -> str:
    codes = ",".join(
        str(w.get("code")) for w in (r["warnings"] or []) if isinstance(w, dict)
    )
    return (
        f"{r['doc_key']:<11} v{r['version_no']:<3} {r['edited_path'] or '':<22} "
        f"sha={str(r['content_sha256'])[:10]} written={r['files_written_at'] is not None} "
        f"at={r['created_at'].isoformat() if r['created_at'] else ''} "
        f"id={r['knowledge_version_id']}{'  [' + codes + ']' if codes else ''}"
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def run(
    workspace_id: str,
    *,
    apply: bool,
    expect_count: int | None,
    snapshot_dir: Path,
    conninfo: str,
) -> list[dict[str, Any]]:
    options = None if apply else "-c default_transaction_read_only=on"
    with psycopg.connect(conninfo, options=options) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_SELECT, (workspace_id,))
            rows = cur.fetchall()
        conn.rollback()
        deletable, notes = find_spurious(rows)

        print(f"workspace {workspace_id}: {len(rows)} versions, {len(deletable)} spurious")
        for r in deletable:
            print("  " + _fmt(r))
        for n in notes:
            print("  note: " + n)

        if not apply:
            print("DRY RUN: nothing changed (re-run with --apply --expect-count N)")
            return deletable

        if expect_count is None or expect_count != len(deletable):
            raise SystemExit(
                f"REFUSED: --expect-count {expect_count} != {len(deletable)} candidates"
            )
        if not deletable:
            print("nothing to delete")
            return deletable

        snapshot_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        snap = snapshot_dir / f"knowledge_direct_edit_dedupe_{workspace_id}_{stamp}.json"
        snap.write_text(
            json.dumps(deletable, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        print(f"snapshot written: {snap}")

        expected_ids = sorted(r["knowledge_version_id"] for r in deletable)
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            for doc_key in sorted({r["doc_key"] for r in deletable}):
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"dalton:kv:{workspace_id}:{doc_key}",),
                )
            cur.execute(_SELECT, (workspace_id,))
            again, _ = find_spurious(cur.fetchall())
            again_ids = sorted(r["knowledge_version_id"] for r in again)
            if again_ids != expected_ids:
                raise RuntimeError("candidates changed since the snapshot; rolled back")
            cur.execute(f"ALTER TABLE dalton_knowledge_versions DISABLE TRIGGER {TRIGGER}")
            cur.execute(
                "DELETE FROM dalton_knowledge_versions "
                "WHERE workspace_id = %s AND knowledge_version_id = ANY(%s::uuid[])",
                (workspace_id, expected_ids),
            )
            deleted = cur.rowcount
            cur.execute(f"ALTER TABLE dalton_knowledge_versions ENABLE TRIGGER {TRIGGER}")
            if deleted != len(expected_ids):
                raise RuntimeError(
                    f"deleted {deleted} rows, expected {len(expected_ids)}; rolled back"
                )
        print(f"APPLIED: deleted {deleted} rows; snapshot {snap}")
        return deletable


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="Workspace UUID")
    ap.add_argument("--apply", action="store_true", help="Delete the listed rows")
    ap.add_argument("--expect-count", type=int, default=None, help="Required with --apply")
    ap.add_argument(
        "--snapshot-dir",
        type=Path,
        default=Path.home() / "Backups" / "dalton",
        help="Directory for the pre-apply JSON snapshot",
    )
    args = ap.parse_args(argv)
    run(
        args.workspace,
        apply=args.apply,
        expect_count=args.expect_count,
        snapshot_dir=args.snapshot_dir,
        conninfo=build_conninfo(resolve_db_params()),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
