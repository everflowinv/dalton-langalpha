#!/usr/bin/env python3
"""Read-only diagnostic script for Dalton data-job monitor artifact / source_ref conflicts.

Inspects ``dalton_monitors`` and ``share_links`` in ``COMPANY_MONITOR_WORKSPACE_ID``
against ``MONITOR_DATA_JOB_REGISTRY`` inside a ``READ ONLY`` transaction. Never writes
to the database; classifies active conflicts vs. unreferenced historical ``index.html``
artifacts and prints non-destructive remediation guidance.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import sys
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from psycopg.rows import dict_row  # noqa: E402

from src.dalton.monitors.data_job_registry import (  # noqa: E402
    MONITOR_DATA_JOB_REGISTRY,
    MonitorDataJobBinding,
    get_registered_monitor_bindings,
)
from src.dalton.monitors.db import COMPANY_MONITOR_WORKSPACE_ID  # noqa: E402
from src.server.database.pool import get_db_connection, get_or_create_pool  # noqa: E402


def _all_registered_bindings() -> list[MonitorDataJobBinding]:
    bindings: list[MonitorDataJobBinding] = []
    for job_key in MONITOR_DATA_JOB_REGISTRY:
        bindings.extend(get_registered_monitor_bindings(job_key))
    return bindings


def analyze_monitor_conflicts(
    *,
    monitor_rows: Sequence[Mapping[str, Any]],
    workspace_links: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare persisted DB rows against ``MONITOR_DATA_JOB_REGISTRY`` and classify conflicts."""
    registered = _all_registered_bindings()
    by_slug = {b.slug: b for b in registered}

    source_ref_mismatches: list[dict[str, Any]] = []
    legacy_entry_paths: list[dict[str, Any]] = []
    unregistered_monitors: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    by_source_ref: dict[str, list[str]] = {}

    for row in monitor_rows:
        slug = str(row.get("slug") or "")
        source_ref = row.get("source_ref")
        entry_path = str(row.get("entry_path") or "")
        monitor_id = str(row.get("monitor_id") or "")
        artifact_id = str(row.get("artifact_id") or "")
        seen_slugs.add(slug)
        if source_ref is not None:
            by_source_ref.setdefault(str(source_ref), []).append(slug)

        binding = by_slug.get(slug)
        if binding is None:
            unregistered_monitors.append(
                {
                    "monitor_id": monitor_id,
                    "slug": slug,
                    "source_ref": source_ref,
                    "entry_path": entry_path,
                    "artifact_id": artifact_id,
                }
            )
            continue

        expected_entry = f"{binding.slug}/{binding.entry_file}"
        if source_ref != binding.job_key:
            source_ref_mismatches.append(
                {
                    "monitor_id": monitor_id,
                    "slug": slug,
                    "actual_source_ref": source_ref,
                    "expected_source_ref": binding.job_key,
                    "entry_path": entry_path,
                    "artifact_id": artifact_id,
                }
            )
        if entry_path != expected_entry:
            legacy_entry_paths.append(
                {
                    "monitor_id": monitor_id,
                    "slug": slug,
                    "source_ref": source_ref,
                    "actual_entry_path": entry_path,
                    "expected_entry_path": expected_entry,
                    "artifact_id": artifact_id,
                    "published_seq": row.get("published_seq"),
                }
            )

    referenced_artifact_ids = {
        str(row.get("artifact_id") or "")
        for row in monitor_rows
        if row.get("artifact_id")
    }
    legacy_shared_links: list[dict[str, Any]] = []
    historical_shared_links: list[dict[str, Any]] = []
    for link in workspace_links:
        if str(link.get("path") or "") != "index.html":
            continue
        aid = str(link.get("artifact_id") or "")
        entry = {
            "artifact_id": aid,
            "path": str(link.get("path") or ""),
            "latest_seq": link.get("latest_seq"),
        }
        is_referenced = (
            bool(link.get("is_referenced"))
            if "is_referenced" in link
            else aid in referenced_artifact_ids
        )
        if is_referenced:
            legacy_shared_links.append(entry)
        else:
            historical_shared_links.append(entry)

    missing_registered = [
        {
            "job_key": b.job_key,
            "slug": b.slug,
            "expected_entry_path": f"{b.slug}/{b.entry_file}",
        }
        for b in registered
        if b.slug not in seen_slugs
    ]

    remediation_sql: list[str] = []
    if source_ref_mismatches:
        remediation_sql.append("-- 建议在维护窗口核对后手动执行（以下仅作参考，请先 SELECT 核实）：")
    for item in source_ref_mismatches:
        remediation_sql.append(
            f"-- 纠正 slug={item['slug']} 被覆盖的 source_ref（原={item['actual_source_ref']} -> 应为={item['expected_source_ref']}）:\n"
            f"UPDATE dalton_monitors SET source_ref = '{item['expected_source_ref']}', updated_at = NOW() "
            f"WHERE monitor_id = '{item['monitor_id']}'::uuid;"
        )
    for item in legacy_entry_paths:
        expected_job_key = (
            by_slug[item["slug"]].job_key
            if item["slug"] in by_slug
            else str(item.get("source_ref") or "<job_key>")
        )
        remediation_sql.append(
            f"-- slug={item['slug']}（job_key={expected_job_key}）仍指向旧路径 '{item['actual_entry_path']}'："
            f"重跑该数据任务即可自动迁移到 '{item['expected_entry_path']}'（保留面板 ID、成员权限、历史版本登记与实时数据）。"
        )
    covered_legacy_aids = {item["artifact_id"] for item in legacy_entry_paths}
    for item in legacy_shared_links:
        if item["artifact_id"] not in covered_legacy_aids:
            remediation_sql.append(
                f"-- 仍有面板引用旧共享资源 index.html（artifact_id={item['artifact_id']}）："
                "重跑该数据任务即可自动迁移。"
            )

    has_conflicts = bool(
        source_ref_mismatches or legacy_entry_paths or legacy_shared_links or unregistered_monitors
    )
    return {
        "ok": not has_conflicts,
        "has_conflicts": has_conflicts,
        "company_workspace_id": COMPANY_MONITOR_WORKSPACE_ID,
        "total_data_job_monitors": len(monitor_rows),
        "source_ref_mismatches": source_ref_mismatches,
        "legacy_entry_paths": legacy_entry_paths,
        "legacy_shared_links": legacy_shared_links,
        "historical_shared_links": historical_shared_links,
        "unregistered_monitors": unregistered_monitors,
        "missing_registered": missing_registered,
        "remediation_sql": remediation_sql,
    }


async def inspect_monitor_conflicts(conn: Any | None = None) -> dict[str, Any]:
    """Run read-only queries against Postgres and return the conflict diagnostic report."""

    async def _query(db_conn: Any) -> dict[str, Any]:
        async with db_conn.transaction():
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SET TRANSACTION READ ONLY")
                await cur.execute(
                    """
                    SELECT m.monitor_id::text AS monitor_id,
                           m.slug,
                           m.source_kind,
                           m.source_ref,
                           m.title,
                           m.category,
                           m.published_seq,
                           m.artifact_id::text AS artifact_id,
                           l.path AS entry_path
                      FROM dalton_monitors m
                      JOIN share_links l ON l.artifact_id = m.artifact_id
                     WHERE m.source_kind = 'data_job'
                     ORDER BY m.slug
                    """
                )
                monitor_rows = [dict(row) for row in await cur.fetchall()]
                await cur.execute(
                    """
                    SELECT l.artifact_id::text AS artifact_id,
                           l.path,
                           l.latest_seq,
                           EXISTS (
                               SELECT 1
                                 FROM dalton_monitors am
                                WHERE am.artifact_id = l.artifact_id
                           ) AS is_referenced
                      FROM share_links l
                     WHERE l.workspace_id = %s::uuid
                       AND l.kind = 'file'
                     ORDER BY l.path
                    """,
                    (COMPANY_MONITOR_WORKSPACE_ID,),
                )
                workspace_links = [dict(row) for row in await cur.fetchall()]
        return analyze_monitor_conflicts(
            monitor_rows=monitor_rows,
            workspace_links=workspace_links,
        )

    if conn is not None:
        return await _query(conn)

    pool = get_or_create_pool()
    await pool.open()
    try:
        async with get_db_connection() as owned_conn:
            return await _query(owned_conn)
    finally:
        await pool.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only check for Dalton data-job monitor artifact/source_ref conflicts."
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON diagnostic report.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    report = asyncio.run(inspect_monitor_conflicts())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Company Monitor Workspace: {report['company_workspace_id']}")
        print(f"Existing data_job monitors: {report['total_data_job_monitors']}")
        print(f"Conflicts detected: {'YES' if report['has_conflicts'] else 'NO'}")
        for label, key in (
            ("source_ref mismatches", "source_ref_mismatches"),
            ("legacy entry paths", "legacy_entry_paths"),
            ("referenced legacy index.html share_links", "legacy_shared_links"),
            ("historical unreferenced index.html share_links (info)", "historical_shared_links"),
            ("unregistered data_job monitors", "unregistered_monitors"),
            ("missing registered panels", "missing_registered"),
        ):
            items = report[key]
            print(f"- {label}: {len(items)}")
            for item in items:
                print(f"    {json.dumps(item, ensure_ascii=False)}")
        if report["remediation_sql"]:
            print("\nSuggested manual remediation:")
            for stmt in report["remediation_sql"]:
                print(stmt)
    return 1 if report["has_conflicts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
