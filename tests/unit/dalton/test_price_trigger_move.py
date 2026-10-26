"""Unit tests for Dalton stock-move price trigger rules.

Uses a fake clock and ``unittest.mock.create_autospec`` test doubles.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

import pytest
from psycopg import AsyncConnection, AsyncCursor
from redis.asyncio import Redis

import src.data_client as data_client
import src.server.database.pool as db_pool
import src.server.services.price_monitor as price_monitor
import src.utils.cache.redis_cache as redis_cache
from src.dalton.autonomy import price_trigger
from src.dalton.autonomy.price_trigger import (
    classify_dalton_price_move,
    is_dalton_price_move_automation,
    process_polled_snapshot,
    record_snapshot_reference,
    refresh_dalton_reference_if_needed,
    symbol_has_dalton_move,
)
from src.dalton.autonomy.schedule import build_company_watch_specs
from src.data_client.market_data_provider import MarketDataProvider
from src.server.database import automation as auto_db
from src.server.models.automation import MarketType, PriceTriggerConfig
from src.server.services.automation_scheduler import AutomationScheduler
from src.server.services.price_monitor import PriceMonitorService
from src.utils.cache.redis_cache import RedisCacheClient

ET = ZoneInfo("America/New_York")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _make_dalton_move_automation(
    ticker: str = "NVDA",
    direction: str = "down",
    automation_id: str = "auto-nvda-down",
    user_id: str = "user-1",
    name_override: str | None = None,
) -> dict[str, Any]:
    specs = build_company_watch_specs(
        ticker=ticker,
        tier="Core",
        workspace_id=f"ws-{ticker.lower()}",
        timezone_name="America/New_York",
    )
    target_name = f"dalton-{ticker.lower()}-move-{direction}"
    spec = next(s for s in specs if s["name"] == target_name)
    row = dict(spec)
    if name_override is not None:
        row["name"] = name_override
    row["automation_id"] = automation_id
    row["user_id"] = user_id
    row["status"] = "active"
    row["_parsed_config"] = PriceTriggerConfig(**row["trigger_config"])
    return row


def _make_normal_price_automation(
    symbol: str = "NVDA",
    condition_type: str = "price_below",
    value: float = 96.0,
    automation_id: str = "auto-normal-1",
    user_id: str = "user-1",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trigger_config = {
        "symbol": symbol,
        "market": "stock",
        "conditions": [
            {
                "type": condition_type,
                "value": value,
                "reference": "previous_close",
            }
        ],
        "retrigger": {"mode": "recurring", "cooldown_seconds": 86400},
    }
    return {
        "automation_id": automation_id,
        "user_id": user_id,
        "name": f"user-{symbol.lower()}-alert",
        "trigger_type": "price",
        "trigger_config": trigger_config,
        "_parsed_config": PriceTriggerConfig(**trigger_config),
        "status": "active",
        "metadata": metadata or {},
    }


class _Harness:
    """Test harness wiring ``PriceMonitorService`` with ``create_autospec`` collaborators."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, initial_now: datetime) -> None:
        self._now = initial_now.astimezone(timezone.utc)
        self.redis_store: dict[str, tuple[str, float | None]] = {}
        self.redis_broken = False
        self.db_automations: dict[str, dict[str, Any]] = {}
        self.db_executions: list[dict[str, Any]] = []
        # Unique index store for dalton_price_move_firings:
        # (user_id, symbol, direction, trading_date) -> row dict
        self.db_move_firings: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self._table_lock = asyncio.Lock()

        self.clock = create_autospec(price_monitor._now_utc)
        self.clock.side_effect = lambda: self._now
        monkeypatch.setattr(price_monitor, "_now_utc", self.clock)

        self.provider = create_autospec(MarketDataProvider, instance=True)
        self.get_provider = create_autospec(
            data_client.get_market_data_provider,
            return_value=self.provider,
        )
        monkeypatch.setattr(
            data_client, "get_market_data_provider", self.get_provider
        )

        self.redis_client = create_autospec(Redis, instance=True)

        async def _redis_get(name: str) -> str | None:
            if self.redis_broken:
                raise ConnectionError("Redis unavailable")
            now_ts = self._now.timestamp()
            existing = self.redis_store.get(name)
            if existing is None:
                return None
            val, exp_ts = existing
            if exp_ts is not None and now_ts >= exp_ts:
                del self.redis_store[name]
                return None
            return val

        async def _redis_set(
            name: str,
            value: Any,
            ex: int | None = None,
            px: int | None = None,
            nx: bool = False,
            xx: bool = False,
            keepttl: bool = False,
            get: bool = False,
            exat: int | None = None,
            pxat: int | None = None,
        ) -> bool:
            if self.redis_broken:
                raise ConnectionError("Redis unavailable")
            now_ts = self._now.timestamp()
            existing = self.redis_store.get(name)
            if existing is not None:
                _, exp_ts = existing
                if exp_ts is not None and now_ts >= exp_ts:
                    del self.redis_store[name]
                    existing = None
            if nx and existing is not None:
                return False
            exp = (now_ts + float(ex)) if ex is not None else None
            self.redis_store[name] = (str(value), exp)
            return True

        async def _redis_delete(*names: str) -> int:
            if self.redis_broken:
                raise ConnectionError("Redis unavailable")
            removed = 0
            for n in names:
                if n in self.redis_store:
                    del self.redis_store[n]
                    removed += 1
            return removed

        self.redis_client.get.side_effect = _redis_get
        self.redis_client.set.side_effect = _redis_set
        self.redis_client.delete.side_effect = _redis_delete

        self.cache = create_autospec(RedisCacheClient, instance=True)
        self.cache.enabled = True
        self.cache.client = self.redis_client
        self.get_cache = create_autospec(
            redis_cache.get_cache_client,
            return_value=self.cache,
        )
        monkeypatch.setattr(redis_cache, "get_cache_client", self.get_cache)

        self.scheduler = create_autospec(AutomationScheduler, instance=True)
        self.scheduler.server_id = "worker-1"

        def _on_dispatch(
            automation: dict[str, Any],
            execution_id: str,
            *,
            name: str | None = None,
        ) -> None:
            aid = str(automation["automation_id"])
            if aid in self.db_automations:
                self.db_automations[aid]["status"] = "active"

        self.scheduler.dispatch.side_effect = _on_dispatch
        self.get_scheduler = create_autospec(
            AutomationScheduler.get_instance,
            return_value=self.scheduler,
        )
        monkeypatch.setattr(
            AutomationScheduler, "get_instance", self.get_scheduler
        )

        async def _claim_normal_firing(
            automation_id: str, server_id: str
        ) -> str | None:
            exec_id = f"exec-normal-{len(self.db_executions) + 1}"
            self.db_executions.append(
                {
                    "automation_execution_id": exec_id,
                    "automation_id": automation_id,
                    "status": "pending",
                    "server_id": server_id,
                    "created_at": self._now,
                }
            )
            return exec_id

        self.claim_firing = create_autospec(
            auto_db.claim_price_firing,
            side_effect=_claim_normal_firing,
        )
        monkeypatch.setattr(auto_db, "claim_price_firing", self.claim_firing)

        self.get_active_price_automations = create_autospec(
            auto_db.get_active_price_automations,
            return_value=[],
        )
        monkeypatch.setattr(
            auto_db,
            "get_active_price_automations",
            self.get_active_price_automations,
        )

        @asynccontextmanager
        async def _fake_get_db_connection() -> AsyncIterator[Any]:
            conn = create_autospec(AsyncConnection, instance=True)

            @asynccontextmanager
            async def _transaction() -> AsyncIterator[None]:
                async with self._table_lock:
                    await asyncio.sleep(0)
                    yield

            @asynccontextmanager
            async def _cursor() -> AsyncIterator[Any]:
                cur = create_autospec(AsyncCursor, instance=True)
                state: dict[str, Any] = {"fetchone": None, "rowcount": 0}

                async def _execute(sql: str, params: tuple[Any, ...] = ()) -> None:
                    if "INSERT INTO dalton_price_move_firings" in sql:
                        (
                            user_id,
                            sym_upper,
                            dir_lower,
                            trading_date_iso,
                            automation_id,
                        ) = params
                        uk = (
                            str(user_id),
                            str(sym_upper),
                            str(dir_lower),
                            str(trading_date_iso),
                        )
                        if uk in self.db_move_firings:
                            state["fetchone"] = None
                            state["rowcount"] = 0
                        else:
                            self.db_move_firings[uk] = {
                                "user_id": uk[0],
                                "symbol": uk[1],
                                "direction": uk[2],
                                "trading_date": uk[3],
                                "automation_id": str(automation_id),
                                "created_at": self._now,
                            }
                            state["fetchone"] = (str(automation_id),)
                            state["rowcount"] = 1
                        cur.rowcount = state["rowcount"]
                    elif "DELETE FROM dalton_price_move_firings" in sql:
                        user_id, sym_upper, dir_lower, trading_date_iso = params
                        uk = (
                            str(user_id),
                            str(sym_upper),
                            str(dir_lower),
                            str(trading_date_iso),
                        )
                        self.db_move_firings.pop(uk, None)
                    elif "UPDATE automations" in sql:
                        automation_id, user_id = params
                        auto = self.db_automations.get(str(automation_id))
                        if (
                            auto is not None
                            and str(auto.get("user_id") or "") == str(user_id)
                            and auto.get("status") == "active"
                        ):
                            auto["status"] = "executing"
                            state["rowcount"] = 1
                        else:
                            state["rowcount"] = 0
                        cur.rowcount = state["rowcount"]
                    elif "INSERT INTO automation_executions" in sql:
                        execution_id, automation_id, server_id = params
                        self.db_executions.append(
                            {
                                "automation_execution_id": str(execution_id),
                                "automation_id": str(automation_id),
                                "status": "pending",
                                "server_id": str(server_id),
                                "created_at": self._now,
                            }
                        )
                        state["rowcount"] = 1
                        cur.rowcount = 1

                async def _fetchone() -> Any:
                    return state["fetchone"]

                cur.execute.side_effect = _execute
                cur.fetchone.side_effect = _fetchone
                cur.rowcount = 0
                yield cur

            conn.transaction = _transaction
            conn.cursor = _cursor
            yield conn

        self.get_db_connection = create_autospec(
            db_pool.get_db_connection,
            side_effect=_fake_get_db_connection,
        )
        monkeypatch.setattr(
            db_pool, "get_db_connection", self.get_db_connection
        )

    def set_time(self, dt: datetime) -> None:
        self._now = dt.astimezone(timezone.utc)

    def set_snapshots(self, snapshots: list[dict[str, Any]]) -> None:
        self.provider.get_snapshots.return_value = snapshots

    def register_automation(self, automation: dict[str, Any]) -> None:
        self.db_automations[str(automation["automation_id"])] = dict(automation)

    def make_service(
        self, *automations: dict[str, Any]
    ) -> PriceMonitorService:
        svc = PriceMonitorService()
        # Upstream keys _symbol_automations by (symbol, market) listings and
        # the poll loop reads the merged _listings set _resubscribe maintains.
        sym_map: dict[tuple[str, MarketType], list[dict[str, Any]]] = {}
        for auto in automations:
            self.register_automation(auto)
            sym = str(auto["trigger_config"]["symbol"]).upper()
            market = MarketType(
                str(auto["trigger_config"].get("market", "stock")).lower()
            )
            sym_map.setdefault((sym, market), []).append(auto)
        svc._symbol_automations = sym_map
        svc._automation_listings = set(sym_map)
        svc._listings = set(sym_map)
        return svc


@pytest.mark.asyncio
async def test_friday_intraday_drop_triggers_once_and_does_not_retrigger_over_weekend_or_monday_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """周五盘中跌 4% 触发一次；当晚、周六、周日都不触发；周一开盘时用新的参考价重新计算，不会重复触发。"""
    fri_intraday = datetime(2026, 10, 23, 11, 0, tzinfo=ET)
    harness = _Harness(monkeypatch, fri_intraday)
    auto = _make_dalton_move_automation("NVDA", "down")
    svc = harness.make_service(auto)

    # 1. Friday 11:00 ET (regular session): drops -4.5% vs Thursday close (100.0 -> 95.5) -> triggers once
    harness.set_snapshots(
        [
            {
                "symbol": "NVDA",
                "price": 95.5,
                "previous_close": 100.0,
                "open": 99.0,
                "as_of": _ms(fri_intraday),
                "regular_as_of": _ms(fri_intraday),
                "regular_trading_date": "2026-10-23",
            }
        ]
    )
    await svc._poll_snapshots(poll_stock=True, poll_index=False)
    assert harness.scheduler.dispatch.call_count == 1
    assert "dalton:price_trigger:user-1:NVDA:down:2026-10-23" in harness.redis_store

    # 2. Friday night 20:00 ET (after-hours): snapshot still shows -4.5% regular close -> does not trigger
    fri_night = datetime(2026, 10, 23, 20, 0, tzinfo=ET)
    harness.set_time(fri_night)
    await svc._poll_snapshots(poll_stock=True, poll_index=False)
    assert harness.scheduler.dispatch.call_count == 1

    # 3. Saturday 14:00 ET (> 86400s after Friday 11:00 ET): weekend -> does not trigger
    saturday = datetime(2026, 10, 24, 14, 0, tzinfo=ET)
    harness.set_time(saturday)
    await svc._poll_snapshots(poll_stock=True, poll_index=False)
    assert harness.scheduler.dispatch.call_count == 1

    # 4. Sunday 14:00 ET: weekend -> does not trigger
    sunday = datetime(2026, 10, 25, 14, 0, tzinfo=ET)
    harness.set_time(sunday)
    await svc._poll_snapshots(poll_stock=True, poll_index=False)
    assert harness.scheduler.dispatch.call_count == 1

    # 5. Monday 09:35 ET (regular session): new reference price is Friday's close (95.5);
    # Monday price 95.0 is -0.52% vs 95.5 (even though -5.0% vs Thursday's 100.0) -> does not trigger
    mon_open = datetime(2026, 10, 26, 9, 35, tzinfo=ET)
    harness.set_time(mon_open)
    harness.set_snapshots(
        [
            {
                "symbol": "NVDA",
                "price": 95.0,
                "previous_close": 95.5,
                "open": 95.4,
                "as_of": _ms(mon_open),
                "regular_as_of": _ms(mon_open),
                "regular_trading_date": "2026-10-26",
            }
        ]
    )
    await svc._poll_snapshots(poll_stock=True, poll_index=False)
    assert harness.scheduler.dispatch.call_count == 1
    assert svc._dalton_move_refs[("NVDA", MarketType.STOCK)]["previous_close"] == 95.5
    assert svc._dalton_move_refs[("NVDA", MarketType.STOCK)]["trading_date"] == "2026-10-26"


@pytest.mark.asyncio
async def test_next_trading_day_drop_of_four_percent_relative_to_new_previous_close_triggers_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """周五触发后，下一交易日相对新前收再跌 ≥4% 会再触发一次。"""
    fri_intraday = datetime(2026, 10, 23, 11, 0, tzinfo=ET)
    harness = _Harness(monkeypatch, fri_intraday)
    auto = _make_dalton_move_automation("NVDA", "down")
    svc = harness.make_service(auto)

    # Friday 11:00 ET: drops -4.5% (100.0 -> 95.5) -> triggers once
    harness.set_snapshots(
        [
            {
                "symbol": "NVDA",
                "price": 95.5,
                "previous_close": 100.0,
                "open": 99.0,
                "as_of": _ms(fri_intraday),
                "regular_as_of": _ms(fri_intraday),
                "regular_trading_date": "2026-10-23",
            }
        ]
    )
    await svc._poll_snapshots(poll_stock=True, poll_index=False)
    assert harness.scheduler.dispatch.call_count == 1
    assert "dalton:price_trigger:user-1:NVDA:down:2026-10-23" in harness.redis_store

    # Monday 10:15 ET: new previous_close is 95.5, price drops to 91.0 (-4.71% vs 95.5) -> triggers again
    mon_intraday = datetime(2026, 10, 26, 10, 15, tzinfo=ET)
    harness.set_time(mon_intraday)
    harness.set_snapshots(
        [
            {
                "symbol": "NVDA",
                "price": 91.0,
                "previous_close": 95.5,
                "open": 95.0,
                "as_of": _ms(mon_intraday),
                "regular_as_of": _ms(mon_intraday),
                "regular_trading_date": "2026-10-26",
            }
        ]
    )
    await svc._poll_snapshots(poll_stock=True, poll_index=False)
    assert harness.scheduler.dispatch.call_count == 2
    assert "dalton:price_trigger:user-1:NVDA:down:2026-10-26" in harness.redis_store


@pytest.mark.asyncio
async def test_repeated_intraday_drops_below_four_percent_trigger_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一个交易日内反复跌破 4%，只触发一次（含多 worker 共享 Redis/DB）。"""
    t1 = datetime(2026, 10, 23, 10, 15, tzinfo=ET)
    harness = _Harness(monkeypatch, t1)
    auto = _make_dalton_move_automation("NVDA", "down")
    svc_worker1 = harness.make_service(auto)
    svc_worker2 = harness.make_service(auto)

    # First breach at 10:15 ET (-4.2%) -> triggers once
    harness.set_snapshots(
        [
            {
                "symbol": "NVDA",
                "price": 95.8,
                "previous_close": 100.0,
                "open": 99.5,
                "as_of": _ms(t1),
                "regular_as_of": _ms(t1),
                "regular_trading_date": "2026-10-23",
            }
        ]
    )
    await svc_worker1._poll_snapshots(poll_stock=True, poll_index=False)
    assert harness.scheduler.dispatch.call_count == 1

    # Rebound at 11:30 ET (-2.5%) -> no trigger
    t2 = datetime(2026, 10, 23, 11, 30, tzinfo=ET)
    harness.set_time(t2)
    harness.set_snapshots(
        [
            {
                "symbol": "NVDA",
                "price": 97.5,
                "previous_close": 100.0,
                "open": 99.5,
                "as_of": _ms(t2),
                "regular_as_of": _ms(t2),
                "regular_trading_date": "2026-10-23",
            }
        ]
    )
    await svc_worker1._poll_snapshots(poll_stock=True, poll_index=False)
    assert harness.scheduler.dispatch.call_count == 1

    # Second breach at 13:45 ET (-4.8%) on worker 1 -> still only 1 trigger
    t3 = datetime(2026, 10, 23, 13, 45, tzinfo=ET)
    harness.set_time(t3)
    harness.set_snapshots(
        [
            {
                "symbol": "NVDA",
                "price": 95.2,
                "previous_close": 100.0,
                "open": 99.5,
                "as_of": _ms(t3),
                "regular_as_of": _ms(t3),
                "regular_trading_date": "2026-10-23",
            }
        ]
    )
    await svc_worker1._poll_snapshots(poll_stock=True, poll_index=False)
    assert harness.scheduler.dispatch.call_count == 1

    # Third breach at 15:30 ET (-5.5%) evaluated on worker 2 -> blocked by shared marker
    t4 = datetime(2026, 10, 23, 15, 30, tzinfo=ET)
    harness.set_time(t4)
    harness.set_snapshots(
        [
            {
                "symbol": "NVDA",
                "price": 94.5,
                "previous_close": 100.0,
                "open": 99.5,
                "as_of": _ms(t4),
                "regular_as_of": _ms(t4),
                "regular_trading_date": "2026-10-23",
            }
        ]
    )
    await svc_worker2._poll_snapshots(poll_stock=True, poll_index=False)
    assert harness.scheduler.dispatch.call_count == 1


@pytest.mark.asyncio
async def test_after_hours_price_change_does_not_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """盘后的价格变化不触发。"""
    fri_regular = datetime(2026, 10, 23, 15, 55, tzinfo=ET)
    harness = _Harness(monkeypatch, fri_regular)
    auto = _make_dalton_move_automation("NVDA", "down")
    svc = harness.make_service(auto)

    # Regular session close is -1.5% (100.0 -> 98.5): no trigger
    harness.set_snapshots(
        [
            {
                "symbol": "NVDA",
                "price": 98.5,
                "previous_close": 100.0,
                "open": 99.5,
                "as_of": _ms(fri_regular),
                "regular_as_of": _ms(fri_regular),
                "regular_trading_date": "2026-10-23",
            }
        ]
    )
    await svc._poll_snapshots(poll_stock=True, poll_index=False)
    assert harness.scheduler.dispatch.call_count == 0

    # After-hours (16:30 ET): stock drops to 92.0 in extended trading (change_percent = -8.0%),
    # while regular close ``price`` is 98.5 -> must not poll or trigger on REST or WS path.
    fri_after_hours = datetime(2026, 10, 23, 16, 30, tzinfo=ET)
    harness.set_time(fri_after_hours)
    harness.set_snapshots(
        [
            {
                "symbol": "NVDA",
                "price": 98.5,
                "last_trade_price": 92.0,
                "previous_close": 100.0,
                "change_percent": -8.0,
                "regular_trading_change_percent": -1.5,
                "late_trading_change_percent": -6.599,
                "market_status": "late_trading",
                "extended_hours_mode": "post",
                "as_of": _ms(fri_after_hours),
                "regular_as_of": _ms(datetime(2026, 10, 23, 16, 0, tzinfo=ET)),
                "regular_trading_date": "2026-10-23",
            }
        ]
    )
    await svc._poll_snapshots(poll_stock=True, poll_index=False)
    await svc._on_message(
        '{"ev":"AM"}',
        {"symbol": "NVDA", "close": 92.0, "time": _ms(fri_after_hours)},
    )
    assert harness.scheduler.dispatch.call_count == 0


@pytest.mark.asyncio
async def test_stale_reference_price_and_failed_snapshot_validation_do_not_mark_today_valid_or_trigger_ws(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """参考价是旧的时候不触发；校验失败的快照不写入 _dalton_move_refs，WebSocket tick 不能用它判断。"""
    fri_close = datetime(2026, 10, 23, 15, 59, tzinfo=ET)
    mon_open = datetime(2026, 10, 26, 9, 31, tzinfo=ET)
    harness = _Harness(monkeypatch, mon_open)
    auto = _make_dalton_move_automation("NVDA", "down")
    svc = harness.make_service(auto)

    # Pre-seed _dalton_move_refs with Friday's validated reference (trading_date="2026-10-23")
    svc._dalton_move_refs[("NVDA", MarketType.STOCK)] = {
        "previous_close": 100.0,
        "day_open": 99.0,
        "trading_date": "2026-10-23",
    }

    # 1. Live WS tick arrives at Monday 09:31 ET before Monday's reference price is refreshed
    await svc._on_message(
        '{"ev":"AM"}',
        {"symbol": "NVDA", "close": 95.0, "time": _ms(mon_open)},
    )
    assert harness.scheduler.dispatch.call_count == 0

    # 2. Snapshot with reference_date=today ("2026-10-26") but regular_as_of=last_friday
    # fails extract_today_regular_snapshot -> must NOT write "2026-10-26" into _dalton_move_refs,
    # and clears Friday's stale entry so subsequent WS ticks cannot evaluate against it.
    bad_snapshot = {
        "symbol": "NVDA",
        "price": 95.0,
        "previous_close": 100.0,
        "open": 99.0,
        "reference_date": "2026-10-26",
        "regular_as_of": _ms(fri_close),
        "as_of": _ms(fri_close),
    }
    harness.set_snapshots([bad_snapshot])
    await svc._poll_snapshots(poll_stock=True, poll_index=False)
    assert svc._dalton_move_refs.get(("NVDA", MarketType.STOCK)) is None
    assert harness.scheduler.dispatch.call_count == 0

    await svc._evaluator.refresh_references([("NVDA", MarketType.STOCK)])
    assert svc._dalton_move_refs.get(("NVDA", MarketType.STOCK)) is None

    # Subsequent WS tick on Monday with -6% price cannot evaluate because _dalton_move_refs has no valid entry
    await svc._on_message(
        '{"ev":"AM"}',
        {"symbol": "NVDA", "close": 94.0, "time": _ms(mon_open)},
    )
    assert harness.scheduler.dispatch.call_count == 0

    # 3. Once a valid Monday snapshot is recorded in _dalton_move_refs, a later failed snapshot
    # keeps today's entry in _dalton_move_refs without touching evaluator._reference_prices.
    valid_monday_snapshot = {
        "symbol": "NVDA",
        "price": 99.0,
        "previous_close": 98.0,
        "open": 98.5,
        "reference_date": "2026-10-26",
        "regular_as_of": _ms(mon_open),
        "as_of": _ms(mon_open),
    }
    assert (
        record_snapshot_reference(
            svc, "NVDA", valid_monday_snapshot, MarketType.STOCK, mon_open
        )
        == "2026-10-26"
    )
    assert svc._dalton_move_refs[("NVDA", MarketType.STOCK)]["previous_close"] == 98.0
    assert svc._dalton_move_refs[("NVDA", MarketType.STOCK)]["trading_date"] == "2026-10-26"

    record_snapshot_reference(
        svc,
        "NVDA",
        {**bad_snapshot, "previous_close": 200.0},
        MarketType.STOCK,
        mon_open,
    )
    assert svc._dalton_move_refs[("NVDA", MarketType.STOCK)]["previous_close"] == 98.0
    assert svc._dalton_move_refs[("NVDA", MarketType.STOCK)]["trading_date"] == "2026-10-26"


@pytest.mark.asyncio
async def test_isolated_dalton_refs_mixed_symbol_interleaved_ws_tick_and_deleted_move_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """混合标的普通提醒在盘后/周末与基线一致；普通任务领取等待期间插入 WebSocket tick 不会让 Dalton 用未验证参考价触发；删除异动任务后不再被 Dalton 接管。"""
    fri_after_hours = datetime(2026, 10, 23, 18, 0, tzinfo=ET)
    harness = _Harness(monkeypatch, fri_after_hours)

    # 1. Mixed symbol in after-hours and weekend: normal pct_change_below alert uses evaluator._reference_prices
    # and triggers identically to baseline e15cca35, while Dalton move task does not touch evaluator._reference_prices
    dalton_nvda = _make_dalton_move_automation(
        "NVDA", "down", automation_id="auto-nvda-dalton"
    )
    normal_nvda = _make_normal_price_automation(
        symbol="NVDA",
        condition_type="pct_change_below",
        value=2.0,
        automation_id="auto-nvda-normal",
    )
    svc_mixed = harness.make_service(dalton_nvda, normal_nvda)
    assert not hasattr(svc_mixed._evaluator, "_reference_trading_dates")

    harness.set_snapshots(
        [
            {
                "symbol": "NVDA",
                "price": 94.0,
                "previous_close": 100.0,
                "open": 99.0,
                "market_status": "late_trading",
                "extended_hours_mode": "post",
                "as_of": _ms(fri_after_hours),
                "regular_as_of": _ms(datetime(2026, 10, 23, 16, 0, tzinfo=ET)),
                "regular_trading_date": "2026-10-23",
            }
        ]
    )
    await svc_mixed._poll_snapshots(poll_stock=True, poll_index=False)
    assert svc_mixed._evaluator._reference_prices[("NVDA", MarketType.STOCK)] == {
        "previous_close": 100.0,
        "day_open": 99.0,
    }
    assert svc_mixed._dalton_move_refs == {}
    assert [
        c.args[0]["automation_id"]
        for c in harness.scheduler.dispatch.call_args_list
    ] == ["auto-nvda-normal"]

    # Weekend: Saturday and Sunday polls on the mixed symbol behave identically to baseline e15cca35
    for weekend_dt in (
        datetime(2026, 10, 24, 14, 0, tzinfo=ET),  # Saturday
        datetime(2026, 10, 25, 14, 0, tzinfo=ET),  # Sunday
    ):
        harness.set_time(weekend_dt)
        svc_mixed._local_locks.clear()
        harness.redis_store.clear()
        harness.set_snapshots(
            [
                {
                    "symbol": "NVDA",
                    "price": 94.0,
                    "previous_close": 100.0,
                    "open": 99.0,
                    "market_status": "closed",
                    "as_of": _ms(weekend_dt),
                    "regular_as_of": _ms(datetime(2026, 10, 23, 16, 0, tzinfo=ET)),
                    "regular_trading_date": "2026-10-23",
                }
            ]
        )
        await svc_mixed._poll_snapshots(poll_stock=True, poll_index=False)

    assert [
        c.args[0]["automation_id"]
        for c in harness.scheduler.dispatch.call_args_list
    ] == ["auto-nvda-normal", "auto-nvda-normal", "auto-nvda-normal"]
    assert svc_mixed._dalton_move_refs == {}

    # String values on mixed symbol follow upstream baseline _positive (rejecting non-Real strings):
    # - price="94" (string) is skipped without triggering normal alert;
    # - previous_close="100" (string) does not call set_reference to overwrite the valid 100.0 reference with 0.0.
    svc_mixed._local_locks.clear()
    harness.redis_store.clear()
    harness.set_snapshots(
        [
            {
                "symbol": "NVDA",
                "price": "94",
                "previous_close": 100.0,
                "open": 99.0,
                "as_of": _ms(fri_after_hours),
            }
        ]
    )
    await svc_mixed._poll_snapshots(poll_stock=True, poll_index=False)
    assert len(harness.scheduler.dispatch.call_args_list) == 3

    harness.set_snapshots(
        [
            {
                "symbol": "NVDA",
                "price": 95.0,
                "previous_close": "100",
                "open": 99.0,
                "as_of": _ms(fri_after_hours),
            }
        ]
    )
    await svc_mixed._poll_snapshots(poll_stock=True, poll_index=False)
    assert svc_mixed._evaluator._reference_prices[("NVDA", MarketType.STOCK)]["previous_close"] == 100.0
    assert len(harness.scheduler.dispatch.call_args_list) == 4
    assert (
        harness.scheduler.dispatch.call_args_list[-1].args[0]["automation_id"]
        == "auto-nvda-normal"
    )

    # 2. On Monday regular hours (09:31 ET), an unvalidated snapshot arrives (regular_as_of=Friday).
    # While the normal task is awaiting claim_price_firing (after evaluator._reference_prices["NVDA"]
    # has been set to 100.0), an interleaved WebSocket tick (close=94.0, -6% vs 100.0) arrives.
    # Dalton must NOT trigger using evaluator._reference_prices!
    mon_open = datetime(2026, 10, 26, 9, 31, tzinfo=ET)
    harness.set_time(mon_open)
    svc_mixed._local_locks.clear()
    harness.redis_store.clear()

    fri_close = datetime(2026, 10, 23, 15, 59, tzinfo=ET)
    unvalidated_monday_snap = {
        "symbol": "NVDA",
        "price": 94.0,
        "previous_close": 100.0,
        "open": 99.0,
        "reference_date": "2026-10-26",
        "regular_as_of": _ms(fri_close),
        "as_of": _ms(fri_close),
    }
    harness.set_snapshots([unvalidated_monday_snap])

    async def _claim_with_interleaved_ws_tick(
        automation_id: str, server_id: str
    ) -> str | None:
        # At this exact point, evaluator._reference_prices["NVDA"]["previous_close"] is 100.0
        assert svc_mixed._evaluator._reference_prices[("NVDA", MarketType.STOCK)]["previous_close"] == 100.0
        await svc_mixed._on_message(
            '{"ev":"AM"}',
            {"symbol": "NVDA", "close": 94.0, "time": _ms(mon_open)},
        )
        return "exec-normal-interleaved"

    harness.claim_firing.side_effect = _claim_with_interleaved_ws_tick
    await svc_mixed._poll_snapshots(poll_stock=True, poll_index=False)

    dispatched_ids = [
        c.args[0]["automation_id"]
        for c in harness.scheduler.dispatch.call_args_list
    ]
    assert dispatched_ids == ["auto-nvda-normal"] * 5
    assert "auto-nvda-dalton" not in dispatched_ids

    # 3. After deleting the Dalton move task from _symbol_automations, the symbol is handed back to upstream as-is
    svc_mixed._symbol_automations[("NVDA", MarketType.STOCK)] = [normal_nvda]
    remaining_normal = await process_polled_snapshot(
        svc_mixed, "NVDA", MarketType.STOCK, unvalidated_monday_snap, mon_open
    )
    assert remaining_normal == [normal_nvda]
    assert ("NVDA", MarketType.STOCK) not in svc_mixed._dalton_move_refs


@pytest.mark.asyncio
async def test_dedup_table_concurrent_redis_down_redis_recovery_and_file_name_only_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """测试：Redis 不可用时两个并发调用只触发一次；Redis 恢复后同日再来一次不触发；仅靠 file_name 识别的任务也能正确去重。"""
    fri_intraday = datetime(2026, 10, 23, 11, 0, tzinfo=ET)
    harness = _Harness(monkeypatch, fri_intraday)

    snap = [
        {
            "symbol": "NVDA",
            "price": 95.0,
            "previous_close": 100.0,
            "open": 99.0,
            "as_of": _ms(fri_intraday),
            "regular_as_of": _ms(fri_intraday),
            "regular_trading_date": "2026-10-23",
        }
    ]
    harness.set_snapshots(snap)

    # 1. Redis is unavailable and two concurrent calls run -> unique constraint on dalton_price_move_firings triggers only once
    harness.redis_broken = True
    auto_1 = _make_dalton_move_automation(
        "NVDA", "down", automation_id="auto-nvda-1", user_id="user-1"
    )
    svc_w1 = harness.make_service(auto_1)
    svc_w2 = harness.make_service(auto_1)
    await asyncio.gather(
        svc_w1._poll_snapshots(poll_stock=True, poll_index=False),
        svc_w2._poll_snapshots(poll_stock=True, poll_index=False),
    )
    assert harness.scheduler.dispatch.call_count == 1
    assert len(harness.db_move_firings) == 1
    assert ("user-1", "NVDA", "down", "2026-10-23") in harness.db_move_firings
    assert harness.redis_store == {}

    # 2. Redis recovers and another poll arrives on the same day -> ON CONFLICT DO NOTHING skips, backfills Redis
    harness.redis_broken = False
    await svc_w1._poll_snapshots(poll_stock=True, poll_index=False)
    assert harness.scheduler.dispatch.call_count == 1
    assert "dalton:price_trigger:user-1:NVDA:down:2026-10-23" in harness.redis_store

    # 3. Tasks identified ONLY by file_name (no metadata.dalton_kind and empty name)
    # use the same classify_dalton_price_move function and deduplicate via dalton_price_move_firings
    file_only_a = _make_dalton_move_automation(
        "TSLA", "down", automation_id="auto-tsla-file-a", user_id="user-2"
    )
    file_only_a["name"] = ""
    file_only_a["metadata"] = {}
    file_only_a["file_name"] = "dalton-tsla-move-down.json"

    file_only_b = _make_dalton_move_automation(
        "TSLA", "down", automation_id="auto-tsla-file-b", user_id="user-2"
    )
    file_only_b["name"] = ""
    file_only_b["metadata"] = {}
    file_only_b["file_name"] = "dalton-tsla-alt-move-down.json"

    assert is_dalton_price_move_automation(file_only_a) is True
    assert classify_dalton_price_move(file_only_a) == "down"
    assert classify_dalton_price_move(file_only_b) == "down"

    svc_tsla = harness.make_service(file_only_a, file_only_b)
    harness.set_snapshots(
        [
            {
                "symbol": "TSLA",
                "price": 190.0,
                "previous_close": 200.0,
                "open": 198.0,
                "as_of": _ms(fri_intraday),
                "regular_as_of": _ms(fri_intraday),
                "regular_trading_date": "2026-10-23",
            }
        ]
    )
    harness.redis_broken = True
    await svc_tsla._poll_snapshots(poll_stock=True, poll_index=False)
    tsla_dispatched = [
        call.args[0]["automation_id"]
        for call in harness.scheduler.dispatch.call_args_list[1:]
    ]
    assert tsla_dispatched == ["auto-tsla-file-a"]
    assert ("user-2", "TSLA", "down", "2026-10-23") in harness.db_move_firings


@pytest.mark.asyncio
async def test_load_automations_populates_symbol_automations_before_refresh_so_ws_tick_triggers_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """走真实 _load_automations 顺序：先给 _symbol_automations 赋值再调用 refresh_references，加载后立即有有效的 Dalton 参考价，WebSocket tick 能触发。"""
    fri_intraday = datetime(2026, 10, 23, 10, 5, tzinfo=ET)
    harness = _Harness(monkeypatch, fri_intraday)

    auto = _make_dalton_move_automation(
        "NVDA", "down", automation_id="auto-nvda-load", user_id="user-1"
    )
    harness.register_automation(auto)
    harness.get_active_price_automations.return_value = [auto]
    harness.set_snapshots(
        [
            {
                "symbol": "NVDA",
                "price": 99.0,
                "previous_close": 100.0,
                "open": 99.5,
                "as_of": _ms(fri_intraday),
                "regular_as_of": _ms(fri_intraday),
                "regular_trading_date": "2026-10-23",
            }
        ]
    )

    svc = PriceMonitorService()
    await svc._load_automations()

    assert svc._dalton_move_refs.get(("NVDA", MarketType.STOCK)) == {
        "previous_close": 100.0,
        "day_open": 99.5,
        "trading_date": "2026-10-23",
    }

    # Immediate WebSocket tick (-4.5% vs 100.0) triggers right away without waiting for a poll pass
    await svc._on_message(
        '{"ev":"AM"}',
        {"symbol": "NVDA", "close": 95.5, "time": _ms(fri_intraday)},
    )
    assert harness.scheduler.dispatch.call_count == 1
    assert (
        harness.scheduler.dispatch.call_args.args[0]["automation_id"]
        == "auto-nvda-load"
    )


@pytest.mark.asyncio
async def test_same_named_stock_and_index_references_are_keyed_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同名股票/指数同时被监控时，Dalton 参考价按 (symbol, market) listing 键互不影响：
    一侧的刷新不会覆盖另一侧，一侧无 Dalton 自动化时也只清理自己的键。"""
    fri_intraday = datetime(2026, 10, 23, 11, 0, tzinfo=ET)
    harness = _Harness(monkeypatch, fri_intraday)
    now = harness._now

    dalton_hsi_stock = _make_dalton_move_automation("HSI", "down")
    normal_hsi_index = _make_normal_price_automation(
        symbol="HSI", condition_type="pct_change_below", value=2.0
    )
    normal_hsi_index["trigger_config"]["market"] = "index"
    normal_hsi_index["_parsed_config"] = PriceTriggerConfig(
        **normal_hsi_index["trigger_config"]
    )
    svc = harness.make_service(dalton_hsi_stock, normal_hsi_index)

    # Session validation is stubbed per market: only snapshots of the market
    # named in `valid_for` pass, so the test does not depend on two venues
    # sharing regular hours.
    extract_stub = create_autospec(price_trigger.extract_today_regular_snapshot)
    extract_stub.side_effect = lambda sym, market, snap, at: (
        ("2026-10-23", snap["price"], snap["previous_close"], snap["open"])
        if market == snap["valid_for"]
        else None
    )
    monkeypatch.setattr(price_trigger, "extract_today_regular_snapshot", extract_stub)

    stock_snap = {"symbol": "HSI", "price": 96.0, "previous_close": 100.0, "open": 99.0, "valid_for": MarketType.STOCK}
    index_snap = {"symbol": "HSI", "price": 19600.0, "previous_close": 20000.0, "open": 19900.0, "valid_for": MarketType.INDEX}

    # The listing identity decides everything: only the stock listing carries
    # a Dalton automation, so only it may hold a reference.
    assert symbol_has_dalton_move(svc, "HSI", MarketType.STOCK) is True
    assert symbol_has_dalton_move(svc, "HSI", MarketType.INDEX) is False

    assert (
        refresh_dalton_reference_if_needed(svc, "HSI", stock_snap, MarketType.STOCK, now)
        == "2026-10-23"
    )
    # Pre-fix this overwrote the shared bare-symbol entry with 20000.0; now the
    # index listing has no Dalton automation, so nothing is written at all.
    assert (
        refresh_dalton_reference_if_needed(svc, "HSI", index_snap, MarketType.INDEX, now)
        is None
    )
    assert svc._dalton_move_refs[("HSI", MarketType.STOCK)]["previous_close"] == 100.0
    assert ("HSI", MarketType.INDEX) not in svc._dalton_move_refs

    # A poll on the index listing finds no Dalton automations and pops only the
    # index listing's key; the stock reference survives untouched.
    stale_index_snap = dict(index_snap, valid_for=None, price=0.0)
    remaining = await process_polled_snapshot(
        svc, "HSI", MarketType.INDEX, stale_index_snap, now
    )
    assert [a["automation_id"] for a in remaining] == ["auto-normal-1"]
    assert svc._dalton_move_refs[("HSI", MarketType.STOCK)]["previous_close"] == 100.0

    # Even when both listings carry a Dalton automation (a shape Dalton itself
    # never creates — its stock-move tasks are always market=stock), the two
    # references coexist instead of clobbering each other.
    svc._symbol_automations[("HSI", MarketType.INDEX)] = [dalton_hsi_stock]
    assert (
        refresh_dalton_reference_if_needed(svc, "HSI", index_snap, MarketType.INDEX, now)
        == "2026-10-23"
    )
    assert svc._dalton_move_refs[("HSI", MarketType.STOCK)]["previous_close"] == 100.0
    assert svc._dalton_move_refs[("HSI", MarketType.INDEX)]["previous_close"] == 20000.0

    # Reading for evaluation is per listing too: each market's config sees its
    # own reference, never the other listing's.
    def _config(market: MarketType) -> PriceTriggerConfig:
        return PriceTriggerConfig(
            symbol="HSI",
            market=market,
            conditions=[{"type": "pct_change_below", "value": 2.0, "reference": "previous_close"}],
        )

    refs_ok_stub = create_autospec(price_trigger.trading_session_date, return_value="2026-10-23")
    monkeypatch.setattr(price_trigger, "trading_session_date", refs_ok_stub)
    assert price_trigger.can_evaluate_automation(svc, dalton_hsi_stock, _config(MarketType.STOCK), now) is True
    assert price_trigger.can_evaluate_automation(svc, dalton_hsi_stock, _config(MarketType.INDEX), now) is True
    svc._dalton_move_refs.pop(("HSI", MarketType.INDEX), None)
    assert price_trigger.can_evaluate_automation(svc, dalton_hsi_stock, _config(MarketType.STOCK), now) is True
    assert price_trigger.can_evaluate_automation(svc, dalton_hsi_stock, _config(MarketType.INDEX), now) is False


@pytest.mark.asyncio
async def test_pure_dalton_listing_poll_still_evaluates_market_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一个 listing 上只有 Dalton 异动任务时（process_polled_snapshot 返回 None），
    轮询仍要评估同 listing 上的上游 market-watch 目标价，只跳过普通自动化段。"""
    fri_intraday = datetime(2026, 10, 23, 11, 0, tzinfo=ET)
    harness = _Harness(monkeypatch, fri_intraday)
    auto = _make_dalton_move_automation("NVDA", "down")
    svc = harness.make_service(auto)

    harness.set_snapshots(
        [
            {
                "symbol": "NVDA",
                "price": 95.5,
                "previous_close": 100.0,
                "open": 99.0,
                "as_of": _ms(fri_intraday),
                "regular_as_of": _ms(fri_intraday),
                "regular_trading_date": "2026-10-23",
            }
        ]
    )

    targets_spy = create_autospec(svc._evaluate_targets)
    monkeypatch.setattr(svc, "_evaluate_targets", targets_spy)

    await svc._poll_snapshots(poll_stock=True, poll_index=False)

    # The Dalton listing is fully handled inside process_polled_snapshot, but a
    # market-watch target on the same listing must still be evaluated.
    assert targets_spy.await_count == 1
    assert targets_spy.await_args.args[0] == ("NVDA", MarketType.STOCK)
    assert targets_spy.await_args.args[1] == 95.5
    assert targets_spy.await_args.args[2] == _ms(fri_intraday)
