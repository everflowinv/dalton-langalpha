"""Unit tests for per-user coverage rosters, rule groups, Chief of Staff tool, and earnings calendar mirror."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from psycopg import AsyncConnection, AsyncTransaction
from psycopg_pool import AsyncConnectionPool

from src.dalton.app import dalton_router
import src.dalton.autonomy.board as board_mod
import src.dalton.autonomy.preflight as preflight_mod
import src.dalton.autonomy.probes.sec as sec_probe_mod
import src.dalton.autonomy.system_jobs as system_jobs_mod
import src.dalton.autonomy.tracking as tracking_mod
import src.dalton.conversations.extractor as extractor_mod
import src.dalton.coverage as coverage_pkg
from src.dalton.coverage.models import (
    build_coverage_doc,
    company_to_row,
    dump_coverage_yaml,
    extract_row_group_ids,
    extract_row_stale_group_ids,
    row_to_company,
    validate_earnings_calendar_ledger,
)
import src.dalton.coverage.service as coverage_svc
import src.dalton.database.coverage as coverage_db
import src.dalton.database.watch as watch_db
from src.dalton.watchlist import policies as _wl_policies_orig_mod
from src.dalton.watchlist import reconciler as _wl_reconciler_orig_mod

_ORIG_INSERT_RECONCILE_JOB = _wl_policies_orig_mod.insert_reconcile_job
_ORIG_RECONCILE_JOB = _wl_reconciler_orig_mod.TierPolicyReconciler.reconcile_job
_ORIG_USER_WORKSPACE_TICKERS = coverage_svc._user_workspace_tickers
from src.dalton.tools import build_dalton_tools
from src.dalton.tools.coverage_tools import build_coverage_tools
from src.server.utils.api import get_current_user_id

REPO_ROOT = Path(__file__).resolve().parents[3]
COVERAGE_YAML_PATH = REPO_ROOT / "config" / "dalton" / "coverage.yaml"

USER_ALICE = "user-alice-111"
USER_BOB = "user-bob-222"


class _InMemoryCoverageStore:
    """In-memory multi-user store backing `src.dalton.database.coverage` in unit tests."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.groups: dict[tuple[str, str], dict[str, Any]] = {}
        self.mirrors: dict[str, dict[str, Any]] = {}
        self.lock_calls: list[str] = []
        self._seq = 0

    def _next_ts(self) -> datetime:
        self._seq += 1
        return datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc) + timedelta(microseconds=self._seq)

    async def acquire_coverage_advisory_lock(self, conn: Any, user_id: str) -> None:
        _ = conn
        self.lock_calls.append(str(user_id))

    async def list_coverage_rows(self, user_id: str, *, conn: Any = None) -> list[dict[str, Any]]:
        _ = conn
        uid = str(user_id).strip()
        items = [dict(v) for (u, _), v in self.rows.items() if u == uid]
        items.sort(
            key=lambda r: (
                r.get("added_at") or datetime.min.replace(tzinfo=timezone.utc),
                r.get("ticker") or "",
            )
        )
        return items

    async def list_all_coverage_rows(self, *, conn: Any = None) -> list[dict[str, Any]]:
        _ = conn
        return [dict(v) for v in self.rows.values()]

    async def get_coverage_row(
        self,
        user_id: str,
        ticker: str,
        *,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        _ = conn
        row = self.rows.get((str(user_id).strip(), str(ticker).strip().upper()))
        return dict(row) if row is not None else None

    async def count_coverage_rows(self, user_id: str, *, conn: Any = None) -> int:
        _ = conn
        uid = str(user_id).strip()
        return sum(1 for (u, _) in self.rows if u == uid)

    async def upsert_coverage_rows(
        self,
        user_id: str,
        rows: list[dict[str, Any]],
        *,
        conn: Any = None,
        expected_unique_count: int | None = None,
    ) -> tuple[int, list[dict[str, Any]]]:
        _ = conn
        uid = str(user_id).strip()
        prev_count = await self.count_coverage_rows(uid)
        snapshot = {k: dict(v) for k, v in self.rows.items()}
        upserted: list[dict[str, Any]] = []
        seen_tickers: set[str] = set()
        for row in rows:
            sym = str(row["ticker"]).strip().upper()
            seen_tickers.add(sym)
            key = (uid, sym)
            now = self._next_ts()
            existing = self.rows.get(key)
            if existing is None:
                stored = {
                    "user_id": uid,
                    "ticker": sym,
                    "name": row.get("name") or sym,
                    "tier": str(row.get("tier") or "watch").lower(),
                    "market": row.get("market") or "US",
                    "peer_group": row.get("peer_group") or "",
                    "peers": list(row.get("peers") or []),
                    "aliases": dict(row.get("aliases") or {}),
                    "earnings": dict(row.get("earnings") or {}),
                    "extra": dict(row.get("extra") or {}),
                    "source": row.get("source") or "chat",
                    "group_id": str(row["group_id"]) if row.get("group_id") else None,
                    "added_at": now,
                    "updated_at": now,
                }
            else:
                incoming_src = str(row.get("source") or "chat").lower()
                eff_tier = (
                    existing["tier"]
                    if incoming_src == "group"
                    else str(row.get("tier") or existing.get("tier") or "watch").lower()
                )
                eff_source = (
                    existing["source"]
                    if incoming_src == "group" and existing.get("source") != "group"
                    else incoming_src
                )
                stored = {
                    "user_id": uid,
                    "ticker": sym,
                    "name": row.get("name") or existing.get("name") or sym,
                    "tier": eff_tier,
                    "market": row.get("market") or existing.get("market") or "US",
                    "peer_group": row.get("peer_group") if row.get("peer_group") is not None else existing.get("peer_group"),
                    "peers": list(row.get("peers") or []),
                    "aliases": dict(row.get("aliases") or {}),
                    "earnings": dict(row.get("earnings") or {}),
                    "extra": dict(row.get("extra") or {}),
                    "source": eff_source,
                    "group_id": str(row["group_id"]) if row.get("group_id") else existing.get("group_id"),
                    "added_at": existing["added_at"],
                    "updated_at": now,
                }
            self.rows[key] = stored
            upserted.append(dict(stored))
        if expected_unique_count is not None and (
            len(upserted) != expected_unique_count or len(seen_tickers) != expected_unique_count
        ):
            self.rows = snapshot
            raise AssertionError(
                f"Written unique row count ({len(seen_tickers)}) does not match expected count ({expected_unique_count})"
            )
        return prev_count, upserted

    async def update_coverage_tier(
        self,
        user_id: str,
        tickers: list[str],
        tier: str,
        *,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        _ = conn
        uid = str(user_id).strip()
        updated: list[dict[str, Any]] = []
        for t in tickers:
            sym = str(t).strip().upper()
            row = self.rows.get((uid, sym))
            if row is not None:
                row["tier"] = str(tier).lower()
                row["updated_at"] = self._next_ts()
                updated.append(dict(row))
        return updated

    async def delete_coverage_tickers(
        self,
        user_id: str,
        tickers: list[str],
        *,
        conn: Any = None,
    ) -> list[str]:
        _ = conn
        uid = str(user_id).strip()
        removed: list[str] = []
        for t in tickers:
            sym = str(t).strip().upper()
            if (uid, sym) in self.rows:
                del self.rows[(uid, sym)]
                removed.append(sym)
        return removed

    async def mark_coverage_rows_group_stale(
        self,
        user_id: str,
        tickers: list[str],
        *,
        group_id: str | None = None,
        stale: bool = True,
        conn: Any = None,
    ) -> list[str]:
        _ = conn
        uid = str(user_id).strip()
        gid_clean = str(group_id).strip() if group_id else ""
        changed: list[str] = []
        for t in tickers:
            sym = str(t).strip().upper()
            row = self.rows.get((uid, sym))
            if row is not None:
                row_gids = extract_row_group_ids(row)
                stale_gids = [
                    g for g in extract_row_stale_group_ids(row) if not row_gids or g in row_gids
                ]
                target_gid = gid_clean or str(row.get("group_id") or "").strip() or (
                    row_gids[0] if row_gids else ""
                )
                if stale:
                    if target_gid and target_gid not in stale_gids:
                        stale_gids.append(target_gid)
                else:
                    if target_gid:
                        stale_gids = [g for g in stale_gids if g != target_gid]
                    else:
                        stale_gids = []
                extra = dict(row.get("extra") or {})
                extra.pop("group_stale", None)
                if stale_gids:
                    extra["group_stale_ids"] = stale_gids
                else:
                    extra.pop("group_stale_ids", None)
                row["extra"] = extra
                row["updated_at"] = self._next_ts()
                changed.append(sym)
        return changed

    async def list_coverage_groups(self, user_id: str, *, conn: Any = None) -> list[dict[str, Any]]:
        _ = conn
        uid = str(user_id).strip()
        return [dict(g) for (u, _), g in self.groups.items() if u == uid]

    async def get_coverage_group(
        self,
        user_id: str,
        group_id: str,
        *,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        _ = conn
        g = self.groups.get((str(user_id).strip(), str(group_id).strip()))
        return dict(g) if g is not None else None

    async def insert_coverage_group(
        self,
        user_id: str,
        *,
        label: str,
        criteria: Any,
        default_tier: str = "watch",
        auto_refresh: bool = True,
        last_resolved_at: datetime | None = None,
        group_id: str | None = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        _ = conn
        uid = str(user_id).strip()
        gid = str(group_id or f"grp-{len(self.groups) + 1:04d}")
        now = self._next_ts()
        row = {
            "group_id": gid,
            "user_id": uid,
            "label": label,
            "criteria": dict(criteria or {}),
            "default_tier": str(default_tier).lower(),
            "auto_refresh": bool(auto_refresh),
            "last_resolved_at": last_resolved_at or now,
            "created_at": now,
        }
        self.groups[(uid, gid)] = row
        return dict(row)

    async def update_coverage_group_resolved(
        self,
        user_id: str,
        group_id: str,
        *,
        last_resolved_at: datetime | None = None,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        _ = conn
        key = (str(user_id).strip(), str(group_id).strip())
        if key not in self.groups:
            return None
        self.groups[key]["last_resolved_at"] = last_resolved_at or self._next_ts()
        return dict(self.groups[key])

    async def delete_coverage_group_and_exclusive_members(
        self,
        user_id: str,
        group_id: str,
        *,
        conn: Any = None,
    ) -> dict[str, Any]:
        _ = conn
        uid = str(user_id).strip()
        gid = str(group_id).strip()
        if (uid, gid) not in self.groups:
            return {"deleted": False, "group_id": gid, "removed_tickers": []}
        valid_other_group_ids = {
            g for (u, g) in self.groups.keys() if u == uid and g != gid
        }
        label = self.groups[(uid, gid)].get("label")
        removed: list[str] = []
        kept_shared: list[str] = []
        kept_manual: list[str] = []
        for key in list(self.rows.keys()):
            if key[0] != uid:
                continue
            row = self.rows[key]
            gids = extract_row_group_ids(row)
            if gid not in gids and str(row.get("group_id") or "") != gid:
                continue
            remaining = [g for g in gids if g != gid and g in valid_other_group_ids]
            src = str(row.get("source") or "chat").strip().lower()
            if src == "group" and not remaining:
                removed.append(key[1])
                del self.rows[key]
            else:
                extra = dict(row.get("extra") or {})
                stale_gids = [
                    g for g in extract_row_stale_group_ids(row) if g != gid and g in remaining
                ]
                extra.pop("group_stale", None)
                if stale_gids:
                    extra["group_stale_ids"] = stale_gids
                else:
                    extra.pop("group_stale_ids", None)
                if remaining:
                    extra["group_ids"] = remaining
                else:
                    extra.pop("group_ids", None)
                row["extra"] = extra
                row["group_id"] = remaining[0] if remaining else None
                row["updated_at"] = self._next_ts()
                if src == "group":
                    kept_shared.append(key[1])
                else:
                    kept_manual.append(key[1])
        del self.groups[(uid, gid)]
        return {
            "deleted": True,
            "group_id": gid,
            "label": label,
            "removed_tickers": sorted(removed),
            "kept_shared_tickers": sorted(kept_shared),
            "kept_manual_tickers": sorted(kept_manual),
        }

    async def get_earnings_calendar_mirror(
        self,
        user_id: str,
        *,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        _ = conn
        m = self.mirrors.get(str(user_id).strip())
        return dict(m) if m is not None else None

    async def upsert_earnings_calendar_mirror(
        self,
        user_id: str,
        ledger: dict[str, Any],
        *,
        conn: Any = None,
    ) -> dict[str, Any]:
        _ = conn
        uid = str(user_id).strip()
        row = {
            "user_id": uid,
            "ledger": dict(ledger),
            "published_at": self._next_ts(),
        }
        self.mirrors[uid] = row
        return dict(row)


@pytest.fixture(autouse=True)
def _reset_extractor_alias_cache() -> Any:
    extractor_mod.invalidate_ticker_company_aliases_cache()
    yield
    extractor_mod.invalidate_ticker_company_aliases_cache()


def _wire_coverage_store(monkeypatch: pytest.MonkeyPatch, store: _InMemoryCoverageStore) -> AsyncConnectionPool:
    extractor_mod.invalidate_ticker_company_aliases_cache()
    fake_tx = create_autospec(AsyncTransaction, instance=True)
    fake_tx.__aenter__.return_value = fake_tx
    fake_tx.__aexit__.return_value = False

    fake_conn = create_autospec(AsyncConnection, instance=True)
    fake_conn.transaction.return_value = fake_tx

    fake_pool = create_autospec(AsyncConnectionPool, instance=True)
    fake_pool.open.return_value = None
    fake_pool.close.return_value = None

    @asynccontextmanager
    async def _fake_conn_cm(conn: Any = None):
        yield conn if conn is not None else fake_conn

    import src.server.database.pool as pool_mod

    monkeypatch.setattr(
        pool_mod,
        "get_or_create_pool",
        create_autospec(pool_mod.get_or_create_pool, return_value=fake_pool),
    )
    monkeypatch.setattr(
        coverage_db,
        "get_db_connection",
        create_autospec(coverage_db.get_db_connection, side_effect=_fake_conn_cm),
    )
    monkeypatch.setattr(
        pool_mod,
        "get_db_connection",
        create_autospec(pool_mod.get_db_connection, side_effect=_fake_conn_cm),
    )

    for fn_name in (
        "acquire_coverage_advisory_lock",
        "list_coverage_rows",
        "list_all_coverage_rows",
        "get_coverage_row",
        "count_coverage_rows",
        "upsert_coverage_rows",
        "update_coverage_tier",
        "delete_coverage_tickers",
        "mark_coverage_rows_group_stale",
        "list_coverage_groups",
        "get_coverage_group",
        "insert_coverage_group",
        "update_coverage_group_resolved",
        "delete_coverage_group_and_exclusive_members",
        "get_earnings_calendar_mirror",
        "upsert_earnings_calendar_mirror",
    ):
        orig = getattr(coverage_db, fn_name)
        bound = getattr(store, fn_name)
        monkeypatch.setattr(
            coverage_db,
            fn_name,
            create_autospec(orig, side_effect=bound),
        )

    # Wire in-memory watchlist outbox & reconciler hooks so legacy coverage entry points
    # converging onto WatchlistMutationService run against the in-memory store.
    import src.dalton.watchlist.policies as wl_policies_mod
    import src.dalton.watchlist.reconciler as wl_reconciler_mod
    import src.dalton.watchlist.service as wl_service_mod

    store_jobs: dict[str, dict[str, Any]] = {}

    async def _store_insert_job(
        uid: str,
        *,
        trigger_action: str,
        target_policy_versions: Any,
        pending_tickers: Any,
        override_user_pause: bool = False,
        job_context: Any = None,
        job_id: str | None = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        _ = (override_user_pause, conn)
        jid = job_id or f"store-job-{len(store_jobs) + 1}"
        job = {
            "job_id": jid,
            "user_id": uid,
            "trigger_action": trigger_action,
            "target_policy_versions": dict(target_policy_versions),
            "pending_tickers": list(pending_tickers),
            "completed_tickers": [],
            "failed_items": [],
            "job_context": dict(job_context or {}),
            "yaml_synced": False,
            "status": "pending",
        }
        store_jobs[jid] = job
        return dict(job)

    async def _store_reconcile_job(
        self: Any,
        uid: str,
        jid: str,
        *,
        sync_home: bool = True,
        _preclaimed_job: Any = None,
    ) -> dict[str, Any]:
        _ = (self, _preclaimed_job)
        job = store_jobs[jid]
        job["completed_tickers"] = list(job["pending_tickers"])
        job["pending_tickers"] = []
        if sync_home:
            synced = bool(await coverage_svc.sync_user_coverage_to_home(uid))
            job["yaml_synced"] = synced
        job["status"] = "completed" if job["yaml_synced"] or not sync_home else "partial"
        return {
            "ok": job["status"] == "completed",
            "job_id": jid,
            "status": job["status"],
            "yaml_synced": job["yaml_synced"],
            "failed_items": [],
        }

    async def _store_policies(uid: str, *, conn: Any = None):
        _ = (uid, conn)
        return dict(wl_policies_mod.DEFAULT_TIER_POLICIES)

    orig_get_policies = wl_policies_mod.get_user_tier_policies
    orig_insert_job = wl_policies_mod.insert_reconcile_job
    policies_mock = create_autospec(orig_get_policies, side_effect=_store_policies)
    insert_job_mock = create_autospec(orig_insert_job, side_effect=_store_insert_job)

    monkeypatch.setattr(wl_policies_mod, "get_user_tier_policies", policies_mock)
    monkeypatch.setattr(wl_service_mod, "get_user_tier_policies", policies_mock)
    monkeypatch.setattr(wl_policies_mod, "insert_reconcile_job", insert_job_mock)
    monkeypatch.setattr(wl_service_mod, "insert_reconcile_job", insert_job_mock)
    monkeypatch.setattr(
        wl_reconciler_mod.TierPolicyReconciler,
        "reconcile_job",
        create_autospec(wl_reconciler_mod.TierPolicyReconciler.reconcile_job, side_effect=_store_reconcile_job),
    )
    async def _proxy_validate(tickers: Any):
        return await coverage_svc.validate_and_enrich_tickers(tickers)

    async def _proxy_preview_group(label: str, criteria: Any):
        return await coverage_svc.preview_coverage_group(label, criteria)

    async def _proxy_ensure_cal(uid: str):
        return await coverage_svc.ensure_user_earnings_calendar_automation(uid)

    async def _proxy_user_ws(uid: str):
        return await coverage_svc._user_workspace_tickers(uid)

    monkeypatch.setattr(
        wl_service_mod,
        "validate_and_enrich_tickers",
        create_autospec(coverage_svc.validate_and_enrich_tickers, side_effect=_proxy_validate),
    )
    monkeypatch.setattr(
        wl_service_mod,
        "preview_coverage_group",
        create_autospec(coverage_svc.preview_coverage_group, side_effect=_proxy_preview_group),
    )
    monkeypatch.setattr(
        wl_service_mod,
        "ensure_user_earnings_calendar_automation",
        create_autospec(coverage_svc.ensure_user_earnings_calendar_automation, side_effect=_proxy_ensure_cal),
    )
    monkeypatch.setattr(
        wl_service_mod,
        "_user_workspace_tickers",
        create_autospec(coverage_svc._user_workspace_tickers, side_effect=_proxy_user_ws),
    )
    monkeypatch.setattr(
        wl_service_mod,
        "resolve_company_workspace_id",
        create_autospec(
            wl_reconciler_mod.resolve_company_workspace_id,
            return_value=None,
        ),
    )
    return fake_pool


def test_coverage_yaml_lossless_round_trip() -> None:
    """1. Every company in `config/dalton/coverage.yaml` round-trips losslessly through `company_to_row` and `row_to_company`."""
    raw_doc = yaml.safe_load(COVERAGE_YAML_PATH.read_text(encoding="utf-8"))
    companies = raw_doc["companies"]
    assert len(companies) == 105

    rows = [company_to_row(c, user_id=USER_ALICE, source="seed") for c in companies]
    reconstructed = [row_to_company(r) for r in rows]
    assert reconstructed == companies
    for orig_c, rec_c in zip(companies, reconstructed, strict=True):
        assert list(orig_c.keys()) == list(rec_c.keys())

    rendered_doc = build_coverage_doc(
        reconstructed,
        as_of=raw_doc["as_of"],
        generated_at=raw_doc["generated_at"],
        sources=raw_doc["sources"],
    )
    reparsed = yaml.safe_load(dump_coverage_yaml(rendered_doc))
    assert reparsed == raw_doc


@pytest.mark.asyncio
async def test_seed_script_dry_run_and_apply_and_user_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2. `seed_user_coverage.py` is dry-run by default, writes 105 rows under `--apply`, and isolates users."""
    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)
    monkeypatch.setattr(
        coverage_svc,
        "sync_user_coverage_to_home",
        create_autospec(coverage_svc.sync_user_coverage_to_home, return_value=True),
    )
    monkeypatch.setattr(
        coverage_svc,
        "ensure_user_earnings_calendar_automation",
        create_autospec(
            coverage_svc.ensure_user_earnings_calendar_automation,
            return_value={
                "ok": True,
                "action": "created",
                "automation_id": "auto-cal-1",
                "name": "dalton-earnings-calendar",
            },
        ),
    )

    from scripts.dalton.seed_user_coverage import run_seed

    # Default dry-run: 0 rows written to DB
    res_dry = await run_seed(user_id=USER_ALICE, yaml_path=COVERAGE_YAML_PATH, apply=False)
    assert res_dry["ok"] is True
    assert res_dry["mode"] == "dry_run"
    assert res_dry["planned_count"] == 105
    assert len(store.rows) == 0

    # With --apply: 105 rows written for Alice, 0 for Bob
    res_apply = await run_seed(user_id=USER_ALICE, yaml_path=COVERAGE_YAML_PATH, apply=True)
    assert res_apply["ok"] is True
    assert res_apply["mode"] == "applied"
    assert res_apply["written_count"] == 105
    alice_cov = await coverage_pkg.get_user_coverage(USER_ALICE)
    bob_cov = await coverage_pkg.get_user_coverage(USER_BOB)
    assert len(alice_cov["companies"]) == 105
    assert alice_cov["tiers"]["core"]["count"] == 27
    assert alice_cov["tiers"]["watch"]["count"] == 78
    assert len(bob_cov["companies"]) == 0

    assert await coverage_pkg.get_user_tier(USER_ALICE, "META") == "core"
    assert await coverage_pkg.get_user_tier(USER_ALICE, "PLTR") == "watch"
    assert await coverage_pkg.get_user_tier(USER_BOB, "META") is None

    alice_aliases = await coverage_pkg.get_user_aliases(USER_ALICE)
    assert "Meta Platforms" in alice_aliases["META"]
    # Union fallback when user_id is None still includes supplemental + Alice's aliases
    union_aliases = await coverage_pkg.get_user_aliases(None)
    assert "META" in union_aliases


@pytest.mark.asyncio
async def test_four_server_callers_use_per_user_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3. `board.py`, `tracking.py`, `probes/sec.py`, and `extractor.py` read per-user coverage."""
    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)

    await store.upsert_coverage_rows(
        USER_ALICE,
        [
            company_to_row(
                {
                    "ticker": "ACME",
                    "name": "Acme Robotics Corp",
                    "tier": "core",
                    "market": "US",
                    "cik": "0001234567",
                    "aliases": {"search_names": ["Acme Robotics", "ACME"]},
                },
                user_id=USER_ALICE,
                source="chat",
            )
        ],
    )

    # 1) board.py
    alice_map = await board_mod._load_user_coverage_map(USER_ALICE)
    bob_map = await board_mod._load_user_coverage_map(USER_BOB)
    assert "ACME" in alice_map
    assert "ACME" not in bob_map

    # 2) tracking.py
    assert await tracking_mod.resolve_default_tier("ACME", user_id=USER_ALICE) == "core"
    assert await tracking_mod.resolve_default_tier("ACME", user_id=USER_BOB) == "watch"

    # 3) probes/sec.py
    assert await sec_probe_mod.resolve_company_cik("ACME", allow_network=False, user_id=USER_ALICE) == "0001234567"
    assert await sec_probe_mod.resolve_company_cik("ACME", allow_network=False, user_id=USER_BOB) is None

    # 4) extractor.py
    alice_ext_aliases = extractor_mod._load_ticker_company_aliases(USER_ALICE)
    assert "Acme Robotics" in alice_ext_aliases.get("ACME", ())


@pytest.mark.asyncio
async def test_cos_coverage_tool_binding_actions_and_quarterly_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4. `dalton_coverage` tool is bound only to `chief_of_staff`, supports all 7 actions, and preflight refreshes due groups (>85d) + renders Home `coverage.yaml`."""
    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)

    # Role binding check
    cos_tool_names = {t.name for t in build_dalton_tools("chief_of_staff", enabled=True)}
    analyst_tool_names = {t.name for t in build_dalton_tools("analyst", enabled=True)}
    assert "dalton_coverage" in cos_tool_names
    assert "dalton_coverage" not in analyst_tool_names

    home_writes: list[str] = []

    async def _fake_sync_home(
        user_id: str,
        *,
        yaml_text: str | None = None,
        backend: Any = None,
        rel_path: str = "dalton/coverage.yaml",
    ) -> bool:
        _ = (backend, rel_path)
        rendered = yaml_text if yaml_text is not None else await coverage_svc.render_coverage_yaml(user_id)
        home_writes.append(rendered)
        return True

    monkeypatch.setattr(
        coverage_svc,
        "sync_user_coverage_to_home",
        create_autospec(coverage_svc.sync_user_coverage_to_home, side_effect=_fake_sync_home),
    )
    monkeypatch.setattr(
        preflight_mod,
        "sync_user_coverage_to_home",
        create_autospec(preflight_mod.sync_user_coverage_to_home, side_effect=_fake_sync_home),
    )

    auto_created_calls: list[str] = []

    async def _fake_ensure_cal_auto(user_id: str) -> dict[str, Any]:
        auto_created_calls.append(user_id)
        return {"ok": True, "action": "created" if len(auto_created_calls) == 1 else "exists", "name": "dalton-earnings-calendar"}

    monkeypatch.setattr(
        coverage_svc,
        "ensure_user_earnings_calendar_automation",
        create_autospec(
            coverage_svc.ensure_user_earnings_calendar_automation,
            side_effect=_fake_ensure_cal_auto,
        ),
    )

    async def _fake_validate_ticker(ticker: str) -> dict[str, Any] | None:
        sym = ticker.strip().upper()
        if sym in {"NVDA", "AVGO", "AMD", "QCOM"}:
            return {
                "ticker": sym,
                "name": f"{sym} Corp",
                "market": "US",
                "sector": "Technology",
                "industry": "Semiconductors",
            }
        return None

    monkeypatch.setattr(
        coverage_svc,
        "validate_and_enrich_ticker",
        create_autospec(coverage_svc.validate_and_enrich_ticker, side_effect=_fake_validate_ticker),
    )

    screener_results: list[dict[str, Any]] = [
        {"symbol": "NVDA", "companyName": "NVIDIA Corp", "exchangeShortName": "NASDAQ", "sector": "Technology", "industry": "Semiconductors"},
        {"symbol": "AVGO", "companyName": "Broadcom Inc", "exchangeShortName": "NASDAQ", "sector": "Technology", "industry": "Semiconductors"},
        {"symbol": "AMD", "companyName": "Advanced Micro Devices", "exchangeShortName": "NASDAQ", "sector": "Technology", "industry": "Semiconductors"},
    ]

    async def _fake_screener(filters: Any) -> list[dict[str, Any]]:
        _ = filters
        return list(screener_results)

    monkeypatch.setattr(
        coverage_svc,
        "_fetch_yf_screener_rows",
        create_autospec(coverage_svc._fetch_yf_screener_rows, side_effect=_fake_screener),
    )
    monkeypatch.setattr(
        coverage_svc,
        "_user_workspace_tickers",
        create_autospec(
            _ORIG_USER_WORKSPACE_TICKERS,
            return_value={"NVDA": "ws-nvda-123"},
        ),
    )

    import src.dalton.tools.coverage_tools as cov_tools_mod
    import src.dalton.watchlist.policies as wl_policies_mod
    import src.dalton.watchlist.reconciler as wl_reconciler_mod
    import src.dalton.watchlist.service as wl_service_mod
    from src.tools.secretary import _commands as sec_commands

    reconcile_jobs_store: dict[str, dict[str, Any]] = {}
    reconciled_batches: list[list[str]] = []

    async def _fake_insert_job(
        uid: str,
        *,
        trigger_action: str,
        target_policy_versions: Any,
        pending_tickers: Any,
        override_user_pause: bool = False,
        job_context: Any = None,
        job_id: str | None = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        _ = (override_user_pause, job_context, conn)
        jid = job_id or f"job-{len(reconcile_jobs_store) + 1}"
        job = {
            "job_id": jid,
            "user_id": uid,
            "trigger_action": trigger_action,
            "target_policy_versions": dict(target_policy_versions),
            "pending_tickers": list(pending_tickers),
            "completed_tickers": [],
            "failed_items": [],
            "yaml_synced": False,
            "status": "pending",
        }
        reconcile_jobs_store[jid] = job
        return dict(job)

    async def _fake_reconcile_job(
        self: Any,
        uid: str,
        jid: str,
        *,
        sync_home: bool = True,
        _preclaimed_job: Any = None,
    ) -> dict[str, Any]:
        _ = (self, _preclaimed_job)
        job = reconcile_jobs_store[jid]
        reconciled_batches.append(list(job["pending_tickers"]))
        job["completed_tickers"] = list(job["pending_tickers"])
        job["pending_tickers"] = []
        if sync_home:
            await _fake_sync_home(uid)
            job["yaml_synced"] = True
        job["status"] = "completed"
        return {
            "ok": True,
            "job_id": jid,
            "status": "completed",
            "yaml_synced": job["yaml_synced"],
            "failed_items": [],
        }

    hitl_mock = create_autospec(
        sec_commands.hitl_confirm,
        return_value=(True, {"decisions": [{"type": "approve"}]}),
    )
    monkeypatch.setattr(cov_tools_mod, "hitl_confirm", hitl_mock)
    monkeypatch.setattr(
        coverage_svc,
        "_user_workspace_tickers",
        create_autospec(
            _ORIG_USER_WORKSPACE_TICKERS,
            return_value={"NVDA": "ws-nvda-123"},
        ),
    )
    monkeypatch.setattr(
        wl_service_mod,
        "resolve_company_workspace_id",
        create_autospec(
            wl_reconciler_mod.resolve_company_workspace_id,
            side_effect=lambda uid, sym, *, cov_row=None, conn=None: (
                "ws-nvda-123" if str(sym).upper() == "NVDA" else None
            ),
        ),
    )
    monkeypatch.setattr(
        wl_service_mod,
        "resolve_workspace_tracking_owner",
        create_autospec(
            wl_service_mod.resolve_workspace_tracking_owner,
            return_value=USER_ALICE,
        ),
    )
    monkeypatch.setattr(
        wl_service_mod,
        "list_all_user_automations",
        create_autospec(wl_service_mod.list_all_user_automations, return_value=[]),
    )
    monkeypatch.setattr(
        wl_policies_mod,
        "list_unfinished_reconcile_jobs",
        create_autospec(wl_policies_mod.list_unfinished_reconcile_jobs, return_value=[]),
    )
    fake_insert_spec = create_autospec(_ORIG_INSERT_RECONCILE_JOB, side_effect=_fake_insert_job)
    fake_reconcile_spec = create_autospec(_ORIG_RECONCILE_JOB, side_effect=_fake_reconcile_job)
    monkeypatch.setattr(wl_service_mod, "insert_reconcile_job", fake_insert_spec)
    monkeypatch.setattr(wl_policies_mod, "insert_reconcile_job", fake_insert_spec)
    monkeypatch.setattr(wl_reconciler_mod.TierPolicyReconciler, "reconcile_job", fake_reconcile_spec)

    tool = build_coverage_tools(role="chief_of_staff")[0]
    cfg = {"configurable": {"user_id": USER_ALICE}}
    call_seq = 0

    async def _invoke_cov(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal call_seq
        call_seq += 1
        res = await tool.ainvoke(
            {
                "name": "dalton_coverage",
                "id": f"tc-cov-{call_seq}",
                "type": "tool_call",
                "args": args,
            },
            config=cfg,
        )
        return json.loads(res.update["messages"][0].content)

    # 1) add specific tickers (one valid, one invalid) -> ≤5 net-new tickers is exempt from confirmation
    #    and non-empty roster triggers idempotent ensure of dalton-earnings-calendar
    add_out = await _invoke_cov({"action": "add", "tickers": ["NVDA", "FAKE999"], "tier": "core"})
    assert add_out["ok"] is True
    assert [x["ticker"] for x in add_out["added"]] == ["NVDA"]
    assert add_out["not_found"] == ["FAKE999"]
    assert auto_created_calls == [USER_ALICE]
    assert len(home_writes) >= 1
    assert hitl_mock.call_count == 0

    # 2) set_tier — triggers confirmation and writes outbox + reconciles NVDA
    reconciled_batches.clear()
    tier_out = await _invoke_cov({"action": "set_tier", "tickers": ["NVDA"], "tier": "watch"})
    assert tier_out["ok"] is True
    assert tier_out["updated"] == ["NVDA"]
    assert (await coverage_pkg.get_user_tier(USER_ALICE, "NVDA")) == "watch"
    assert hitl_mock.call_count == 1
    assert reconciled_batches == [["NVDA"]]
    # A ticker outside the roster is reported not_found and its tasks are left alone.
    reconciled_batches.clear()
    miss_out = await _invoke_cov({"action": "set_tier", "tickers": ["NVDA", "ZZZZ"], "tier": "watch"})
    assert "ZZZZ" in miss_out["not_found"]
    assert reconciled_batches == [["NVDA"]]
    # Set back to core so we verify group sync preserves user's manual tier
    await _invoke_cov({"action": "set_tier", "tickers": ["NVDA"], "tier": "core"})

    # 3) preview_group -> does NOT write to DB
    prev_out = await _invoke_cov(
        {
            "action": "preview_group",
            "label": "美股半导体大票",
            "criteria": {"country": "US", "industry": "Semiconductors", "market_cap_min": 50_000_000_000},
        }
    )
    assert prev_out["ok"] is True
    assert prev_out["preview_only"] is True
    assert [c["ticker"] for c in prev_out["companies"]] == ["NVDA", "AVGO", "AMD"]
    assert await store.count_coverage_rows(USER_ALICE) == 1  # Still only NVDA

    # 4) add_group -> writes group + AVGO & AMD with source='group', while NVDA keeps tier='core' & source='chat'
    grp_out = await _invoke_cov(
        {
            "action": "add_group",
            "label": "美股半导体大票",
            "criteria": {"country": "US", "industry": "Semiconductors"},
            "default_tier": "watch",
        }
    )
    assert grp_out["ok"] is True
    group_id = grp_out["group"]["group_id"]
    assert await store.count_coverage_rows(USER_ALICE) == 3
    assert (await store.get_coverage_row(USER_ALICE, "NVDA"))["tier"] == "core"
    assert (await store.get_coverage_row(USER_ALICE, "NVDA"))["source"] == "chat"
    assert (await store.get_coverage_row(USER_ALICE, "AVGO"))["source"] == "group"

    # 5) Quarterly preflight refresh (>85 days since last_resolved_at):
    # Suppose QCOM enters the screener and AMD drops out:
    # - QCOM is added with default_tier='watch'
    # - AMD is NOT deleted, but marked with extra.group_stale = True
    store.groups[(USER_ALICE, group_id)]["last_resolved_at"] = datetime.now(timezone.utc) - timedelta(days=90)
    screener_results[:] = [
        {"symbol": "NVDA", "companyName": "NVIDIA Corp", "exchangeShortName": "NASDAQ"},
        {"symbol": "AVGO", "companyName": "Broadcom Inc", "exchangeShortName": "NASDAQ"},
        {"symbol": "QCOM", "companyName": "Qualcomm Inc", "exchangeShortName": "NASDAQ"},
    ]

    monkeypatch.setattr(
        preflight_mod,
        "verify_workspace_access",
        create_autospec(
            preflight_mod.verify_workspace_access,
            return_value={"workspace_id": "ws-home-alice", "user_id": USER_ALICE},
        ),
    )
    monkeypatch.setattr(
        preflight_mod,
        "_check_budget_available",
        create_autospec(preflight_mod._check_budget_available, return_value=(True, None)),
    )
    monkeypatch.setattr(
        watch_db,
        "expire_old_signals",
        create_autospec(watch_db.expire_old_signals, return_value=0),
    )
    monkeypatch.setattr(
        watch_db,
        "get_watch_state",
        create_autospec(watch_db.get_watch_state, return_value={}),
    )
    monkeypatch.setattr(
        watch_db,
        "upsert_signal",
        create_autospec(
            watch_db.upsert_signal,
            return_value=({"signal_id": "sig-cal-1"}, True),
        ),
    )
    monkeypatch.setattr(
        watch_db,
        "claim_signals_woken",
        create_autospec(
            watch_db.claim_signals_woken,
            return_value=[{"signal_id": "sig-cal-1"}],
        ),
    )
    monkeypatch.setattr(
        watch_db,
        "insert_preflight_log",
        create_autospec(watch_db.insert_preflight_log, return_value=None),
    )

    pf_res = await preflight_mod.preflight_check(
        {
            "automation_id": "auto-cal-1",
            "user_id": USER_ALICE,
            "workspace_id": "ws-home-alice",
            "name": "dalton-earnings-calendar",
            "trigger_type": "cron",
            "cron_expression": "0 9 * * 6",
        },
        "exec-cal-1",
        persist=True,
    )
    assert pf_res.should_wake is True
    assert "<coverage_group_refresh>" in (pf_res.wake_context or "")
    assert "QCOM" in (pf_res.wake_context or "")
    assert "AMD" in (pf_res.wake_context or "")
    amd_row = await store.get_coverage_row(USER_ALICE, "AMD")
    assert amd_row is not None
    assert group_id in (amd_row["extra"].get("group_stale_ids") or [])
    assert "group_stale" not in amd_row["extra"]

    # 6) list shows stale_tickers
    list_out = await _invoke_cov({"action": "list"})
    assert "AMD" in list_out["stale_tickers"]

    # 7) remove_group removes group-only entries (AVGO, AMD, QCOM) and keeps NVDA (added via chat)
    rm_grp_out = await _invoke_cov({"action": "remove_group", "group_id": group_id})
    assert rm_grp_out["ok"] is True
    assert set(rm_grp_out["removed_tickers"]) == {"AMD", "AVGO", "QCOM"}
    assert await store.get_coverage_row(USER_ALICE, "NVDA") is not None

    # 8) remove ticker with active workspace warns that workspace and history are preserved while tasks pause
    rm_out = await _invoke_cov({"action": "remove", "tickers": ["NVDA"]})
    assert rm_out["ok"] is True
    assert rm_out["removed"] == ["NVDA"]
    assert "workspace 和历史文档不会被删除" in (rm_out["workspace_notice"] or "")


@pytest.mark.asyncio
async def test_router_coverage_and_earnings_calendar_mirror_and_non_pm_system_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5. HTTP endpoints for `/dalton/coverage*` and `/dalton/earnings/calendar*` + non-PM user can toggle/run their own `dalton-earnings-calendar`."""
    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)
    monkeypatch.setattr(
        coverage_svc,
        "sync_user_coverage_to_home",
        create_autospec(coverage_svc.sync_user_coverage_to_home, return_value=True),
    )
    monkeypatch.setattr(
        coverage_svc,
        "ensure_user_earnings_calendar_automation",
        create_autospec(
            coverage_svc.ensure_user_earnings_calendar_automation,
            return_value={
                "ok": True,
                "action": "created",
                "automation_id": "auto-cal-1",
                "name": "dalton-earnings-calendar",
            },
        ),
    )

    app = FastAPI()
    current_user = {"id": USER_ALICE}
    app.dependency_overrides[get_current_user_id] = lambda: current_user["id"]
    app.include_router(dalton_router)
    client = TestClient(app, raise_server_exceptions=False)

    # Publish mirror for Alice
    ledger_payload = {
        "updated": "2026-10-23",
        "entries": {
            "NVDA": {
                "name": "NVIDIA",
                "tier": "Core",
                "status": "confirmed",
                "date_status": "confirmed",
                "quarter": "FY27Q3",
                "date": "2026-11-18",
                "timing": "AMC",
            },
            "TSM": {
                "name": "TSMC",
                "tier": "Watch",
                "status": "pending",
                "date_status": "estimated",
                "quarter": "FY26Q4",
                "tentative_date": "2026-11-20",
                "timing": "BMO",
            },
        },
        "reported": [
            {
                "ticker": "ASML",
                "quarter": "FY26Q3",
                "date": "2026-10-15",
                "timing": "BMO",
            }
        ],
    }
    r_put = client.put("/api/v1/dalton/earnings/calendar/mirror", json={"ledger": ledger_payload})
    assert r_put.status_code == 200
    assert r_put.json()["ok"] is True
    assert r_put.json()["entries_count"] == 2

    # Query Alice's calendar in range
    r_cal = client.get("/api/v1/dalton/earnings/calendar?from=2026-10-01&to=2026-12-01")
    assert r_cal.status_code == 200
    cal_data = r_cal.json()
    assert cal_data["mirror_empty"] is False
    assert cal_data["count"] == 3
    by_sym = {item["symbol"]: item for item in cal_data["data"]}
    assert by_sym["NVDA"]["status"] == "confirmed"
    assert by_sym["NVDA"]["tier"] == "core"
    assert by_sym["NVDA"]["timing"] == "AMC"
    assert by_sym["TSM"]["status"] == "pending"
    assert by_sym["TSM"]["date_status"] == "estimated"
    assert by_sym["ASML"]["status"] == "reported"

    # Bob sees his own empty mirror
    current_user["id"] = USER_BOB
    r_bob = client.get("/api/v1/dalton/earnings/calendar?from=2026-10-01&to=2026-12-01")
    assert r_bob.status_code == 200
    assert r_bob.json()["mirror_empty"] is True
    assert r_bob.json()["data"] == []

    # Oversized mirror (>1 MB) is rejected with 413
    huge_ledger = {
        "updated": "2026-10-23",
        "entries": {"BIG": {"status": "pending", "note": "x" * 1_100_000}},
        "reported": [],
    }
    with pytest.raises(ValueError, match="1 MB"):
        validate_earnings_calendar_ledger(huge_ledger)
    r_huge = client.put("/api/v1/dalton/earnings/calendar/mirror", json={"ledger": huge_ledger})
    assert r_huge.status_code == 413

    # Non-PM user visibility on `dalton-earnings-calendar` vs other Home jobs
    cal_job = system_jobs_mod._apply_caller_visibility(
        {"key": "dalton-earnings-calendar", "scope": "home", "can_toggle": True, "can_run_now": True},
        is_pm=False,
    )
    assert cal_job["can_toggle"] is True
    assert cal_job["can_run_now"] is True

    other_home_job = system_jobs_mod._apply_caller_visibility(
        {"key": "dalton-director-daily", "scope": "home", "can_toggle": True, "can_run_now": True},
        is_pm=False,
    )
    assert other_home_job["can_toggle"] is False
    assert other_home_job["can_run_now"] is False


@pytest.mark.asyncio
async def test_yfinance_primary_screener_otc_filter_and_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 2 Item 1: yfinance is primary for screener & validation, filters out OTC & duplicate stems, and raises structured error on upstream failure."""
    raw_rows = [
        {"symbol": "NVDA", "companyName": "NVIDIA Corporation", "exchange": "NMS", "exchangeShortName": "NMS", "marketCap": 3_000_000_000_000},
        {"symbol": "TSM", "companyName": "Taiwan Semiconductor Manufacturing Co.", "exchange": "NYQ", "exchangeShortName": "NYQ", "marketCap": 1_000_000_000_000},
        {"symbol": "ASML", "companyName": "ASML Holding N.V.", "exchange": "NMS", "exchangeShortName": "NMS", "marketCap": 400_000_000_000},
        {"symbol": "ASMLF", "companyName": "ASML Holding N.V.", "exchange": "PNK", "exchangeShortName": "PNK", "marketCap": 400_000_000_000},
        {"symbol": "TOELY", "companyName": "Tokyo Electron Ltd", "exchange": "PNK", "exchangeShortName": "PNK", "marketCap": 100_000_000_000},
        {"symbol": "GOOGL", "companyName": "Alphabet Inc.", "exchange": "NMS", "exchangeShortName": "NMS", "marketCap": 2_000_000_000_000},
        {"symbol": "GOOG", "companyName": "Alphabet Inc.", "exchange": "NMS", "exchangeShortName": "NMS", "marketCap": 1_900_000_000_000},
    ]
    filtered = coverage_svc._filter_mainboard_screener_rows(raw_rows)
    filtered_syms = [r["symbol"] for r in filtered]
    assert "ASMLF" not in filtered_syms
    assert "TOELY" not in filtered_syms
    # Duplicate company stem "Alphabet" deduplicated to primary higher market-cap listing
    assert filtered_syms == ["NVDA", "TSM", "ASML", "GOOGL"]

    # Ticker validation: yfinance primary succeeds even when FMP fails with 402/429
    monkeypatch.setenv("FMP_API_KEY", "test-fmp-key")
    yf_lookup_mock = create_autospec(
        coverage_svc._lookup_via_yfinance,
        return_value={"ticker": "MU", "name": "Micron Technology, Inc.", "market": "US", "sector": "Technology", "industry": "Semiconductors"},
    )
    monkeypatch.setattr(coverage_svc, "_lookup_via_yfinance", yf_lookup_mock)
    monkeypatch.setattr(
        coverage_svc,
        "_lookup_via_fmp_profile",
        create_autospec(
            coverage_svc._lookup_via_fmp_profile,
            side_effect=coverage_svc.CoverageDataSourceError("fmp", "FMP returned HTTP 402", status_code=402),
        ),
    )
    enriched = await coverage_svc.validate_and_enrich_ticker("MU")
    assert enriched is not None
    assert enriched["ticker"] == "MU"
    assert enriched["name"] == "Micron Technology, Inc."

    # When both yfinance and FMP fail with upstream errors, raises CoverageDataSourceError (not None / "not_found")
    yf_lookup_mock.return_value = None
    yf_lookup_mock.side_effect = coverage_svc.CoverageDataSourceError("yfinance", "rate limited")
    with pytest.raises(coverage_svc.CoverageDataSourceError) as exc_info:
        await coverage_svc.validate_and_enrich_ticker("MU")
    assert exc_info.value.provider in {"yfinance", "fmp"}
    assert "test-fmp-key" not in str(exc_info.value)

    # Without FMP key configured, raises yfinance CoverageDataSourceError directly
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    with pytest.raises(coverage_svc.CoverageDataSourceError) as exc_info_yf:
        await coverage_svc.validate_and_enrich_ticker("MU")
    assert exc_info_yf.value.provider == "yfinance"


@pytest.mark.asyncio
async def test_seed_pool_lifecycle_row_assertion_and_build_coverage_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Round 2 Items 2, 3, 4: synchronous get_or_create_pool + open/close, transactional duplicate/row-count rejection, and integer exit code in build_coverage."""
    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)
    monkeypatch.setattr(
        coverage_svc,
        "sync_user_coverage_to_home",
        create_autospec(coverage_svc.sync_user_coverage_to_home, return_value=True),
    )
    monkeypatch.setattr(
        coverage_svc,
        "ensure_user_earnings_calendar_automation",
        create_autospec(
            coverage_svc.ensure_user_earnings_calendar_automation,
            return_value={"ok": True, "action": "created", "automation_id": "auto-cal-1", "name": "dalton-earnings-calendar"},
        ),
    )

    from scripts.dalton import seed_user_coverage as seed_mod
    import src.server.database.pool as pool_mod

    # Duplicate ticker in YAML must be rejected before committing and leave 0 rows in DB
    dup_yaml = tmp_path / "dup_coverage.yaml"
    dup_yaml.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "companies": [
                    {"ticker": "NVDA", "name": "NVIDIA", "tier": "core", "market": "US"},
                    {"ticker": "NVDA", "name": "NVIDIA Dup", "tier": "watch", "market": "US"},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplicate ticker"):
        await seed_mod.run_seed(user_id=USER_ALICE, yaml_path=dup_yaml, apply=True)
    assert await store.count_coverage_rows(USER_ALICE) == 0

    # Pool open() and close() were called on the autospec pool returned by get_or_create_pool()
    fake_pool = pool_mod.get_or_create_pool.return_value
    assert fake_pool.open.await_count >= 1
    assert fake_pool.close.await_count >= 1


@pytest.mark.asyncio
async def test_extractor_per_owner_aliases_for_stripping_and_proper_nouns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 2 Item 5: extractor strips company aliases and validates proper nouns using conversation owner's coverage aliases."""
    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)

    # Alice has company "ZETA" with Chinese alias "泽塔99科技" (contains digits that should be stripped only for Alice or union fallback)
    await store.upsert_coverage_rows(
        USER_ALICE,
        [
            company_to_row(
                {
                    "ticker": "ZETA",
                    "name": "Zeta Semi Corp",
                    "tier": "core",
                    "market": "US",
                    "aliases": {"search_names": ["泽塔99科技", "ZetaSemi"]},
                },
                user_id=USER_ALICE,
                source="chat",
            )
        ],
    )

    alice_strip = extractor_mod._chinese_company_aliases_for_stripping(USER_ALICE)
    bob_strip = extractor_mod._chinese_company_aliases_for_stripping(USER_BOB)
    union_strip = extractor_mod._chinese_company_aliases_for_stripping(None)
    assert "泽塔99科技" in alice_strip
    assert "泽塔99科技" not in bob_strip
    assert "泽塔99科技" in union_strip

    # Number parsing for Alice strips "99" inside "泽塔99科技", leaving only "25", whereas for Bob "99" is not stripped
    alice_nums = extractor_mod.parse_number_mentions("泽塔99科技今年营收增长25%", user_id=USER_ALICE)
    bob_nums = extractor_mod.parse_number_mentions("泽塔99科技今年营收增长25%", user_id=USER_BOB)
    assert [n[0] for n in alice_nums] == ["25"]
    assert "99" in [n[0] for n in bob_nums]

    # Entity alias groups for proper noun validation include ZETA for Alice, not for Bob
    alice_groups = extractor_mod._build_entity_alias_groups(USER_ALICE)
    bob_groups = extractor_mod._build_entity_alias_groups(USER_BOB)
    assert any("泽塔99科技" in grp for grp in alice_groups)
    assert not any("泽塔99科技" in grp for grp in bob_groups)

    # Roster mutations actively invalidate the per-user alias cache so changes are visible immediately
    monkeypatch.setattr(
        coverage_svc,
        "sync_user_coverage_to_home",
        create_autospec(coverage_svc.sync_user_coverage_to_home, return_value=True),
    )
    await coverage_svc.remove_user_coverage_tickers(USER_ALICE, ["ZETA"])
    assert "泽塔99科技" not in extractor_mod._chinese_company_aliases_for_stripping(USER_ALICE)
    assert not any("泽塔99科技" in grp for grp in extractor_mod._build_entity_alias_groups(USER_ALICE))

    # Direct store writes followed by cache_clear() also refresh immediately
    await store.upsert_coverage_rows(
        USER_BOB,
        [
            company_to_row(
                {
                    "ticker": "OMEGA",
                    "name": "Omega 88 Holdings",
                    "tier": "watch",
                    "market": "US",
                    "aliases": {"search_names": ["欧米伽88控股"]},
                },
                user_id=USER_BOB,
                source="chat",
            )
        ],
    )
    extractor_mod._load_ticker_company_aliases.cache_clear()
    assert "欧米伽88控股" in extractor_mod._chinese_company_aliases_for_stripping(USER_BOB)
    await coverage_svc.set_user_coverage_tier(USER_BOB, ["OMEGA"], tier="core")
    assert "欧米伽88控股" in extractor_mod._chinese_company_aliases_for_stripping(USER_BOB)


@pytest.mark.asyncio
async def test_multi_group_membership_include_validation_and_preflight_autonomy_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 2 Items 6, 7, 8, 9: multi-group membership preservation on delete, unvalidated criteria.include rejection, idempotent automation check, and preflight sync when autonomy.enabled=False."""
    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)

    home_sync_calls: list[str] = []

    async def _fake_sync_home(
        user_id: str,
        *,
        yaml_text: str | None = None,
        backend: Any = None,
        rel_path: str = "dalton/coverage.yaml",
    ) -> bool:
        _ = (yaml_text, backend, rel_path)
        home_sync_calls.append(user_id)
        return True

    monkeypatch.setattr(
        coverage_svc,
        "sync_user_coverage_to_home",
        create_autospec(coverage_svc.sync_user_coverage_to_home, side_effect=_fake_sync_home),
    )
    monkeypatch.setattr(
        preflight_mod,
        "sync_user_coverage_to_home",
        create_autospec(preflight_mod.sync_user_coverage_to_home, side_effect=_fake_sync_home),
    )

    ensure_calls: list[str] = []

    async def _fake_ensure(user_id: str) -> dict[str, Any]:
        ensure_calls.append(user_id)
        return {"ok": True, "action": "exists", "automation_id": "auto-cal-1", "name": "dalton-earnings-calendar"}

    monkeypatch.setattr(
        coverage_svc,
        "ensure_user_earnings_calendar_automation",
        create_autospec(coverage_svc.ensure_user_earnings_calendar_automation, side_effect=_fake_ensure),
    )

    async def _fake_validate(ticker: str) -> dict[str, Any] | None:
        sym = ticker.strip().upper()
        if sym in {"NVDA", "AVGO", "TSM"}:
            return {"ticker": sym, "name": f"{sym} Inc", "market": "US", "sector": "Technology", "industry": "Semiconductors"}
        return None

    monkeypatch.setattr(
        coverage_svc,
        "validate_and_enrich_ticker",
        create_autospec(coverage_svc.validate_and_enrich_ticker, side_effect=_fake_validate),
    )
    yf_screener_mock = create_autospec(
        coverage_svc._fetch_yf_screener_rows,
        return_value=[
            {"symbol": "NVDA", "companyName": "NVIDIA", "exchangeShortName": "NMS"},
            {"symbol": "AVGO", "companyName": "Broadcom", "exchangeShortName": "NMS"},
        ],
    )
    monkeypatch.setattr(coverage_svc, "_fetch_yf_screener_rows", yf_screener_mock)
    monkeypatch.setattr(
        coverage_svc,
        "_user_workspace_tickers",
        create_autospec(coverage_svc._user_workspace_tickers, return_value={}),
    )

    # Group 1 includes NVDA, AVGO (from screener) + TSM (valid include) + BOGUS99 (invalid include -> rejected/not_found)
    g1 = await coverage_svc.add_coverage_group(
        USER_ALICE,
        label="Group One",
        criteria={"country": "US", "industry": "Semiconductors", "include": ["TSM", "BOGUS99"]},
        default_tier="watch",
    )
    assert g1["ok"] is True
    assert g1["not_found"] == ["BOGUS99"]
    assert await store.get_coverage_row(USER_ALICE, "BOGUS99") is None
    assert await store.get_coverage_row(USER_ALICE, "TSM") is not None

    # Group 2 also matches NVDA (shared between Group 1 and Group 2)
    yf_screener_mock.return_value = [
        {"symbol": "NVDA", "companyName": "NVIDIA", "exchangeShortName": "NMS"},
    ]
    g2 = await coverage_svc.add_coverage_group(
        USER_ALICE,
        label="Group Two",
        criteria={"country": "US", "industry": "Semiconductors"},
        default_tier="watch",
    )
    assert g2["ok"] is True
    g1_id = g1["group"]["group_id"]
    g2_id = g2["group"]["group_id"]

    nvda_row = await store.get_coverage_row(USER_ALICE, "NVDA")
    assert nvda_row is not None
    assert set(nvda_row["extra"].get("group_ids", [])) == {g1_id, g2_id}

    # Deleting Group 1 removes AVGO and TSM (exclusive to Group 1), but keeps NVDA (still in Group 2)
    rm1 = await coverage_svc.remove_coverage_group(USER_ALICE, group_id=g1_id)
    assert rm1["ok"] is True
    assert set(rm1["removed_tickers"]) == {"AVGO", "TSM"}
    nvda_after = await store.get_coverage_row(USER_ALICE, "NVDA")
    assert nvda_after is not None
    assert nvda_after["extra"].get("group_ids") == [g2_id]
    assert nvda_after["group_id"] == g2_id

    # ensure_user_earnings_calendar_automation was called on every mutation while roster is non-empty
    assert len(ensure_calls) >= 3

    # Preflight syncs Home/dalton/coverage.yaml even when autonomy.enabled is False
    home_sync_calls.clear()
    disabled_cfg = preflight_mod.get_dalton_settings().model_copy(deep=True)
    disabled_cfg.autonomy.enabled = False
    monkeypatch.setattr(
        preflight_mod,
        "get_dalton_settings",
        create_autospec(preflight_mod.get_dalton_settings, return_value=disabled_cfg),
    )
    monkeypatch.setattr(
        preflight_mod,
        "verify_workspace_access",
        create_autospec(
            preflight_mod.verify_workspace_access,
            return_value={"workspace_id": "ws-home-alice", "user_id": USER_ALICE},
        ),
    )
    pf_disabled = await preflight_mod.preflight_check(
        {
            "automation_id": "auto-cal-1",
            "user_id": USER_ALICE,
            "workspace_id": "ws-home-alice",
            "name": "dalton-earnings-calendar",
            "trigger_type": "cron",
        },
        "exec-disabled-1",
        persist=True,
    )
    assert pf_disabled.should_wake is True
    assert pf_disabled.reason == "autonomy_disabled"
    assert USER_ALICE in home_sync_calls


@pytest.mark.asyncio
async def test_two_users_mutual_isolation_service_and_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 2 Item 11: two users' coverage rosters and groups are mutually invisible and non-modifiable at both service and router layers."""
    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)
    monkeypatch.setattr(
        coverage_svc,
        "sync_user_coverage_to_home",
        create_autospec(coverage_svc.sync_user_coverage_to_home, return_value=True),
    )
    monkeypatch.setattr(
        coverage_svc,
        "ensure_user_earnings_calendar_automation",
        create_autospec(
            coverage_svc.ensure_user_earnings_calendar_automation,
            return_value={"ok": True, "action": "exists", "automation_id": "auto-cal-1", "name": "dalton-earnings-calendar"},
        ),
    )
    monkeypatch.setattr(
        coverage_svc,
        "_user_workspace_tickers",
        create_autospec(coverage_svc._user_workspace_tickers, return_value={}),
    )

    async def _fake_validate(ticker: str) -> dict[str, Any] | None:
        sym = ticker.strip().upper()
        return {"ticker": sym, "name": f"{sym} Corp", "market": "US", "sector": "Technology", "industry": "Semiconductors"}

    monkeypatch.setattr(
        coverage_svc,
        "validate_and_enrich_ticker",
        create_autospec(coverage_svc.validate_and_enrich_ticker, side_effect=_fake_validate),
    )
    monkeypatch.setattr(
        coverage_svc,
        "_fetch_yf_screener_rows",
        create_autospec(
            coverage_svc._fetch_yf_screener_rows,
            return_value=[{"symbol": "NVDA", "companyName": "NVIDIA Corp", "exchangeShortName": "NMS"}],
        ),
    )

    # --- Service-layer mutual isolation ---
    await coverage_svc.add_user_coverage_tickers(USER_ALICE, ["AAPL"], tier="core")
    alice_grp = await coverage_svc.add_coverage_group(
        USER_ALICE,
        label="Alice Semis",
        criteria={"country": "US", "industry": "Semiconductors"},
        default_tier="watch",
    )
    alice_group_id = alice_grp["group"]["group_id"]

    await coverage_svc.add_user_coverage_tickers(USER_BOB, ["MSFT"], tier="watch")

    # Bob cannot see Alice's tickers or groups, and cannot modify or delete them via service calls
    bob_list = await coverage_svc.list_user_coverage_summary(USER_BOB)
    assert [c["ticker"] for c in bob_list["companies"]] == ["MSFT"]
    assert bob_list["groups"] == []

    bob_tier_attempt = await coverage_svc.set_user_coverage_tier(USER_BOB, ["AAPL"], tier="watch")
    assert bob_tier_attempt["updated"] == []
    assert bob_tier_attempt["missing_tickers"] == ["AAPL"]
    assert (await coverage_pkg.get_user_tier(USER_ALICE, "AAPL")) == "core"

    bob_rm_attempt = await coverage_svc.remove_user_coverage_tickers(USER_BOB, ["AAPL"])
    assert bob_rm_attempt["removed"] == []
    assert (await coverage_pkg.get_user_tier(USER_ALICE, "AAPL")) == "core"

    bob_grp_rm_attempt = await coverage_svc.remove_coverage_group(USER_BOB, group_id=alice_group_id)
    assert bob_grp_rm_attempt["deleted"] is False
    assert bob_grp_rm_attempt["removed_tickers"] == []
    assert len(await coverage_db.list_coverage_groups(USER_ALICE)) == 1

    # --- Router-layer mutual isolation ---
    app = FastAPI()
    current_user = {"id": USER_BOB}
    app.dependency_overrides[get_current_user_id] = lambda: current_user["id"]
    app.include_router(dalton_router)
    client = TestClient(app, raise_server_exceptions=False)

    # Bob's GET /dalton/coverage only returns MSFT
    r_bob_cov = client.get("/api/v1/dalton/coverage")
    assert r_bob_cov.status_code == 200
    assert [c["ticker"] for c in r_bob_cov.json()["companies"]] == ["MSFT"]

    # Bob attempting POST /dalton/coverage with action=set_tier and action=remove on Alice's AAPL mutates 0 rows and returns not_found
    r_bob_set_tier_alice = client.post(
        "/api/v1/dalton/coverage",
        json={"action": "set_tier", "tickers": ["AAPL"], "tier": "watch"},
    )
    assert r_bob_set_tier_alice.status_code == 200
    assert r_bob_set_tier_alice.json()["updated"] == []
    assert r_bob_set_tier_alice.json()["not_found"] == ["AAPL"]

    # Item 5: legacy REST POST /coverage converges onto WatchlistMutationService
    # (same-tx Outbox + TierPolicyReconciler), updating only Bob's own MSFT row.
    r_bob_core = client.post(
        "/api/v1/dalton/coverage",
        json={"action": "set_tier", "tickers": ["MSFT", "AAPL"], "tier": "core"},
    )
    assert r_bob_core.status_code == 200
    assert r_bob_core.json()["updated"] == ["MSFT"]
    assert r_bob_core.json()["not_found"] == ["AAPL"]
    assert r_bob_core.json()["job_id"]
    assert r_bob_core.json()["reconcile_status"] == "completed"

    r_bob_rm_alice = client.post(
        "/api/v1/dalton/coverage",
        json={"action": "remove", "tickers": ["AAPL"]},
    )
    assert r_bob_rm_alice.status_code == 200
    assert r_bob_rm_alice.json()["removed"] == []
    assert r_bob_rm_alice.json()["not_found"] == ["AAPL"]

    r_bob_del_grp = client.delete(f"/api/v1/dalton/coverage/groups/{alice_group_id}")
    assert r_bob_del_grp.status_code == 404

    # Switch to Alice and verify her AAPL (core) and group are untouched
    current_user["id"] = USER_ALICE
    r_alice_cov = client.get("/api/v1/dalton/coverage")
    assert r_alice_cov.status_code == 200
    alice_tickers = {c["ticker"]: c["tier"] for c in r_alice_cov.json()["companies"]}
    assert alice_tickers == {"AAPL": "core", "NVDA": "watch"}
    assert len(r_alice_cov.json()["groups"]) == 1


@pytest.mark.asyncio
async def test_resolve_and_delete_group_race_interleaving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 Item 1: interleaved resolve_coverage_group and remove_coverage_group aborts write with status='group_deleted', and group deletion only selects new primary group from still-existing groups."""
    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)
    monkeypatch.setattr(
        coverage_svc,
        "sync_user_coverage_to_home",
        create_autospec(coverage_svc.sync_user_coverage_to_home, return_value=True),
    )
    monkeypatch.setattr(
        coverage_svc,
        "ensure_user_earnings_calendar_automation",
        create_autospec(
            coverage_svc.ensure_user_earnings_calendar_automation,
            return_value={"ok": True, "action": "exists", "automation_id": "auto-cal-1", "name": "dalton-earnings-calendar"},
        ),
    )
    monkeypatch.setattr(
        coverage_svc,
        "_user_workspace_tickers",
        create_autospec(coverage_svc._user_workspace_tickers, return_value={}),
    )

    screener_mock = create_autospec(
        coverage_svc._fetch_yf_screener_rows,
        return_value=[
            {"symbol": "NVDA", "companyName": "NVIDIA Corp", "exchangeShortName": "NMS"},
            {"symbol": "AVGO", "companyName": "Broadcom Inc", "exchangeShortName": "NMS"},
        ],
    )
    monkeypatch.setattr(coverage_svc, "_fetch_yf_screener_rows", screener_mock)

    grp = await coverage_svc.add_coverage_group(
        USER_ALICE,
        label="Semis Group",
        criteria={"country": "US", "industry": "Semiconductors"},
        default_tier="watch",
    )
    gid = grp["group"]["group_id"]
    assert await store.count_coverage_rows(USER_ALICE) == 2

    # Interleave: while preview_coverage_group is running inside resolve_coverage_group (before lock acquisition),
    # another operation deletes the group and its exclusive members.
    orig_preview = coverage_svc.preview_coverage_group

    async def _interleaved_preview(label: str, criteria: Any) -> dict[str, Any]:
        res = await orig_preview(label, criteria)
        await coverage_svc.remove_coverage_group(USER_ALICE, gid)
        return res

    monkeypatch.setattr(
        coverage_svc,
        "preview_coverage_group",
        create_autospec(orig_preview, side_effect=_interleaved_preview),
    )

    resolve_res = await coverage_svc.resolve_coverage_group(USER_ALICE, gid)
    assert resolve_res["status"] == "group_deleted"
    assert resolve_res["ok"] is False
    # Deleted group and its exclusive tickers were NOT resurrected by the in-flight resolve
    assert await store.get_coverage_group(USER_ALICE, gid) is None
    assert await store.count_coverage_rows(USER_ALICE) == 0

    # Restore preview_coverage_group and verify deleting a group only picks new primary group_id from still-existing groups
    monkeypatch.setattr(coverage_svc, "preview_coverage_group", orig_preview)
    g_active_1 = await coverage_svc.add_coverage_group(
        USER_ALICE,
        label="Active 1",
        criteria={"country": "US", "industry": "Semiconductors"},
        default_tier="watch",
    )
    g_active_2 = await coverage_svc.add_coverage_group(
        USER_ALICE,
        label="Active 2",
        criteria={"country": "US", "industry": "Semiconductors"},
        default_tier="watch",
    )
    g1_id = g_active_1["group"]["group_id"]
    g2_id = g_active_2["group"]["group_id"]

    # Inject a stale non-existent ghost group ID ahead of g2_id in extra["group_ids"]
    nvda_row = store.rows[(USER_ALICE, "NVDA")]
    nvda_row["extra"]["group_ids"] = [g1_id, "grp-ghost-deleted", g2_id]

    await coverage_svc.remove_coverage_group(USER_ALICE, g1_id)
    nvda_after = await store.get_coverage_row(USER_ALICE, "NVDA")
    assert nvda_after is not None
    assert nvda_after["group_id"] == g2_id
    assert nvda_after["extra"]["group_ids"] == [g2_id]


@pytest.mark.asyncio
async def test_fast_info_standalone_fallback_and_market_data_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 Item 2: empty info with valid fast_info exchange + price validates ticker without quoteType; upstream errors raise MarketDataUnavailable instead of returning None."""
    import yfinance as yf
    import src.data_client.yfinance.yahoo as yahoo_mod

    monkeypatch.delenv("FMP_API_KEY", raising=False)

    # 1) info is empty ({}), no quoteType, but fast_info has valid exchange and price -> validates via fast_info alone
    fake_ticker_ok = create_autospec(yf.Ticker, instance=True)
    fake_ticker_ok.info = {}
    fake_ticker_ok.fast_info = {"exchange": "NMS", "lastPrice": 185.25, "marketCap": 250_000_000_000}

    yahoo_ticker_mock = create_autospec(yahoo_mod.yahoo_ticker, return_value=fake_ticker_ok)
    monkeypatch.setattr(yahoo_mod, "yahoo_ticker", yahoo_ticker_mock)

    hit = await coverage_svc.validate_and_enrich_ticker("AMD")
    assert hit is not None
    assert hit["ticker"] == "AMD"
    assert hit["exchange"] == "NMS"
    assert hit["market"] == "US"

    # 2) info is empty ({}) and fast_info has no exchange / no price -> returns None (genuinely not found)
    fake_ticker_none = create_autospec(yf.Ticker, instance=True)
    fake_ticker_none.info = {}
    fake_ticker_none.fast_info = {"exchange": "", "lastPrice": None}
    yahoo_ticker_mock.return_value = fake_ticker_none

    not_found_hit = await coverage_svc.validate_and_enrich_ticker("NOPE999")
    assert not_found_hit is None

    # 3) info is empty ({}) and fast_info raises a provider/network error -> raises MarketDataUnavailable (not swallowed as None)
    class _BrokenFastInfo:
        def get(self, key: str) -> Any:
            raise RuntimeError(f"Yahoo chart 503 while reading {key}")

    fake_ticker_err = create_autospec(yf.Ticker, instance=True)
    fake_ticker_err.info = {}
    fake_ticker_err.fast_info = create_autospec(_BrokenFastInfo(), instance=True, side_effect= None)
    fake_ticker_err.fast_info.get.side_effect = RuntimeError("Yahoo chart 503 while reading exchange")
    yahoo_ticker_mock.return_value = fake_ticker_err

    with pytest.raises(coverage_svc.MarketDataUnavailable) as exc_info:
        await coverage_svc.validate_and_enrich_ticker("AMD")
    err_dict = exc_info.value.to_dict()
    assert err_dict["error"] == "MarketDataUnavailable"
    assert err_dict["provider"] == "yfinance"


@pytest.mark.asyncio
async def test_preflight_attempts_home_coverage_sync_before_timeout_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 Item 3: preflight attempts Home coverage.yaml sync before any step that can time out and pass through/wake, and bounds sync with its own short timeout."""
    import asyncio

    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)

    monkeypatch.setattr(
        preflight_mod,
        "verify_workspace_access",
        create_autospec(
            preflight_mod.verify_workspace_access,
            return_value={"workspace_id": "ws-home-alice", "user_id": USER_ALICE},
        ),
    )

    sync_attempts: list[str] = []

    async def _fast_sync(
        user_id: str,
        *,
        yaml_text: str | None = None,
        backend: Any = None,
        rel_path: str = "dalton/coverage.yaml",
    ) -> bool:
        _ = (yaml_text, backend, rel_path)
        sync_attempts.append(user_id)
        return True

    sync_mock = create_autospec(preflight_mod.sync_user_coverage_to_home, side_effect=_fast_sync)
    monkeypatch.setattr(
        preflight_mod,
        "sync_user_coverage_to_home",
        sync_mock,
    )

    async def _slow_budget_check(*, user_id: str | None = None) -> tuple[bool, str | None]:
        _ = user_id
        await asyncio.sleep(0.25)
        return True, None

    monkeypatch.setattr(
        preflight_mod,
        "_check_budget_available",
        create_autospec(preflight_mod._check_budget_available, side_effect=_slow_budget_check),
    )

    # _check_budget_available times out after 0.05s and preflight wakes conservatively,
    # but Home coverage.yaml sync was ALREADY attempted before the timeout step!
    res = await preflight_mod.preflight_check(
        {
            "automation_id": "auto-cal-timeout",
            "user_id": USER_ALICE,
            "workspace_id": "ws-home-alice",
            "name": "dalton-earnings-calendar",
            "trigger_type": "cron",
        },
        "exec-timeout-1",
        persist=True,
        timeout_s=0.05,
    )
    assert res.should_wake is True
    assert res.reason == "preflight_timeout"
    assert sync_attempts == [USER_ALICE]

    # Also verify that if sync_user_coverage_to_home itself hangs beyond its dedicated short timeout,
    # _sync_home_coverage_for_preflight logs a warning and continues without raising.
    async def _hanging_sync(
        user_id: str,
        *,
        yaml_text: str | None = None,
        backend: Any = None,
        rel_path: str = "dalton/coverage.yaml",
    ) -> bool:
        _ = (yaml_text, backend, rel_path)
        sync_attempts.append(f"hang:{user_id}")
        await asyncio.sleep(0.5)
        return True

    sync_mock.side_effect = _hanging_sync
    summary = await preflight_mod._sync_home_coverage_for_preflight(
        user_id=USER_ALICE,
        kind="director_daily",
        ticker=None,
        persist=True,
        sync_timeout_s=0.05,
    )
    assert summary is None
    assert f"hang:{USER_ALICE}" in sync_attempts


@pytest.mark.asyncio
async def test_group_stale_per_group_and_legacy_bool_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Item 1: group_stale is tracked per group in extra.group_stale_ids, refreshing Group B preserves Group A's stale mark, and legacy extra.group_stale is read as primary group stale and migrated on write."""
    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)
    monkeypatch.setattr(
        coverage_svc,
        "sync_user_coverage_to_home",
        create_autospec(coverage_svc.sync_user_coverage_to_home, return_value=True),
    )
    monkeypatch.setattr(
        coverage_svc,
        "ensure_user_earnings_calendar_automation",
        create_autospec(
            coverage_svc.ensure_user_earnings_calendar_automation,
            return_value={"ok": True, "action": "exists", "automation_id": "auto-cal-1", "name": "dalton-earnings-calendar"},
        ),
    )
    monkeypatch.setattr(
        coverage_svc,
        "_user_workspace_tickers",
        create_autospec(coverage_svc._user_workspace_tickers, return_value={}),
    )

    screener_mock = create_autospec(
        coverage_svc._fetch_yf_screener_rows,
        return_value=[
            {"symbol": "NVDA", "companyName": "NVIDIA Corp", "exchangeShortName": "NMS"},
            {"symbol": "AVGO", "companyName": "Broadcom Inc", "exchangeShortName": "NMS"},
        ],
    )
    monkeypatch.setattr(coverage_svc, "_fetch_yf_screener_rows", screener_mock)

    # Create Group A and Group B; both initially match NVDA and AVGO
    grp_a = await coverage_svc.add_coverage_group(
        USER_ALICE,
        label="AI Semis A",
        criteria={"country": "US", "industry": "Semiconductors"},
        default_tier="watch",
    )
    grp_b = await coverage_svc.add_coverage_group(
        USER_ALICE,
        label="Networking B",
        criteria={"country": "US", "industry": "Communication Equipment"},
        default_tier="watch",
    )
    group_a_id = grp_a["group"]["group_id"]
    group_b_id = grp_b["group"]["group_id"]

    avgo_initial = await store.get_coverage_row(USER_ALICE, "AVGO")
    assert avgo_initial is not None
    assert avgo_initial["extra"]["group_ids"] == [group_a_id, group_b_id]
    assert "group_stale_ids" not in avgo_initial["extra"]
    assert "group_stale" not in avgo_initial["extra"]

    # 1) Group A refreshes and no longer matches AVGO -> AVGO is marked stale for Group A
    screener_mock.return_value = [
        {"symbol": "NVDA", "companyName": "NVIDIA Corp", "exchangeShortName": "NMS"},
    ]
    res_a = await coverage_svc.resolve_coverage_group(USER_ALICE, group_a_id)
    assert res_a["stale_tickers"] == ["AVGO"]

    avgo_after_a = await store.get_coverage_row(USER_ALICE, "AVGO")
    assert avgo_after_a is not None
    assert avgo_after_a["extra"]["group_stale_ids"] == [group_a_id]
    assert "group_stale" not in avgo_after_a["extra"]

    # 2) Group B refreshes and STILL matches AVGO -> Group A's stale mark on AVGO must remain!
    screener_mock.return_value = [
        {"symbol": "NVDA", "companyName": "NVIDIA Corp", "exchangeShortName": "NMS"},
        {"symbol": "AVGO", "companyName": "Broadcom Inc", "exchangeShortName": "NMS"},
    ]
    res_b = await coverage_svc.resolve_coverage_group(USER_ALICE, group_b_id)
    assert "AVGO" in res_b["matched_tickers"]
    assert res_b["stale_tickers"] == []

    avgo_after_b = await store.get_coverage_row(USER_ALICE, "AVGO")
    assert avgo_after_b is not None
    assert avgo_after_b["extra"]["group_stale_ids"] == [group_a_id]
    assert "group_stale" not in avgo_after_b["extra"]

    # 3) Adding a new Group C that also matches AVGO (upserting & rebuilding extra) preserves Group A's stale ID
    grp_c = await coverage_svc.add_coverage_group(
        USER_ALICE,
        label="Custom Silicon C",
        criteria={"country": "US", "industry": "Semiconductors"},
        default_tier="watch",
    )
    group_c_id = grp_c["group"]["group_id"]
    avgo_after_c = await store.get_coverage_row(USER_ALICE, "AVGO")
    assert avgo_after_c is not None
    assert avgo_after_c["extra"]["group_ids"] == [group_a_id, group_b_id, group_c_id]
    assert avgo_after_c["extra"]["group_stale_ids"] == [group_a_id]

    # 4) Reading and displaying derives group_stale from non-empty group_stale_ids and lists stale groups
    summary = await coverage_svc.list_user_coverage_summary(USER_ALICE)
    assert summary["stale_tickers"] == ["AVGO"]
    avgo_summary = next(c for c in summary["companies"] if c["ticker"] == "AVGO")
    assert avgo_summary["group_stale"] is True
    assert avgo_summary["group_stale_ids"] == [group_a_id]
    assert avgo_summary["stale_group_labels"] == ["AI Semis A"]
    assert avgo_summary["stale_groups"] == [{"group_id": group_a_id, "label": "AI Semis A"}]

    # 5) When Group A refreshes again and matches AVGO again, Group A is removed from group_stale_ids
    res_a_recover = await coverage_svc.resolve_coverage_group(USER_ALICE, group_a_id)
    assert "AVGO" in res_a_recover["matched_tickers"]
    avgo_recovered = await store.get_coverage_row(USER_ALICE, "AVGO")
    assert avgo_recovered is not None
    assert "group_stale_ids" not in avgo_recovered["extra"]
    summary_recovered = await coverage_svc.list_user_coverage_summary(USER_ALICE)
    assert summary_recovered["stale_tickers"] == []

    # 6) Legacy boolean `extra.group_stale = True` compatibility:
    # Reading interprets it as primary group stale; writing migrates to `group_stale_ids` and drops `group_stale`.
    nvda_row = store.rows[(USER_ALICE, "NVDA")]
    nvda_row["group_id"] = group_a_id
    nvda_row["extra"] = {"group_ids": [group_a_id, group_b_id], "group_stale": True}

    assert extract_row_stale_group_ids(nvda_row) == [group_a_id]
    legacy_summary = await coverage_svc.list_user_coverage_summary(USER_ALICE)
    nvda_summary = next(c for c in legacy_summary["companies"] if c["ticker"] == "NVDA")
    assert nvda_summary["group_stale"] is True
    assert nvda_summary["group_stale_ids"] == [group_a_id]
    assert nvda_summary["stale_group_labels"] == ["AI Semis A"]

    # Refreshing Group B (which still matches NVDA) preserves Group A's stale mark and migrates storage on write
    await store.mark_coverage_rows_group_stale(USER_ALICE, ["NVDA"], group_id=group_b_id, stale=False)
    nvda_migrated = await store.get_coverage_row(USER_ALICE, "NVDA")
    assert nvda_migrated is not None
    assert nvda_migrated["extra"]["group_stale_ids"] == [group_a_id]
    assert "group_stale" not in nvda_migrated["extra"]


@pytest.mark.asyncio
async def test_market_data_unavailable_on_all_error_codes_and_incomplete_structures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Item 2: yfinance exception, yfinance incomplete structure, FMP 401/402/403/429/5xx, and timeout all raise MarketDataUnavailable; only explicit not-found returns None."""
    import yfinance as yf
    import src.data_client.yfinance.yahoo as yahoo_mod
    from src.data_client.fmp.fmp_client import FMPClient, FMPRequestError

    monkeypatch.delenv("FMP_API_KEY", raising=False)

    # 1) yfinance raises an exception (including timeout) -> raises MarketDataUnavailable
    yahoo_ticker_mock = create_autospec(
        yahoo_mod.yahoo_ticker,
        side_effect=TimeoutError("yfinance request timed out"),
    )
    monkeypatch.setattr(yahoo_mod, "yahoo_ticker", yahoo_ticker_mock)
    with pytest.raises(coverage_svc.MarketDataUnavailable) as exc_yf_timeout:
        await coverage_svc.validate_and_enrich_ticker("AAPL")
    assert exc_yf_timeout.value.provider == "yfinance"

    # 2) yfinance returns incomplete structure -> raises MarketDataUnavailable
    # 2a) info is None
    tk_none_info = create_autospec(yf.Ticker, instance=True)
    tk_none_info.info = None
    tk_none_info.fast_info = {}
    yahoo_ticker_mock.side_effect = None
    yahoo_ticker_mock.return_value = tk_none_info
    with pytest.raises(coverage_svc.MarketDataUnavailable) as exc_yf_none_info:
        await coverage_svc.validate_and_enrich_ticker("AAPL")
    assert exc_yf_none_info.value.provider == "yfinance"

    # 2b) info has quoteType='EQUITY' but no exchange, name, or price
    tk_partial_equity = create_autospec(yf.Ticker, instance=True)
    tk_partial_equity.info = {"quoteType": "EQUITY"}
    tk_partial_equity.fast_info = {}
    yahoo_ticker_mock.return_value = tk_partial_equity
    with pytest.raises(coverage_svc.MarketDataUnavailable) as exc_yf_partial_eq:
        await coverage_svc.validate_and_enrich_ticker("AAPL")
    assert exc_yf_partial_eq.value.provider == "yfinance"

    # 2c) fast_info has exchange='NMS' but missing/None price and empty info
    tk_partial_fi = create_autospec(yf.Ticker, instance=True)
    tk_partial_fi.info = {}
    tk_partial_fi.fast_info = {"exchange": "NMS", "lastPrice": None}
    yahoo_ticker_mock.return_value = tk_partial_fi
    with pytest.raises(coverage_svc.MarketDataUnavailable) as exc_yf_partial_fi:
        await coverage_svc.validate_and_enrich_ticker("AAPL")
    assert exc_yf_partial_fi.value.provider == "yfinance"

    # 3) Explicit yfinance not-found (empty quoteType and no exchange in fast_info) -> returns None
    tk_explicit_empty = create_autospec(yf.Ticker, instance=True)
    tk_explicit_empty.info = {"quoteType": "NONE"}
    tk_explicit_empty.fast_info = {"exchange": "", "lastPrice": None}
    yahoo_ticker_mock.return_value = tk_explicit_empty
    assert await coverage_svc.validate_and_enrich_ticker("INVALID999") is None

    # 4) FMP status codes: 401, 402, 403, 429, 500, 502, 503, 504 -> each raises MarketDataUnavailable
    monkeypatch.setenv("FMP_API_KEY", "test-fmp-key")
    get_profile_mock = create_autospec(FMPClient.get_profile)
    monkeypatch.setattr(FMPClient, "get_profile", get_profile_mock)
    for status_code in (401, 402, 403, 429, 500, 502, 503, 504):
        get_profile_mock.side_effect = FMPRequestError(
            f"FMP API request failed ({status_code})",
            status_code=status_code,
        )
        with pytest.raises(coverage_svc.MarketDataUnavailable) as exc_fmp_code:
            await coverage_svc.validate_and_enrich_ticker("AAPL")
        assert exc_fmp_code.value.provider == "fmp"
        assert exc_fmp_code.value.status_code == status_code

    # 5) FMP timeout -> raises MarketDataUnavailable
    get_profile_mock.side_effect = FMPRequestError("FMP API request timed out")
    with pytest.raises(coverage_svc.MarketDataUnavailable) as exc_fmp_timeout:
        await coverage_svc.validate_and_enrich_ticker("AAPL")
    assert exc_fmp_timeout.value.provider == "fmp"

    # 6) Explicit FMP not-found: HTTP 200 with empty list [] (after yfinance explicit empty) -> returns None
    get_profile_mock.side_effect = None
    get_profile_mock.return_value = []
    assert await coverage_svc.validate_and_enrich_ticker("INVALID999") is None


@pytest.mark.asyncio
async def test_screener_malformed_response_raises_and_valid_empty_marks_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5 Item 2: non-mapping yf.screen or non-list FMP screener response raises MarketDataUnavailable without updating last_resolved_at or marking stale; valid empty results ({'quotes': []} or []) mark stale and update last_resolved_at."""
    import yfinance.screener.screener as yf_screener_mod
    from src.data_client.fmp.fmp_client import FMPClient

    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.setattr(
        coverage_svc,
        "sync_user_coverage_to_home",
        create_autospec(coverage_svc.sync_user_coverage_to_home, return_value=True),
    )
    monkeypatch.setattr(
        coverage_svc,
        "ensure_user_earnings_calendar_automation",
        create_autospec(
            coverage_svc.ensure_user_earnings_calendar_automation,
            return_value={"ok": True, "action": "exists", "automation_id": "auto-cal-1", "name": "dalton-earnings-calendar"},
        ),
    )
    monkeypatch.setattr(
        coverage_svc,
        "_user_workspace_tickers",
        create_autospec(coverage_svc._user_workspace_tickers, return_value={}),
    )

    yf_screen_mock = create_autospec(
        yf_screener_mod.screen,
        return_value={
            "quotes": [
                {"symbol": "NVDA", "longName": "NVIDIA Corp", "exchange": "NMS", "quoteType": "EQUITY", "marketCap": 3_000_000_000_000},
                {"symbol": "AVGO", "longName": "Broadcom Inc", "exchange": "NMS", "quoteType": "EQUITY", "marketCap": 800_000_000_000},
            ]
        },
    )
    monkeypatch.setattr(yf_screener_mod, "screen", yf_screen_mock)

    grp_res = await coverage_svc.add_coverage_group(
        USER_ALICE,
        label="US Semis",
        criteria={"country": "US", "industry": "Semiconductors"},
        default_tier="watch",
    )
    gid = grp_res["group"]["group_id"]
    initial_ts = datetime.now(timezone.utc) - timedelta(days=90)
    store.groups[(USER_ALICE, gid)]["last_resolved_at"] = initial_ts

    # 1) Malformed yf.screen response (not a mapping, e.g. None or list, or non-list quotes) -> raises MarketDataUnavailable
    for bad_payload in (None, ["unexpected"], {"quotes": None}):
        yf_screen_mock.return_value = bad_payload
        with pytest.raises(coverage_svc.MarketDataUnavailable) as exc_yf:
            await coverage_svc.resolve_coverage_group(
                USER_ALICE,
                gid,
                now=datetime.now(timezone.utc),
            )
        assert exc_yf.value.provider == "yfinance"
        assert store.groups[(USER_ALICE, gid)]["last_resolved_at"] == initial_ts
        nvda_row = await store.get_coverage_row(USER_ALICE, "NVDA")
        assert nvda_row is not None
        assert "group_stale_ids" not in nvda_row["extra"]

    # 2) Malformed FMP screener response (not a list, e.g. dict error payload) -> raises MarketDataUnavailable
    monkeypatch.setenv("FMP_API_KEY", "test-fmp-key")
    fmp_screener_mock = create_autospec(
        FMPClient.get_company_screener,
        return_value={"Error Message": "Invalid response shape"},  # type: ignore[arg-type]
    )
    monkeypatch.setattr(FMPClient, "get_company_screener", fmp_screener_mock)
    with pytest.raises(coverage_svc.MarketDataUnavailable) as exc_fmp:
        await coverage_svc.resolve_coverage_group(
            USER_ALICE,
            gid,
            now=datetime.now(timezone.utc),
        )
    assert exc_fmp.value.provider == "fmp"
    assert store.groups[(USER_ALICE, gid)]["last_resolved_at"] == initial_ts
    nvda_row_after_fmp_err = await store.get_coverage_row(USER_ALICE, "NVDA")
    assert nvda_row_after_fmp_err is not None
    assert "group_stale_ids" not in nvda_row_after_fmp_err["extra"]

    # 3) Valid empty FMP screener response ([] when yf.screen fails) -> treated as valid empty result
    fmp_screener_mock.return_value = []
    empty_fmp_rows = await coverage_svc._fetch_screener_rows({"country": "US", "industry": "Semiconductors"})
    assert empty_fmp_rows == []

    # 4) Valid empty yf.screen response (mapping with quotes=[]) -> marks members stale and updates last_resolved_at
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    yf_screen_mock.return_value = {"quotes": []}
    refresh_ts = datetime.now(timezone.utc)
    res_empty = await coverage_svc.resolve_coverage_group(
        USER_ALICE,
        gid,
        now=refresh_ts,
    )
    assert res_empty["matched_tickers"] == []
    assert set(res_empty["stale_tickers"]) == {"NVDA", "AVGO"}
    assert store.groups[(USER_ALICE, gid)]["last_resolved_at"] == refresh_ts
    nvda_stale = await store.get_coverage_row(USER_ALICE, "NVDA")
    assert nvda_stale is not None
    assert nvda_stale["extra"]["group_stale_ids"] == [gid]


@pytest.mark.asyncio
async def test_quarterly_refresh_failure_reports_summary_and_keeps_last_resolved_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5 Item 3: when quarterly refresh fails, refresh_due_coverage_groups_for_preflight appends failure notice to summary_lines, returns failed_groups, and leaves last_resolved_at untouched so next run retries."""
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)
    monkeypatch.setattr(
        coverage_svc,
        "sync_user_coverage_to_home",
        create_autospec(coverage_svc.sync_user_coverage_to_home, return_value=True),
    )
    monkeypatch.setattr(
        coverage_svc,
        "ensure_user_earnings_calendar_automation",
        create_autospec(
            coverage_svc.ensure_user_earnings_calendar_automation,
            return_value={"ok": True, "action": "exists", "automation_id": "auto-cal-1", "name": "dalton-earnings-calendar"},
        ),
    )
    monkeypatch.setattr(
        coverage_svc,
        "_user_workspace_tickers",
        create_autospec(coverage_svc._user_workspace_tickers, return_value={}),
    )

    screener_mock = create_autospec(
        coverage_svc._fetch_yf_screener_rows,
        return_value=[
            {"symbol": "NVDA", "companyName": "NVIDIA Corp", "exchangeShortName": "NMS"},
        ],
    )
    monkeypatch.setattr(coverage_svc, "_fetch_yf_screener_rows", screener_mock)

    grp_res = await coverage_svc.add_coverage_group(
        USER_ALICE,
        label="美股半导体大票",
        criteria={"country": "US", "industry": "Semiconductors"},
        default_tier="watch",
    )
    gid = grp_res["group"]["group_id"]
    stale_resolved_at = datetime.now(timezone.utc) - timedelta(days=90)
    store.groups[(USER_ALICE, gid)]["last_resolved_at"] = stale_resolved_at

    # Simulate data source failure during quarterly refresh
    screener_mock.side_effect = coverage_svc.MarketDataUnavailable("yfinance", "Yahoo screener 503")
    now_t1 = datetime.now(timezone.utc)
    fail_out = await coverage_svc.refresh_due_coverage_groups_for_preflight(
        USER_ALICE,
        now=now_t1,
    )
    assert fail_out["refreshed_groups"] == []
    assert len(fail_out["failed_groups"]) == 1
    assert fail_out["failed_groups"][0]["group_id"] == gid
    assert fail_out["failed_groups"][0]["label"] == "美股半导体大票"
    assert "Yahoo screener 503" in fail_out["failed_groups"][0]["error"]
    expected_line = "规则组「美股半导体大票」季度重解析失败（数据源暂不可用），下次自动重试"
    assert fail_out["summary_lines"] == [expected_line]
    assert fail_out["summary_text"] == expected_line

    # Confirm last_resolved_at was NOT written, so the group remains due for retry on the next run
    assert store.groups[(USER_ALICE, gid)]["last_resolved_at"] == stale_resolved_at
    assert coverage_svc._is_group_due_for_refresh(store.groups[(USER_ALICE, gid)], now=now_t1) is True

    # Next run succeeds once the data source recovers
    screener_mock.side_effect = None
    screener_mock.return_value = [
        {"symbol": "NVDA", "companyName": "NVIDIA Corp", "exchangeShortName": "NMS"},
        {"symbol": "AMD", "companyName": "Advanced Micro Devices", "exchangeShortName": "NMS"},
    ]
    now_t2 = now_t1 + timedelta(minutes=5)
    retry_out = await coverage_svc.refresh_due_coverage_groups_for_preflight(
        USER_ALICE,
        now=now_t2,
    )
    assert retry_out["failed_groups"] == []
    assert len(retry_out["refreshed_groups"]) == 1
    assert retry_out["added_tickers"] == ["AMD"]
    assert store.groups[(USER_ALICE, gid)]["last_resolved_at"] == now_t2


@pytest.mark.asyncio
async def test_extractor_derived_alias_caches_ttl_refresh_and_user_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Derived caches (`cn_strip_aliases` and `entity_alias_groups`) live on the same `(user_id, time_bucket)`
    cache entry as `alias_map`, refresh together on TTL expiry, and never cross-contaminate between users.
    """
    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)

    clock = {"now": 1200.0}
    monkeypatch.setattr(
        extractor_mod.time,
        "monotonic",
        create_autospec(extractor_mod.time.monotonic, side_effect=lambda: clock["now"]),
    )

    await store.upsert_coverage_rows(
        USER_ALICE,
        [
            company_to_row(
                {
                    "ticker": "ALFA",
                    "name": "Alfa 66 Tech",
                    "tier": "core",
                    "market": "US",
                    "aliases": {"search_names": ["阿尔法66科技", "Alfa66Tech"]},
                },
                user_id=USER_ALICE,
                source="chat",
            )
        ],
    )
    await store.upsert_coverage_rows(
        USER_BOB,
        [
            company_to_row(
                {
                    "ticker": "BETA",
                    "name": "Beta 88 Group",
                    "tier": "watch",
                    "market": "US",
                    "aliases": {"search_names": ["贝塔88集团", "Beta88Group"]},
                },
                user_id=USER_BOB,
                source="chat",
            )
        ],
    )

    # 1) Two users with different aliases never cross-contaminate derived results
    alice_strip = extractor_mod._chinese_company_aliases_for_stripping(USER_ALICE)
    bob_strip = extractor_mod._chinese_company_aliases_for_stripping(USER_BOB)
    assert "阿尔法66科技" in alice_strip and "贝塔88集团" not in alice_strip
    assert "贝塔88集团" in bob_strip and "阿尔法66科技" not in bob_strip

    alice_nums = [
        n[0]
        for n in extractor_mod.parse_number_mentions(
            "阿尔法66科技与贝塔88集团今年营收增长25%", user_id=USER_ALICE
        )
    ]
    bob_nums = [
        n[0]
        for n in extractor_mod.parse_number_mentions(
            "阿尔法66科技与贝塔88集团今年营收增长25%", user_id=USER_BOB
        )
    ]
    assert alice_nums == ["88", "25"]
    assert bob_nums == ["66", "25"]

    # Quote has Chinese name "阿尔法66科技", claim uses English alias "Alfa66Tech":
    # supported for Alice (via her entity alias group), unsupported for Bob
    alice_unsup_alfa, _ = extractor_mod._validate_proper_nouns_and_clean_topic(
        "Alfa66Tech 今年签署合作",
        "Alfa66Tech",
        "阿尔法66科技今年签署合作",
        user_id=USER_ALICE,
    )
    bob_unsup_alfa, _ = extractor_mod._validate_proper_nouns_and_clean_topic(
        "Alfa66Tech 今年签署合作",
        "Alfa66Tech",
        "阿尔法66科技今年签署合作",
        user_id=USER_BOB,
    )
    assert alice_unsup_alfa == []
    assert bob_unsup_alfa == ["Alfa66Tech"]

    # Conversely, quote has "贝塔88集团" and claim uses "Beta88Group":
    # supported for Bob, unsupported for Alice
    alice_unsup_beta, _ = extractor_mod._validate_proper_nouns_and_clean_topic(
        "Beta88Group 今年签署合作",
        "Beta88Group",
        "贝塔88集团今年签署合作",
        user_id=USER_ALICE,
    )
    bob_unsup_beta, _ = extractor_mod._validate_proper_nouns_and_clean_topic(
        "Beta88Group 今年签署合作",
        "Beta88Group",
        "贝塔88集团今年签署合作",
        user_id=USER_BOB,
    )
    assert alice_unsup_beta == ["Beta88Group"]
    assert bob_unsup_beta == []

    # Both derived results are attached to the exact same cache entry object as `alias_map` keyed by `(user_id, time_bucket)`
    bucket_0 = int(clock["now"] // extractor_mod._ALIAS_CACHE_TTL_SECONDS)
    alice_entry = extractor_mod._TICKER_ALIASES_CACHE[(USER_ALICE, bucket_0)]
    bob_entry = extractor_mod._TICKER_ALIASES_CACHE[(USER_BOB, bucket_0)]
    assert alice_entry.cn_strip_aliases is alice_strip
    assert bob_entry.cn_strip_aliases is bob_strip
    assert alice_entry.entity_alias_groups is not None
    assert bob_entry.entity_alias_groups is not None

    # 2) Simulate a DB update for Alice without local invalidation (e.g. from another worker):
    # replace ALFA ("阿尔法66科技") with GAMMA ("伽马77半导体")
    await store.delete_coverage_tickers(USER_ALICE, ["ALFA"])
    await store.upsert_coverage_rows(
        USER_ALICE,
        [
            company_to_row(
                {
                    "ticker": "GAMMA",
                    "name": "Gamma 77 Semi",
                    "tier": "core",
                    "market": "US",
                    "aliases": {"search_names": ["伽马77半导体", "Gamma77Semi"]},
                },
                user_id=USER_ALICE,
                source="chat",
            )
        ],
    )

    # Within the same 5-minute TTL bucket, cached derived results remain in effect
    clock["now"] = 1260.0
    assert "阿尔法66科技" in extractor_mod._chinese_company_aliases_for_stripping(USER_ALICE)
    assert "伽马77半导体" not in extractor_mod._chinese_company_aliases_for_stripping(USER_ALICE)

    # After the 5-minute TTL expires (next time bucket), both `alias_map` and derived results refresh together
    clock["now"] = 1200.0 + extractor_mod._ALIAS_CACHE_TTL_SECONDS + 1.0
    bucket_1 = int(clock["now"] // extractor_mod._ALIAS_CACHE_TTL_SECONDS)
    alice_strip_refreshed = extractor_mod._chinese_company_aliases_for_stripping(USER_ALICE)
    alice_groups_refreshed = extractor_mod._build_entity_alias_groups(USER_ALICE)
    assert "伽马77半导体" in alice_strip_refreshed
    assert "阿尔法66科技" not in alice_strip_refreshed
    assert any("伽马77半导体" in grp for grp in alice_groups_refreshed)
    assert not any("阿尔法66科技" in grp for grp in alice_groups_refreshed)
    assert (USER_ALICE, bucket_0) not in extractor_mod._TICKER_ALIASES_CACHE
    assert extractor_mod._TICKER_ALIASES_CACHE[(USER_ALICE, bucket_1)].cn_strip_aliases is alice_strip_refreshed
    assert extractor_mod._TICKER_ALIASES_CACHE[(USER_ALICE, bucket_1)].entity_alias_groups is alice_groups_refreshed

    # Bob's aliases remain isolated from Alice's refreshed aliases
    bob_strip_refreshed = extractor_mod._chinese_company_aliases_for_stripping(USER_BOB)
    assert "贝塔88集团" in bob_strip_refreshed
    assert "伽马77半导体" not in bob_strip_refreshed


@pytest.mark.asyncio
async def test_sync_lead_analyst_personal_coverage_and_canonical_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§6 Decision 2 / G6: bind/transfer writes lead analyst's dalton_coverage idempotently and overlays/redacts workspace_id by Web read permission."""
    from fastapi import HTTPException
    import src.dalton.database.coverage_units as cu_db_mod
    import src.dalton.settings as settings_mod
    import src.dalton.sharing as sharing_mod

    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true")
    monkeypatch.setattr(settings_mod, "is_coverage_units_enabled", lambda: True)

    canon_ws = "11111111-1111-1111-1111-111111111111"
    personal_ws = "33333333-3333-3333-3333-333333333333"
    readable_for: dict[str, set[str]] = {
        USER_ALICE: {canon_ws, personal_ws},
        USER_BOB: {canon_ws},
    }

    async def _fake_req_ws_access(
        ws_id: Any,
        uid: str,
        *,
        channel: str = "web",
        need: str = "read",
        workspace: dict[str, Any] | None = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        wid = str(ws_id or "").strip()
        if wid in readable_for.get(str(uid), set()):
            return {"workspace_id": wid, "user_id": uid}
        raise HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(sharing_mod.require_workspace_access, side_effect=_fake_req_ws_access),
    )

    # Bob already had 700 in personal coverage as tier='watch' -> preserve tier & normalized ticker_key match
    await store.upsert_coverage_rows(
        USER_BOB,
        [
            company_to_row(
                {"ticker": "700", "name": "Tencent Old", "tier": "watch", "market": "HK"},
                user_id=USER_BOB,
                source="chat",
            )
        ],
    )

    unit_alice = {
        "unit_id": "22222222-2222-2222-2222-222222222222",
        "kind": "company",
        "status": "active",
        "owner_user_id": USER_ALICE,
        "lead_analyst_user_id": USER_ALICE,
        "slug": "tencent",
        "display_name": "Tencent",
        "ticker": "0700.HK",
        "workspace_id": canon_ws,
    }
    # First sync writes Alice (new row -> defaults to tier='core', canonical workspace_id attached)
    await coverage_svc.sync_lead_analyst_personal_coverage(unit_alice)
    alice_rows = await store.list_coverage_rows(USER_ALICE)
    assert len(alice_rows) == 1
    assert alice_rows[0]["tier"] == "core"
    assert alice_rows[0]["extra"]["workspace"]["workspace_id"] == canon_ws

    # Alice also has her OWN unrelated NVDA personal workspace; Bob owning canonical NVDA must NOT erase Alice's unrelated workspace_id (GLM L5)
    await store.upsert_coverage_rows(
        USER_ALICE,
        [
            company_to_row(
                {
                    "ticker": "NVDA",
                    "name": "NVIDIA",
                    "tier": "core",
                    "market": "US",
                    "workspace": {"name": "NVDA", "status": "active", "workspace_id": personal_ws},
                },
                user_id=USER_ALICE,
                source="chat",
            )
        ],
    )
    nvda_unit_bob = {
        "unit_id": "44444444-4444-4444-4444-444444444444",
        "kind": "company",
        "status": "active",
        "owner_user_id": USER_BOB,
        "lead_analyst_user_id": USER_BOB,
        "slug": "nvda",
        "display_name": "NVIDIA",
        "ticker": "NVDA",
        "workspace_id": "55555555-5555-5555-5555-555555555555",
    }

    # Transfer to Bob: clears Alice's synced canonical ws & idempotently updates Bob's row preserving 'watch'
    await coverage_svc.clear_unit_from_personal_coverage(unit_alice)
    unit_bob = {**unit_alice, "owner_user_id": USER_BOB, "lead_analyst_user_id": USER_BOB}
    await coverage_svc.sync_lead_analyst_personal_coverage(unit_bob)
    alice_rows_after = await store.list_coverage_rows(USER_ALICE)
    alice_tx_row = next(r for r in alice_rows_after if r["ticker"] == "0700.HK")
    assert "workspace_id" not in alice_tx_row["extra"]["workspace"]
    bob_rows = await store.list_coverage_rows(USER_BOB)
    assert len(bob_rows) == 1
    assert bob_rows[0]["tier"] == "watch"
    assert bob_rows[0]["extra"]["workspace"]["workspace_id"] == canon_ws

    # Read overlay (both get_user_coverage AND GET /coverage summary):
    monkeypatch.setattr(
        cu_db_mod,
        "list_active_company_units_with_ticker",
        create_autospec(
            cu_db_mod.list_active_company_units_with_ticker,
            return_value=[unit_bob, nvda_unit_bob],
        ),
    )
    alice_doc = await coverage_svc.get_user_coverage(USER_ALICE)
    alice_by_t = {c["ticker"]: c for c in alice_doc["companies"]}
    assert alice_by_t["0700.HK"]["workspace"]["status"] == "none"
    assert "workspace_id" not in alice_by_t["0700.HK"]["workspace"]
    assert alice_by_t["NVDA"]["workspace"]["workspace_id"] == personal_ws  # unrelated personal link kept

    bob_doc = await coverage_svc.get_user_coverage(USER_BOB)
    assert bob_doc["companies"][0]["workspace"] == {"name": "Tencent", "status": "active", "workspace_id": canon_ws}
    bob_summary = await coverage_svc.list_user_coverage_summary(USER_BOB)
    assert bob_summary["companies"][0]["workspace"]["workspace_id"] == canon_ws

    # Revoke Bob's Web read permission -> both coverage.yaml AND GET /coverage redact canon_ws
    readable_for[USER_BOB] = set()
    bob_doc_revoked = await coverage_svc.get_user_coverage(USER_BOB)
    assert bob_doc_revoked["companies"][0]["workspace"]["status"] == "none"
    assert "workspace_id" not in bob_doc_revoked["companies"][0]["workspace"]
    bob_summary_revoked = await coverage_svc.list_user_coverage_summary(USER_BOB)
    assert bob_summary_revoked["companies"][0]["workspace"]["status"] == "none"
    assert "workspace_id" not in bob_summary_revoked["companies"][0]["workspace"]

    # Archive cleanup clears synced canonical workspace_id even after unit stops appearing in active list
    await coverage_svc.clear_unit_from_personal_coverage(unit_bob)
    bob_after_archive = await store.list_coverage_rows(USER_BOB)
    assert "workspace_id" not in bob_after_archive[0]["extra"]["workspace"]


@pytest.mark.asyncio
async def test_personal_sync_preserves_manual_ws_and_infers_cnhk_bse_markets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Items 5 & 10: manual workspace_id is never overwritten/deleted on sync/clear, same-ws rebind skips personal sync, and new HK/CN/BSE rows get accurate market."""
    import src.dalton.coverage.units as cu_svc_mod
    import src.dalton.database.coverage_units as cu_db_mod
    import src.dalton.settings as settings_mod

    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true")
    monkeypatch.setattr(settings_mod, "is_coverage_units_enabled", lambda: True)

    canon_ws = "66666666-6666-6666-6666-666666666666"
    manual_ws = "77777777-7777-7777-7777-777777777777"

    # 1) User already has AAPL with manual workspace_id="manual_ws" -> sync must preserve manual_ws & record _synced_*
    await store.upsert_coverage_rows(
        USER_ALICE,
        [
            company_to_row(
                {
                    "ticker": "AAPL",
                    "name": "Apple",
                    "tier": "watch",
                    "market": "US",
                    "workspace": {"name": "My Apple", "status": "active", "workspace_id": manual_ws},
                },
                user_id=USER_ALICE,
                source="chat",
            )
        ],
    )
    aapl_unit = {
        "unit_id": "88888888-8888-8888-8888-888888888888",
        "kind": "company",
        "status": "active",
        "owner_user_id": USER_ALICE,
        "lead_analyst_user_id": USER_ALICE,
        "slug": "apple",
        "display_name": "Apple",
        "ticker": "AAPL",
        "workspace_id": canon_ws,
    }
    await coverage_svc.sync_lead_analyst_personal_coverage(aapl_unit)
    aapl_rows = await store.list_coverage_rows(USER_ALICE)
    assert aapl_rows[0]["extra"]["workspace"]["workspace_id"] == manual_ws
    assert aapl_rows[0]["extra"]["_synced_unit_id"] == aapl_unit["unit_id"]
    assert aapl_rows[0]["extra"]["_synced_workspace_id"] == canon_ws

    # Clear after transfer/archive removes _synced_* metadata WITHOUT touching manual_ws
    await coverage_svc.clear_unit_from_personal_coverage(aapl_unit)
    aapl_after = await store.list_coverage_rows(USER_ALICE)
    assert aapl_after[0]["extra"]["workspace"]["workspace_id"] == manual_ws
    assert "_synced_unit_id" not in aapl_after[0]["extra"]

    # Manual row pointing to canon_ws WITHOUT _synced_unit_id is never cleared on transfer/archive
    await store.upsert_coverage_rows(
        USER_BOB,
        [
            company_to_row(
                {
                    "ticker": "AAPL",
                    "name": "Apple Manual",
                    "tier": "watch",
                    "market": "US",
                    "workspace": {"name": "Apple", "status": "active", "workspace_id": canon_ws},
                },
                user_id=USER_BOB,
                source="chat",
            )
        ],
    )
    assert await coverage_svc.clear_unit_from_personal_coverage(aapl_unit) is None
    assert (await store.list_coverage_rows(USER_BOB))[0]["extra"]["workspace"]["workspace_id"] == canon_ws

    # 2) Same-workspace duplicate bind skips personal coverage sync (does not recreate removed row)
    fake_unit = {**aapl_unit, "workspace_id": canon_ws}
    async def _fake_bind(uid: str, wsid: str, actor: str, *, conn: Any = None, previous_out: Any = None) -> dict[str, Any]:
        if isinstance(previous_out, dict):
            previous_out.update(fake_unit)
        return dict(fake_unit)

    from psycopg import AsyncConnection

    class _TxCm:
        async def __aenter__(self) -> "_TxCm":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    class _ConnWrap:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        async def __aenter__(self) -> Any:
            return self._inner

        async def __aexit__(self, *args: Any) -> None:
            return None

    import src.dalton.sharing as sharing_mod
    from fastapi import HTTPException

    async def _allow_ws_read(
        ws_id: Any,
        uid: str,
        *,
        channel: str = "web",
        need: str = "read",
        workspace: dict[str, Any] | None = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        if str(ws_id or "").strip() == canon_ws:
            return {"workspace_id": canon_ws, "user_id": uid}
        raise HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(sharing_mod.require_workspace_access, side_effect=_allow_ws_read),
    )
    tx_conn = create_autospec(AsyncConnection, instance=True)
    tx_conn.transaction.return_value = _TxCm()
    sync_spy = create_autospec(coverage_svc.sync_lead_analyst_personal_coverage)
    monkeypatch.setattr(cu_db_mod, "get_db_connection", create_autospec(cu_db_mod.get_db_connection, return_value=_ConnWrap(tx_conn)))
    monkeypatch.setattr(cu_db_mod, "bind_coverage_unit_workspace", create_autospec(cu_db_mod.bind_coverage_unit_workspace, side_effect=_fake_bind))
    monkeypatch.setattr(coverage_svc, "sync_lead_analyst_personal_coverage", sync_spy)
    same = await cu_svc_mod.bind_coverage_unit_workspace(aapl_unit["unit_id"], canon_ws, USER_ALICE)
    assert same["workspace_id"] == canon_ws
    sync_spy.assert_not_awaited()

    # 3) New personal rows created for naked HK ("700") & BSE ("920001.BJ") infer accurate markets
    monkeypatch.undo()
    _wire_coverage_store(monkeypatch, store)
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true")
    monkeypatch.setattr(settings_mod, "is_coverage_units_enabled", lambda: True)
    hk_row = await coverage_svc.sync_lead_analyst_personal_coverage(
        {
            "unit_id": "99999999-9999-9999-9999-999999999901",
            "kind": "company",
            "status": "active",
            "owner_user_id": "u-new-mkt",
            "lead_analyst_user_id": "u-new-mkt",
            "slug": "tencent-hk",
            "display_name": "Tencent",
            "ticker": "700",
            "workspace_id": "10101010-1010-1010-1010-101010101010",
        }
    )
    bj_row = await coverage_svc.sync_lead_analyst_personal_coverage(
        {
            "unit_id": "99999999-9999-9999-9999-999999999902",
            "kind": "company",
            "status": "active",
            "owner_user_id": "u-new-mkt",
            "lead_analyst_user_id": "u-new-mkt",
            "slug": "bse-co",
            "display_name": "BSE Co",
            "ticker": "920001.BJ",
            "workspace_id": "20202020-2020-2020-2020-202020202020",
        }
    )
    assert hk_row is not None and hk_row["market"] == "HK"
    assert bj_row is not None and bj_row["market"] == "CN"


@pytest.mark.asyncio
async def test_add_tickers_redacts_unreadable_ws_and_preserves_manual_vs_synced_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Items 3 & 4:
    - POST /coverage (add_user_coverage_tickers) redacts unreadable canonical workspace_id.
    - Manual workspace link equal to canon_ws is marked manual and preserved across sync, re-add, and clear.
    - System-synced _synced_* metadata survives re-add and still clears on transfer/archive.
    """
    from fastapi import HTTPException
    import src.dalton.database.coverage_units as cu_db_mod
    import src.dalton.settings as settings_mod
    import src.dalton.sharing as sharing_mod

    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true")
    monkeypatch.setattr(settings_mod, "is_coverage_units_enabled", lambda: True)

    canon_ws = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    readable_for: dict[str, set[str]] = {
        USER_ALICE: set(),
        USER_BOB: {canon_ws},
    }

    async def _req_ws(
        ws_id: Any,
        uid: str,
        *,
        channel: str = "web",
        need: str = "read",
        workspace: dict[str, Any] | None = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        wid = str(ws_id or "").strip()
        if wid in readable_for.get(str(uid), set()):
            return {"workspace_id": wid, "user_id": uid}
        raise HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(sharing_mod.require_workspace_access, side_effect=_req_ws),
    )

    nvda_unit_alice = {
        "unit_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "kind": "company",
        "status": "active",
        "owner_user_id": USER_ALICE,
        "lead_analyst_user_id": USER_ALICE,
        "slug": "nvda",
        "display_name": "NVIDIA",
        "ticker": "NVDA",
        "workspace_id": canon_ws,
    }
    aapl_unit_bob = {
        "unit_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "kind": "company",
        "status": "active",
        "owner_user_id": USER_BOB,
        "lead_analyst_user_id": USER_BOB,
        "slug": "aapl",
        "display_name": "Apple",
        "ticker": "AAPL",
        "workspace_id": canon_ws,
    }
    orig_list_active_units = cu_db_mod.list_active_company_units_with_ticker
    monkeypatch.setattr(
        cu_db_mod,
        "list_active_company_units_with_ticker",
        create_autospec(
            orig_list_active_units,
            return_value=[nvda_unit_alice, aapl_unit_bob],
        ),
    )

    # Item 3 + Item 4 (system-written link survives re-add and still clears on archive/transfer)
    await coverage_svc.sync_lead_analyst_personal_coverage(nvda_unit_alice)
    readded_alice = await coverage_svc.add_user_coverage_tickers(
        USER_ALICE,
        ["NVDA"],
        tier="watch",
        prevalidated={"NVDA": {"ticker": "NVDA", "name": "NVIDIA", "market": "US"}},
        ensure_automation=False,
        sync_home=False,
    )
    assert readded_alice["added"][0]["workspace"]["status"] == "none"
    assert "workspace_id" not in readded_alice["added"][0]["workspace"]
    alice_row_after_readd = (await store.list_coverage_rows(USER_ALICE))[0]
    assert alice_row_after_readd["extra"]["_synced_unit_id"] == nvda_unit_alice["unit_id"]
    assert alice_row_after_readd["extra"]["_synced_ws_written"] is True

    await coverage_svc.clear_unit_from_personal_coverage(nvda_unit_alice)
    alice_cleared = (await store.list_coverage_rows(USER_ALICE))[0]
    assert "workspace_id" not in alice_cleared["extra"]["workspace"]
    assert "_synced_unit_id" not in alice_cleared["extra"]

    # Item 4 (user manual link happens to equal canon_ws): kept across sync, re-add, and clear
    await store.upsert_coverage_rows(
        USER_BOB,
        [
            company_to_row(
                {
                    "ticker": "AAPL",
                    "name": "Apple Manual",
                    "tier": "watch",
                    "market": "US",
                    "workspace": {"name": "Apple Manual", "status": "active", "workspace_id": canon_ws},
                },
                user_id=USER_BOB,
                source="chat",
            )
        ],
    )
    await coverage_svc.sync_lead_analyst_personal_coverage(aapl_unit_bob)
    bob_synced = (await store.list_coverage_rows(USER_BOB))[0]
    assert bob_synced["extra"]["_manual_workspace_id"] == canon_ws
    assert bob_synced["extra"]["_synced_ws_written"] is False
    assert bob_synced["extra"]["_synced_unit_id"] == aapl_unit_bob["unit_id"]

    await coverage_svc.add_user_coverage_tickers(
        USER_BOB,
        ["AAPL"],
        tier="core",
        prevalidated={"AAPL": {"ticker": "AAPL", "name": "Apple", "market": "US"}},
        ensure_automation=False,
        sync_home=False,
    )
    bob_readded = (await store.list_coverage_rows(USER_BOB))[0]
    assert bob_readded["extra"]["_manual_workspace_id"] == canon_ws
    assert bob_readded["extra"]["_synced_ws_written"] is False
    assert bob_readded["extra"]["workspace"]["workspace_id"] == canon_ws

    await coverage_svc.clear_unit_from_personal_coverage(aapl_unit_bob)
    bob_after_clear = (await store.list_coverage_rows(USER_BOB))[0]
    assert bob_after_clear["extra"]["workspace"]["workspace_id"] == canon_ws
    assert bob_after_clear["extra"]["_manual_workspace_id"] == canon_ws
    assert "_synced_unit_id" not in bob_after_clear["extra"]

    # Non-lead colleague's readable manual link equal to canon_ws is preserved on GET /coverage
    nvda_other_lead = {
        **nvda_unit_alice,
        "owner_user_id": USER_ALICE,
        "lead_analyst_user_id": USER_ALICE,
    }
    monkeypatch.setattr(
        cu_db_mod,
        "list_active_company_units_with_ticker",
        create_autospec(
            orig_list_active_units,
            return_value=[nvda_other_lead],
        ),
    )
    await store.upsert_coverage_rows(
        USER_BOB,
        [
            company_to_row(
                {
                    "ticker": "NVDA",
                    "name": "NVIDIA",
                    "tier": "core",
                    "market": "US",
                    "workspace": {"name": "NVDA", "status": "active", "workspace_id": canon_ws},
                    "_manual_workspace_id": canon_ws,
                    "_synced_ws_written": False,
                },
                user_id=USER_BOB,
                source="chat",
            )
        ],
    )
    # Item 14 / C4: non-lead user's manual link equal to canon_ws is hidden in coverage.yaml/get_user_coverage,
    # while DB row association stays intact.
    bob_doc = await coverage_svc.get_user_coverage(USER_BOB)
    nvda_entry = next(c for c in bob_doc["companies"] if c["ticker"] == "NVDA")
    assert nvda_entry["workspace"]["status"] == "none"
    assert "workspace_id" not in nvda_entry["workspace"]
    bob_nvda_row = await store.get_coverage_row(USER_BOB, "NVDA")
    assert bob_nvda_row["extra"]["workspace"]["workspace_id"] == canon_ws
    assert bob_nvda_row["extra"]["_manual_workspace_id"] == canon_ws


@pytest.mark.asyncio
async def test_personal_roster_endpoints_degrade_when_coverage_units_registry_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 7 Item 4: when canonical coverage-units registry fails, personal roster reads
    and group/add responses keep personal rows, omit unconfirmed synced links, and flag
    units_unavailable=True instead of raising HTTP 500."""
    import src.dalton.database.coverage_units as cu_db_mod
    import src.dalton.settings as settings_mod
    import src.dalton.sharing as sharing_mod
    from fastapi import HTTPException

    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true")
    monkeypatch.setattr(settings_mod, "is_coverage_units_enabled", lambda: True)
    synced_ws = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    manual_ws = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    async def _req_ws(ws_id: Any, uid: str, **kwargs: Any) -> dict[str, Any]:
        wid = str(ws_id or "").strip()
        if wid in {manual_ws, synced_ws}:
            return {"workspace_id": wid, "user_id": uid}
        raise HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(sharing_mod.require_workspace_access, side_effect=_req_ws),
    )
    monkeypatch.setattr(
        coverage_svc,
        "sync_user_coverage_to_home",
        create_autospec(coverage_svc.sync_user_coverage_to_home, return_value=True),
    )
    monkeypatch.setattr(
        coverage_svc,
        "ensure_user_earnings_calendar_automation",
        create_autospec(
            coverage_svc.ensure_user_earnings_calendar_automation,
            return_value={"ok": True, "action": "exists", "automation_id": "auto-cal-1", "name": "dalton-earnings-calendar"},
        ),
    )
    monkeypatch.setattr(
        coverage_svc,
        "_user_workspace_tickers",
        create_autospec(coverage_svc._user_workspace_tickers, return_value={}),
    )

    async def _fake_validate(ticker: str) -> dict[str, Any] | None:
        sym = ticker.strip().upper()
        return {"ticker": sym, "name": f"{sym} Corp", "market": "US", "sector": "Technology", "industry": "Semiconductors"}

    monkeypatch.setattr(
        coverage_svc,
        "validate_and_enrich_ticker",
        create_autospec(coverage_svc.validate_and_enrich_ticker, side_effect=_fake_validate),
    )

    await store.upsert_coverage_rows(
        USER_ALICE,
        [
            company_to_row(
                {
                    "ticker": "NVDA",
                    "name": "NVIDIA",
                    "tier": "core",
                    "market": "US",
                    "workspace": {"name": "NVDA", "status": "active", "workspace_id": synced_ws},
                    "_synced_ws_written": True,
                    "_synced_unit_id": "unit-nvda",
                },
                user_id=USER_ALICE,
                source="chat",
            ),
            company_to_row(
                {
                    "ticker": "AAPL",
                    "name": "Apple",
                    "tier": "watch",
                    "market": "US",
                    "workspace": {"name": "Apple Manual", "status": "active", "workspace_id": manual_ws},
                    "_manual_workspace_id": manual_ws,
                    "_synced_ws_written": False,
                },
                user_id=USER_ALICE,
                source="chat",
            ),
        ],
    )

    orig_list_units = cu_db_mod.list_active_company_units_with_ticker
    monkeypatch.setattr(
        cu_db_mod,
        "list_active_company_units_with_ticker",
        create_autospec(orig_list_units, side_effect=RuntimeError("db down")),
    )

    doc = await coverage_svc.get_user_coverage(USER_ALICE)
    assert doc["units_unavailable"] is True
    by_t = {c["ticker"]: c for c in doc["companies"]}
    # Registry failed: synced binding is preserved cleanly instead of taking the "no active unit" path
    assert by_t["NVDA"]["workspace"]["workspace_id"] == synced_ws
    assert by_t["AAPL"]["workspace"]["workspace_id"] == manual_ws
    rendered = await coverage_svc.render_coverage_yaml(USER_ALICE)
    assert "units_unavailable" not in rendered

    summary = await coverage_svc.list_user_coverage_summary(USER_ALICE)
    assert summary["units_unavailable"] is True
    assert {c["ticker"] for c in summary["companies"]} == {"NVDA", "AAPL"}

    added = await coverage_svc.add_user_coverage_tickers(
        USER_ALICE,
        ["MSFT"],
        tier="watch",
        ensure_automation=False,
        sync_home=False,
    )
    assert added["units_unavailable"] is True
    assert [c["ticker"] for c in added["added"]] == ["MSFT"]

    app = FastAPI()
    app.dependency_overrides[get_current_user_id] = lambda: USER_ALICE
    app.include_router(dalton_router)
    client = TestClient(app, raise_server_exceptions=False)
    cov_resp = client.get("/api/v1/dalton/coverage")
    assert cov_resp.status_code == 200
    cov_body = cov_resp.json()
    assert cov_body["units_unavailable"] is True
    assert {c["ticker"] for c in cov_body["companies"]} == {"NVDA", "AAPL", "MSFT"}

    grp_resp = client.get("/api/v1/dalton/coverage/groups")
    assert grp_resp.status_code == 200
    grp_body = grp_resp.json()
    assert grp_body["units_unavailable"] is True
    assert "groups" in grp_body


@pytest.mark.asyncio
async def test_coverage_units_flag_off_and_permission_db_error_preserve_persisted_ws_and_market(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 8 Items 6 & 7.1:
    - When coverage_units is disabled, infer_market preserves 49a56df4b ('700' -> 'US')
      and get_user_coverage preserves persisted workspace_id without checking permissions.
    - When enabled and _caller_can_read_workspace hits a DB error, keep persisted workspace_id
      and surface units_unavailable=True; only confirmed HTTPException 403/404 clears it."""
    import src.dalton.coverage.models as models_mod
    import src.dalton.database.coverage_units as cu_db_mod
    import src.dalton.settings as settings_mod
    import src.dalton.sharing as sharing_mod
    from fastapi import HTTPException

    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)
    persisted_ws = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    await store.upsert_coverage_rows(
        USER_ALICE,
        [
            company_to_row(
                {
                    "ticker": "NVDA",
                    "name": "NVIDIA",
                    "tier": "core",
                    "market": "US",
                    "workspace": {"name": "NVDA", "status": "active", "workspace_id": persisted_ws},
                },
                user_id=USER_ALICE,
                source="chat",
            )
        ],
    )

    # Flag OFF -> infer_market("700") is "US", zero permission/unit queries, persisted_ws stays intact
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "false")
    monkeypatch.setattr(settings_mod, "is_coverage_units_enabled", lambda: False)
    assert models_mod.infer_market("700") == "US"
    assert models_mod.infer_market("0700.HK") == "HK"
    assert models_mod.infer_market("600519") == "US"
    assert models_mod.infer_market("600519.SH") == "CN"

    orig_req_ws = sharing_mod.require_workspace_access
    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(orig_req_ws, side_effect=AssertionError("flag off must not check ws access")),
    )
    doc_off = await coverage_svc.get_user_coverage(USER_ALICE)
    assert "units_unavailable" not in doc_off
    assert doc_off["companies"][0]["workspace"]["workspace_id"] == persisted_ws

    # Flag ON -> infer_market("700") == "HK"; transient DB error in read check preserves persisted_ws + flags units_unavailable
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true")
    monkeypatch.setattr(settings_mod, "is_coverage_units_enabled", lambda: True)
    assert models_mod.infer_market("700") == "HK"
    monkeypatch.setattr(
        cu_db_mod,
        "list_active_company_units_with_ticker",
        create_autospec(cu_db_mod.list_active_company_units_with_ticker, return_value=[]),
    )
    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(orig_req_ws, side_effect=RuntimeError("transient pool timeout")),
    )
    doc_err = await coverage_svc.get_user_coverage(USER_ALICE)
    assert doc_err["units_unavailable"] is True
    assert doc_err["companies"][0]["workspace"]["workspace_id"] == persisted_ws

    # HTTPException(503) from sharing lookup ALSO treated as unavailable (keeps persisted_ws + strips YAML)
    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(
            orig_req_ws,
            side_effect=HTTPException(status_code=503, detail="Workspace sharing settings unavailable"),
        ),
    )
    doc_503 = await coverage_svc.get_user_coverage(USER_ALICE)
    assert doc_503["units_unavailable"] is True
    assert doc_503["companies"][0]["workspace"]["workspace_id"] == persisted_ws
    yaml_503 = await coverage_svc.render_coverage_yaml(USER_ALICE)
    assert "units_unavailable" not in yaml_503
    assert persisted_ws in yaml_503

    # Confirmed HTTPException(403) clears persisted_ws cleanly
    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(orig_req_ws, side_effect=HTTPException(status_code=403, detail="forbidden")),
    )
    doc_denied = await coverage_svc.get_user_coverage(USER_ALICE)
    assert "units_unavailable" not in doc_denied
    assert "workspace_id" not in doc_denied["companies"][0]["workspace"]


@pytest.mark.asyncio
async def test_group_write_isolates_registry_failure_in_savepoint_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 8 Item 4: add_coverage_group and resolve_coverage_group run registry
    overlay queries inside a SAVEPOINT so catching registry errors never leaves
    the outer tx in InFailedSqlTransaction."""
    import src.dalton.database.coverage_units as cu_db_mod
    import src.dalton.settings as settings_mod

    store = _InMemoryCoverageStore()
    tx_state = {"depth": 0, "aborted_levels": set(), "savepoints": 0, "committed": False}

    class _TxCm:
        def __init__(self, is_savepoint: bool) -> None:
            self._sp = is_savepoint

        async def __aenter__(self) -> None:
            tx_state["depth"] += 1
            if self._sp:
                tx_state["savepoints"] += 1

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            depth = tx_state["depth"]
            tx_state["depth"] -= 1
            if exc_type is not None:
                # Exception inside SAVEPOINT rolls back ONLY the savepoint; outer tx stays healthy!
                if not self._sp:
                    tx_state["aborted_levels"].add(depth)
            elif not self._sp and depth == 1:
                if tx_state["aborted_levels"]:
                    raise RuntimeError("InFailedSqlTransaction")
                tx_state["committed"] = True
            return False

    class _SpConn:
        def transaction(self) -> _TxCm:
            return _TxCm(tx_state["depth"] >= 1)

    _wire_coverage_store(monkeypatch, store)
    conn = _SpConn()

    @asynccontextmanager
    async def _conn_ctx(existing: Any = None):
        yield existing if existing is not None else conn

    monkeypatch.setattr(coverage_db, "get_db_connection", _conn_ctx)
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true")
    monkeypatch.setattr(settings_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(coverage_svc, "sync_user_coverage_to_home", create_autospec(coverage_svc.sync_user_coverage_to_home, return_value=True))
    monkeypatch.setattr(
        coverage_svc,
        "ensure_user_earnings_calendar_automation",
        create_autospec(coverage_svc.ensure_user_earnings_calendar_automation, return_value={"ok": True, "action": "exists"}),
    )
    monkeypatch.setattr(coverage_svc, "_user_workspace_tickers", create_autospec(coverage_svc._user_workspace_tickers, return_value={}))
    orig_screener = coverage_svc._fetch_yf_screener_rows
    monkeypatch.setattr(
        coverage_svc,
        "_fetch_yf_screener_rows",
        create_autospec(
            orig_screener,
            return_value=[{"symbol": "NVDA", "companyName": "NVIDIA Corp", "exchangeShortName": "NMS"}],
        ),
    )

    def _failing_registry(*, conn: Any = None):
        if conn is not None and not tx_state["savepoints"]:
            tx_state["aborted_levels"].add(tx_state["depth"])
        raise RuntimeError("registry query error inside tx")

    monkeypatch.setattr(
        cu_db_mod,
        "list_active_company_units_with_ticker",
        create_autospec(cu_db_mod.list_active_company_units_with_ticker, side_effect=_failing_registry),
    )

    added = await coverage_svc.add_coverage_group(
        USER_ALICE,
        label="Semis",
        criteria={"country": "US", "industry": "Semiconductors"},
        default_tier="core",
    )
    assert added["ok"] is True
    assert added["units_unavailable"] is True
    assert tx_state["savepoints"] >= 1
    assert tx_state["committed"] is True

    tx_state["committed"] = False
    tx_state["savepoints"] = 0
    monkeypatch.setattr(
        coverage_svc,
        "_fetch_yf_screener_rows",
        create_autospec(
            orig_screener,
            return_value=[
                {"symbol": "NVDA", "companyName": "NVIDIA Corp", "exchangeShortName": "NMS"},
                {"symbol": "AMD", "companyName": "AMD Inc", "exchangeShortName": "NMS"},
            ],
        ),
    )
    resolved = await coverage_svc.resolve_coverage_group(USER_ALICE, added["group"]["group_id"])
    assert resolved["ok"] is True
    assert resolved["units_unavailable"] is True
    assert tx_state["savepoints"] >= 1
    assert tx_state["committed"] is True


@pytest.mark.asyncio
async def test_unreadable_owner_tier_404_is_a_failed_lookup_not_a_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner-tier-unavailable 404 keeps the binding; a plain 404 is a denial."""
    from fastapi import HTTPException

    import src.dalton.coverage.service as svc
    import src.dalton.sharing as sharing_mod

    check = create_autospec(sharing_mod.require_workspace_access, side_effect=sharing_mod.OwnerTierUnavailable())
    monkeypatch.setattr(sharing_mod, "require_workspace_access", check)
    assert await svc._caller_can_read_workspace("ws-1", "lead-1") is None

    check.side_effect = HTTPException(status_code=404, detail="Workspace not found")
    assert await svc._caller_can_read_workspace("ws-1", "lead-1") is False


@pytest.mark.asyncio
async def test_phase1_c4_non_lead_synced_or_unmarked_canonical_ws_is_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C4: coverage.yaml only outputs canonical workspace_id + active to current lead_analyst_user_id."""
    import src.dalton.database.coverage_units as cu_db_mod
    import src.dalton.settings as settings_mod
    import src.dalton.sharing as sharing_mod

    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true")
    monkeypatch.setattr(settings_mod, "is_coverage_units_enabled", lambda: True)

    canon_ws = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(
            sharing_mod.require_workspace_access,
            side_effect=lambda ws_id, uid, **_kw: {"workspace_id": str(ws_id), "user_id": uid},
        ),
    )
    unit = {
        "unit_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "kind": "company",
        "status": "active",
        "owner_user_id": USER_ALICE,
        "lead_analyst_user_id": USER_BOB,
        "slug": "nvda",
        "display_name": "NVIDIA",
        "ticker": "NVDA",
        "workspace_id": canon_ws,
    }
    monkeypatch.setattr(
        cu_db_mod,
        "list_active_company_units_with_ticker",
        create_autospec(cu_db_mod.list_active_company_units_with_ticker, return_value=[unit]),
    )
    # Non-lead prior owner (USER_ALICE) still has persisted canon_ws row without manual tag -> cleared
    await store.upsert_coverage_rows(
        USER_ALICE,
        [
            company_to_row(
                {
                    "ticker": "NVDA",
                    "name": "NVIDIA",
                    "tier": "core",
                    "market": "US",
                    "workspace": {"name": "NVDA", "status": "active", "workspace_id": canon_ws},
                },
                user_id=USER_ALICE,
                source="chat",
            )
        ],
    )
    await store.upsert_coverage_rows(
        USER_BOB,
        [
            company_to_row(
                {"ticker": "NVDA", "name": "NVIDIA", "tier": "core", "market": "US"},
                user_id=USER_BOB,
                source="chat",
            )
        ],
    )

    alice_doc = await coverage_svc.get_user_coverage(USER_ALICE)
    bob_doc = await coverage_svc.get_user_coverage(USER_BOB)
    alice_nvda = next(c for c in alice_doc["companies"] if c["ticker"] == "NVDA")
    bob_nvda = next(c for c in bob_doc["companies"] if c["ticker"] == "NVDA")
    assert alice_nvda["workspace"]["status"] == "none"
    assert "workspace_id" not in alice_nvda["workspace"]
    assert bob_nvda["workspace"]["status"] == "active"
    assert bob_nvda["workspace"]["workspace_id"] == canon_ws


@pytest.mark.asyncio
async def test_c4_hides_canonical_workspace_for_non_lead_even_when_manual_row_is_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 14 / C4: non-lead user's manual association pointing at canonical unit workspace is
    hidden from coverage.yaml while preserving DB row."""
    import src.dalton.database.coverage_units as cu_db_mod
    import src.dalton.settings as settings_mod
    import src.dalton.sharing as sharing_mod

    store = _InMemoryCoverageStore()
    _wire_coverage_store(monkeypatch, store)
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true")
    monkeypatch.setattr(settings_mod, "is_coverage_units_enabled", lambda: True)

    canon_ws = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(
            sharing_mod.require_workspace_access,
            side_effect=lambda ws_id, uid, **_kw: {"workspace_id": str(ws_id), "user_id": uid},
        ),
    )
    unit = {
        "unit_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "kind": "company",
        "status": "active",
        "owner_user_id": USER_ALICE,
        "lead_analyst_user_id": USER_BOB,
        "slug": "nvda",
        "display_name": "NVIDIA",
        "ticker": "NVDA",
        "workspace_id": canon_ws,
    }
    monkeypatch.setattr(
        cu_db_mod,
        "list_active_company_units_with_ticker",
        create_autospec(cu_db_mod.list_active_company_units_with_ticker, return_value=[unit]),
    )
    row = company_to_row(
        {
            "ticker": "NVDA",
            "name": "NVIDIA",
            "tier": "core",
            "market": "US",
            "workspace": {"name": "NVDA", "status": "active", "workspace_id": canon_ws},
        },
        user_id=USER_ALICE,
        source="chat",
    )
    row["extra"]["_manual_workspace_id"] = canon_ws
    await store.upsert_coverage_rows(USER_ALICE, [row])

    alice_doc = await coverage_svc.get_user_coverage(USER_ALICE)
    alice_nvda = next(c for c in alice_doc["companies"] if c["ticker"] == "NVDA")
    assert alice_nvda["workspace"]["status"] == "none"
    assert "workspace_id" not in alice_nvda["workspace"]
    # Persisted DB row stays intact (not stripped in DB)
    raw_rows = await store.list_coverage_rows(USER_ALICE)
    assert raw_rows[0]["extra"]["workspace"]["workspace_id"] == canon_ws
    assert raw_rows[0]["extra"]["_manual_workspace_id"] == canon_ws


@pytest.mark.asyncio
async def test_owner_tier_unavailable_is_still_a_plain_404_to_http_clients() -> None:
    import src.dalton.sharing as sharing_mod

    exc = sharing_mod.OwnerTierUnavailable()
    assert exc.status_code == 404 and exc.detail == "Workspace not found"
