"""List (and optionally delete) spurious ``direct_edit`` knowledge versions for one workspace.

Before the canonical-JSON fix, post-turn reconcile compared a hash of the in-memory key
order with a file written in jsonb key order, so it recorded ``data/<doc>.json`` direct
edits whose content equals the previous version, each followed by a ``<doc>.md``
``rendered_md_edited`` marker caused only by the bumped version number in the header.

A row is listed when it is
- a ``data/<doc>.json`` direct_edit whose content equals the previous version's content
  (compared as parsed JSON), or
- a ``<doc>.md`` ``rendered_md_edited`` marker that immediately follows such a row and
  whose recorded md hash equals the render of the version before it or of the duplicate
  itself, i.e. the md was provably a stale auto-render and only the header bumped;
and only when the listed rows are the tail of that doc's history, so deleting them never
leaves a later version pointing at a missing base, and none of them was ever written to
the sandbox (``files_written_at IS NULL``; the script never syncs files). Any other
direct_edit after a duplicate (a real md edit, for one) leaves the whole doc alone, and a
doc with a written candidate is refused; both are reported.

Default is a read-only dry run (``default_transaction_read_only``). ``--apply`` needs
``--expect-count``; it writes a JSON snapshot of the rows first, then in one transaction
takes the same per-doc advisory locks as ``insert_version``, re-selects the candidates,
asserts the ids match the snapshot, deletes them with the immutability trigger disabled
for that transaction only, and asserts the deleted row count.
"""

from __future__ import annotations

import argparse
import hashlib
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
from src.dalton.knowledge.render import render_document_md  # noqa: E402

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


def _md_sha(doc_key: str, row: dict[str, Any], ticker: str) -> str | None:
    md = render_document_md(
        doc_key,
        row["content"],
        version_no=int(row["version_no"]),
        at=row.get("created_at"),
        ticker=ticker,
    )
    return hashlib.sha256(md.encode("utf-8")).hexdigest() if md else None


def _is_stale_md_marker(
    doc_key: str,
    marker: dict[str, Any],
    dup: dict[str, Any],
    earlier: list[dict[str, Any]],
    ticker: str,
) -> bool:
    if marker["source"] != "direct_edit" or marker["edited_path"] != f"{doc_key}.md":
        return False
    md_shas = [
        w.get("md_sha256")
        for w in (marker["warnings"] or [])
        if isinstance(w, dict) and w.get("code") == "rendered_md_edited"
    ]
    if len(md_shas) != 1 or not md_shas[0]:
        return False
    prev_valid = next((v for v in reversed(earlier) if v.get("schema_valid", True)), None)
    stale = {_md_sha(doc_key, dup, ticker)}
    if prev_valid is not None:
        stale.add(_md_sha(doc_key, prev_valid, ticker))
    return md_shas[0] in stale


def _is_raw(content: Any) -> bool:
    return isinstance(content, dict) and "_raw" in content


def _same_json(cur: dict[str, Any], prev: dict[str, Any]) -> bool:
    """Equal parsed content; invalid-JSON rows only by their full-text sha (``_raw`` is truncated)."""
    if _is_raw(cur["content"]) or _is_raw(prev["content"]):
        return (
            _is_raw(cur["content"])
            and _is_raw(prev["content"])
            and bool(cur["content_sha256"])
            and cur["content_sha256"] == prev["content_sha256"]
        )
    return _canon(cur["content"]) == _canon(prev["content"])


def find_spurious(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``(deletable_rows, notes)`` from one workspace's rows ordered by doc, version."""
    by_doc: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_doc.setdefault(r["doc_key"], []).append(r)
    company_valid = [r for r in by_doc.get("company", []) if r.get("schema_valid", True)]
    ticker = str(
        ((company_valid[-1]["content"] or {}) if company_valid else {}).get("ticker") or ""
    ).upper()

    deletable: list[dict[str, Any]] = []
    notes: list[str] = []
    for doc_key, versions in sorted(by_doc.items()):
        spurious_ids: set[str] = set()
        for i in range(1, len(versions)):
            prev, cur = versions[i - 1], versions[i]
            if (
                cur["source"] == "direct_edit"
                and cur["edited_path"] == f"data/{doc_key}.json"
                and _same_json(cur, prev)
            ):
                spurious_ids.add(cur["knowledge_version_id"])
            elif (
                prev["knowledge_version_id"] in spurious_ids
                and prev["edited_path"] == f"data/{doc_key}.json"
                and _is_stale_md_marker(doc_key, cur, prev, versions[: i - 1], ticker)
            ):
                spurious_ids.add(cur["knowledge_version_id"])
        if not spurious_ids:
            continue
        listed = sorted(
            v["version_no"] for v in versions if v["knowledge_version_id"] in spurious_ids
        )
        first = min(
            i for i, v in enumerate(versions) if v["knowledge_version_id"] in spurious_ids
        )
        tail = versions[first:]
        if not all(v["knowledge_version_id"] in spurious_ids for v in tail):
            notes.append(
                f"{doc_key}: duplicates {listed} are followed by versions that are not "
                "provably spurious; left alone"
            )
        elif any(v["files_written_at"] is not None for v in tail):
            notes.append(
                f"{doc_key}: REFUSED, duplicates {listed} include a version already "
                "written to the sandbox (files_written_at set); not deleting"
            )
        else:
            deletable.extend(tail)
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
                "WHERE workspace_id = %s AND knowledge_version_id = ANY(%s::uuid[]) "
                "AND files_written_at IS NULL",
                (workspace_id, expected_ids),
            )
            deleted = cur.rowcount
            cur.execute(f"ALTER TABLE dalton_knowledge_versions ENABLE TRIGGER {TRIGGER}")
            if deleted != len(expected_ids) or deleted != expect_count:
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
