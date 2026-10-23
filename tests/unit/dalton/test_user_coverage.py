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
    row_to_company,
    validate_earnings_calendar_ledger,
)
import src.dalton.coverage.service as coverage_svc
import src.dalton.database.coverage as coverage_db
import src.dalton.database.watch as watch_db
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
        stale: bool = True,
        conn: Any = None,
    ) -> list[str]:
        _ = conn
        uid = str(user_id).strip()
        changed: list[str] = []
        for t in tickers:
            sym = str(t).strip().upper()
            row = self.rows.get((uid, sym))
            if row is not None:
                extra = dict(row.get("extra") or {})
                if stale:
                    extra["group_stale"] = True
                else:
                    extra.pop("group_stale", None)
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
        label = self.groups[(uid, gid)].get("label")
        removed: list[str] = []
        kept_shared: list[str] = []
        kept_manual: list[str] = []
        for key in list(self.rows.keys()):
            if key[0] != uid:
                continue
            row = self.rows[key]
            gids = extract_row_group_ids(row)
            if gid not in gids:
                continue
            remaining = [g for g in gids if g != gid]
            src = str(row.get("source") or "chat").strip().lower()
            if src == "group" and not remaining:
                removed.append(key[1])
                del self.rows[key]
            else:
                extra = dict(row.get("extra") or {})
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


def _wire_coverage_store(monkeypatch: pytest.MonkeyPatch, store: _InMemoryCoverageStore) -> AsyncConnectionPool:
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
            coverage_svc._user_workspace_tickers,
            return_value={"NVDA": "ws-nvda-123"},
        ),
    )

    tool = build_coverage_tools(role="chief_of_staff")[0]
    cfg = {"configurable": {"user_id": USER_ALICE}}

    # 1) add specific tickers (one valid, one invalid) -> non-empty roster triggers idempotent ensure of dalton-earnings-calendar
    add_out = json.loads(
        await tool.ainvoke(
            {"action": "add", "tickers": ["NVDA", "FAKE999"], "tier": "core"},
            config=cfg,
        )
    )
    assert add_out["ok"] is True
    assert [x["ticker"] for x in add_out["added"]] == ["NVDA"]
    assert add_out["not_found"] == ["FAKE999"]
    assert auto_created_calls == [USER_ALICE]
    assert len(home_writes) >= 1

    # 2) set_tier
    tier_out = json.loads(
        await tool.ainvoke(
            {"action": "set_tier", "tickers": ["NVDA"], "tier": "watch"},
            config=cfg,
        )
    )
    assert tier_out["ok"] is True
    assert tier_out["updated"] == ["NVDA"]
    assert (await coverage_pkg.get_user_tier(USER_ALICE, "NVDA")) == "watch"
    # Set back to core so we verify group sync preserves user's manual tier
    await tool.ainvoke({"action": "set_tier", "tickers": ["NVDA"], "tier": "core"}, config=cfg)

    # 3) preview_group -> does NOT write to DB
    prev_out = json.loads(
        await tool.ainvoke(
            {
                "action": "preview_group",
                "label": "美股半导体大票",
                "criteria": {"country": "US", "industry": "Semiconductors", "market_cap_min": 50_000_000_000},
            },
            config=cfg,
        )
    )
    assert prev_out["ok"] is True
    assert prev_out["preview_only"] is True
    assert [c["ticker"] for c in prev_out["companies"]] == ["NVDA", "AVGO", "AMD"]
    assert await store.count_coverage_rows(USER_ALICE) == 1  # Still only NVDA

    # 4) add_group -> writes group + AVGO & AMD with source='group', while NVDA keeps tier='core' & source='chat'
    grp_out = json.loads(
        await tool.ainvoke(
            {
                "action": "add_group",
                "label": "美股半导体大票",
                "criteria": {"country": "US", "industry": "Semiconductors"},
                "default_tier": "watch",
            },
            config=cfg,
        )
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
    assert amd_row["extra"].get("group_stale") is True

    # 6) list shows stale_tickers
    list_out = json.loads(await tool.ainvoke({"action": "list"}, config=cfg))
    assert "AMD" in list_out["stale_tickers"]

    # 7) remove_group removes group-only entries (AVGO, AMD, QCOM) and keeps NVDA (added via chat)
    rm_grp_out = json.loads(
        await tool.ainvoke({"action": "remove_group", "group_id": group_id}, config=cfg)
    )
    assert rm_grp_out["ok"] is True
    assert set(rm_grp_out["removed_tickers"]) == {"AMD", "AVGO", "QCOM"}
    assert await store.get_coverage_row(USER_ALICE, "NVDA") is not None

    # 8) remove ticker with active workspace warns that workspace and automations are not deleted
    rm_out = json.loads(
        await tool.ainvoke({"action": "remove", "tickers": ["NVDA"]}, config=cfg)
    )
    assert rm_out["ok"] is True
    assert rm_out["removed"] == ["NVDA"]
    assert "workspace 和自动任务不会被删除" in (rm_out["workspace_notice"] or "")


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

    from scripts.dalton import build_coverage as build_cov_mod
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

    # build_coverage.sync_to_user_coverage_db returns integer exit code 0 on success and 1 on error
    import asyncio

    orig_run_seed = seed_mod.run_seed
    run_seed_mock = create_autospec(
        orig_run_seed,
        return_value={"ok": True, "mode": "applied", "written_count": 105},
    )
    monkeypatch.setattr(seed_mod, "run_seed", run_seed_mock)
    rc_ok = await asyncio.to_thread(
        build_cov_mod.sync_to_user_coverage_db,
        user_id=USER_ALICE,
        yaml_path=COVERAGE_YAML_PATH,
    )
    assert rc_ok == 0 and isinstance(rc_ok, int)

    run_seed_mock.return_value = {"ok": False, "error": "boom"}
    rc_err = await asyncio.to_thread(
        build_cov_mod.sync_to_user_coverage_db,
        user_id=USER_ALICE,
        yaml_path=COVERAGE_YAML_PATH,
    )
    assert rc_err == 1 and isinstance(rc_err, int)


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

    # Bob attempting PATCH / DELETE on Alice's AAPL or Alice's group_id returns 404 and leaves Alice's data intact
    r_bob_patch_alice = client.patch("/api/v1/dalton/coverage/AAPL", json={"tier": "watch"})
    assert r_bob_patch_alice.status_code == 404

    r_bob_del_alice = client.delete("/api/v1/dalton/coverage/AAPL")
    assert r_bob_del_alice.status_code == 404

    r_bob_del_grp = client.delete(f"/api/v1/dalton/coverage/groups/{alice_group_id}")
    assert r_bob_del_grp.status_code == 404

    # Switch to Alice and verify her AAPL (core) and group are untouched
    current_user["id"] = USER_ALICE
    r_alice_cov = client.get("/api/v1/dalton/coverage")
    assert r_alice_cov.status_code == 200
    alice_tickers = {c["ticker"]: c["tier"] for c in r_alice_cov.json()["companies"]}
    assert alice_tickers == {"AAPL": "core", "NVDA": "watch"}
    assert len(r_alice_cov.json()["groups"]) == 1

