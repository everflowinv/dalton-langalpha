"""Dalton trading-day price trigger guard.

Enforces regular-session-only evaluation, an isolated validated ``previous_close``
reference store (``service._dalton_move_refs``), and at-most-once-per-trading-day
triggering per ``(user_id, symbol, direction, trading_date)`` backed by the
``dalton_price_move_firings`` unique table in a single Postgres transaction with
a Redis fast pre-check. Only applies to Dalton stock-move automations; all other
price automations keep their baseline behavior untouched.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple
from uuid import uuid4

from market_protocol import AssetClass, InstrumentRef, MarketPhase, to_canonical
from src.data_client.instrument_clock import clock_for_ref
from src.server.models.automation import (
    MarketType,
    PriceConditionType,
    PriceTriggerConfig,
)

logger = logging.getLogger(__name__)

_DALTON_MOVE_NAME_RE = re.compile(
    r"^dalton-[a-z0-9._-]+-move-(up|down)(?:\.json)?$",
    re.IGNORECASE,
)

_EXTENDED_OR_CLOSED_STATUSES = frozenset(
    {
        "early_trading",
        "late_trading",
        "closed",
        "pre",
        "post",
        "pre_market",
        "after_hours",
    }
)

_EXTENDED_MODES = frozenset({"pre", "post"})

_MIN_LOCK_TTL_SECONDS = 300
_DEFAULT_LOCK_TTL_SECONDS = 86400


def _positive(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def listing_ref(
    bare_symbol: str, watched_market: Optional[MarketType] = None
) -> InstrumentRef:
    """Resolve ``bare_symbol`` into the ``InstrumentRef`` of its trading venue."""
    return to_canonical(
        bare_symbol.upper(),
        asset_class=(
            AssetClass.INDEX
            if watched_market == MarketType.INDEX
            else AssetClass.EQUITY
        ),
    )


def classify_dalton_price_move(
    automation: Mapping[str, Any],
    config: Optional[PriceTriggerConfig] = None,
) -> Optional[str]:
    """Identify a Dalton stock-move automation and return its direction
    (``'up'`` or ``'down'``), or ``None`` if ``automation`` is not a Dalton
    stock-move price trigger.

    Both task identification and direction resolution go through this single
    function so Python and database deduplication always agree.
    """
    meta = automation.get("metadata")
    is_kind_move = (
        isinstance(meta, Mapping) and meta.get("dalton_kind") == "price_move"
    )

    name = str(automation.get("name") or "").strip()
    name_match = _DALTON_MOVE_NAME_RE.match(name)

    file_name = str(automation.get("file_name") or "").strip()
    file_match = _DALTON_MOVE_NAME_RE.match(file_name)

    if not is_kind_move and name_match is None and file_match is None:
        return None

    if isinstance(meta, Mapping):
        raw_dir = str(meta.get("direction") or "").strip().lower()
        if raw_dir in ("up", "down"):
            return raw_dir

    if name_match is not None:
        return name_match.group(1).lower()
    if file_match is not None:
        return file_match.group(1).lower()

    for label in (name.lower(), file_name.lower()):
        if label.endswith("-move-up") or label.endswith("-move-up.json"):
            return "up"
        if label.endswith("-move-down") or label.endswith("-move-down.json"):
            return "down"

    if config is None:
        parsed = automation.get("_parsed_config")
        if isinstance(parsed, PriceTriggerConfig):
            config = parsed
        else:
            raw_cfg = automation.get("trigger_config")
            if isinstance(raw_cfg, Mapping):
                try:
                    config = PriceTriggerConfig(**raw_cfg)
                except Exception:
                    config = None

    if config is not None:
        for cond in config.conditions:
            if cond.type in (
                PriceConditionType.PCT_CHANGE_ABOVE,
                PriceConditionType.PRICE_ABOVE,
            ):
                return "up"
            if cond.type in (
                PriceConditionType.PCT_CHANGE_BELOW,
                PriceConditionType.PRICE_BELOW,
            ):
                return "down"
    return "move"


def is_dalton_price_move_automation(automation: Mapping[str, Any]) -> bool:
    """Return True iff ``automation`` is a Dalton stock-move price trigger."""
    return classify_dalton_price_move(automation) is not None


def resolve_move_direction(
    automation: Mapping[str, Any],
    config: Optional[PriceTriggerConfig] = None,
) -> str:
    """Resolve trigger direction (``'up'`` or ``'down'``) using the same
    classifier that identifies Dalton stock-move automations.
    """
    return classify_dalton_price_move(automation, config) or "move"


def trading_session_date(
    bare_symbol: str,
    watched_market: Optional[MarketType],
    now: datetime,
) -> Optional[str]:
    """Return ``YYYY-MM-DD`` in the venue's timezone when ``now`` is in regular
    trading hours on a trading day, else ``None``.
    """
    try:
        clock = clock_for_ref(listing_ref(bare_symbol, watched_market))
        if clock.phase(now) is not MarketPhase.REGULAR:
            return None
        day = now.astimezone(clock.tz).date()
        if not clock.is_trading_day(day):
            return None
        return day.isoformat()
    except Exception:
        logger.warning(
            "[DaltonPriceTrigger] Could not resolve trading session for %s",
            bare_symbol,
            exc_info=True,
        )
        return None


def _timestamp_to_venue_date_iso(
    raw_ts: float,
    bare_symbol: str,
    watched_market: Optional[MarketType],
) -> Optional[str]:
    """Convert epoch ms (or seconds) to ``YYYY-MM-DD`` in the venue's timezone."""
    try:
        clock = clock_for_ref(listing_ref(bare_symbol, watched_market))
        ts_sec = raw_ts / 1000.0 if raw_ts > 1e11 else float(raw_ts)
        dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc).astimezone(clock.tz)
        return dt.date().isoformat()
    except Exception:
        return None


def extract_today_regular_snapshot(
    bare_symbol: str,
    watched_market: Optional[MarketType],
    snapshot: Mapping[str, Any],
    now: datetime,
) -> Optional[Tuple[str, float, float, float]]:
    """Validate that ``now`` is in regular trading hours and ``snapshot`` holds
    today's regular-session price and today's refreshed ``previous_close``.

    Returns ``(trading_date_iso, regular_price, previous_close, day_open)`` or
    ``None`` when outside regular session, during extended hours, or when the
    quote/reference price has not rolled to today's trading day.
    """
    today_iso = trading_session_date(bare_symbol, watched_market, now)
    if today_iso is None:
        return None

    market_status = str(snapshot.get("market_status") or "").strip().lower()
    if market_status in _EXTENDED_OR_CLOSED_STATUSES:
        return None

    ext_mode = str(snapshot.get("extended_hours_mode") or "").strip().lower()
    if ext_mode in _EXTENDED_MODES:
        return None

    regular_price = _positive(snapshot.get("price"))
    prev_close = _positive(snapshot.get("previous_close"))
    if regular_price is None or prev_close is None:
        return None
    day_open = _positive(snapshot.get("open")) or 0.0

    explicit_date_matched = False
    for date_key in (
        "reference_date",
        "previous_close_date",
        "regular_trading_date",
        "trading_date",
        "session_date",
    ):
        raw_date = snapshot.get(date_key)
        if raw_date is not None and str(raw_date).strip():
            if str(raw_date).strip()[:10] != today_iso:
                return None
            explicit_date_matched = True

    reg_ts = _positive(snapshot.get("regular_as_of"))
    if reg_ts is None:
        reg_ts = _positive(snapshot.get("as_of"))

    if reg_ts is not None:
        print_date_iso = _timestamp_to_venue_date_iso(
            reg_ts, bare_symbol, watched_market
        )
        if print_date_iso != today_iso:
            return None
    elif not explicit_date_matched:
        return None

    return today_iso, regular_price, prev_close, day_open


def should_poll_us_symbol(
    bare_symbol: str,
    watched_market: Optional[MarketType],
    automations: Iterable[Mapping[str, Any]],
    now: datetime,
) -> bool:
    """Skip US fallback polling outside regular hours only when every automation
    on ``bare_symbol`` is a Dalton price-move automation.
    """
    auto_list = list(automations)
    if auto_list and all(is_dalton_price_move_automation(a) for a in auto_list):
        return trading_session_date(bare_symbol, watched_market, now) is not None
    return True


def _resolve_service(target: Any) -> Any:
    """Resolve ``PriceMonitorService`` from either the service or its evaluator."""
    return getattr(target, "_service", None) or target


def get_dalton_move_refs(target: Any) -> Dict[str, Dict[str, Any]]:
    """Return the isolated Dalton validated reference store
    ``service._dalton_move_refs`` (``symbol -> {previous_close, day_open, trading_date}``).
    """
    service = _resolve_service(target)
    refs = getattr(service, "_dalton_move_refs", None)
    if refs is None:
        refs = {}
        setattr(service, "_dalton_move_refs", refs)
    return refs


def symbol_has_dalton_move(target: Any, bare_symbol: str) -> bool:
    """Return True iff ``bare_symbol`` currently has a Dalton price-move
    automation in ``_symbol_automations``.

    Upstream keys the map by ``(symbol, market)`` listings, and the same
    bare symbol can be watched in two markets at once, so this checks every
    listing the symbol names.
    """
    service = _resolve_service(target)
    sym = bare_symbol.upper()
    for (listing_sym, _market), autos in getattr(
        service, "_symbol_automations", {}
    ).items():
        if listing_sym == sym and any(is_dalton_price_move_automation(a) for a in autos):
            return True
    return False


def record_snapshot_reference(
    target: Any,
    bare_symbol: str,
    snapshot: Mapping[str, Any],
    watched_market: Optional[MarketType],
    now: datetime,
) -> Optional[str]:
    """Write to the isolated ``_dalton_move_refs`` store ONLY when
    ``extract_today_regular_snapshot`` succeeds. Never reads or mutates
    ``evaluator._reference_prices``.
    """
    sym = bare_symbol.upper()
    refs = get_dalton_move_refs(target)
    validated = extract_today_regular_snapshot(sym, watched_market, snapshot, now)
    if validated is not None:
        today_iso, _, prev_close, day_open = validated
        refs[sym] = {
            "previous_close": prev_close,
            "day_open": day_open,
            "trading_date": today_iso,
        }
        return today_iso

    today_iso = trading_session_date(sym, watched_market, now)
    existing = refs.get(sym)
    if existing is not None and existing.get("trading_date") == today_iso and today_iso is not None:
        return today_iso

    refs.pop(sym, None)
    return None


def refresh_dalton_reference_if_needed(
    target: Any,
    bare_symbol: str,
    snapshot: Mapping[str, Any],
    watched_market: Optional[MarketType],
    now: datetime,
) -> Optional[str]:
    """Update ``_dalton_move_refs`` only when ``bare_symbol`` currently has at
    least one Dalton price-move automation in ``_symbol_automations``.
    """
    if not symbol_has_dalton_move(target, bare_symbol):
        get_dalton_move_refs(target).pop(bare_symbol.upper(), None)
        return None
    return record_snapshot_reference(
        target, bare_symbol, snapshot, watched_market, now
    )


def can_evaluate_automation(
    target: Any,
    automation: Mapping[str, Any],
    config: PriceTriggerConfig,
    now: datetime,
) -> bool:
    """Return True if ``automation`` may be evaluated at ``now``.

    Non-Dalton automations always return ``True``. Dalton move automations
    check only the isolated ``_dalton_move_refs`` store.
    """
    if not is_dalton_price_move_automation(automation):
        return True

    symbol = config.symbol.upper()
    today_iso = trading_session_date(symbol, config.market, now)
    if today_iso is None:
        return False

    ref_entry = get_dalton_move_refs(target).get(symbol)
    if not isinstance(ref_entry, Mapping):
        return False
    if ref_entry.get("trading_date") != today_iso:
        return False
    if (_positive(ref_entry.get("previous_close")) or 0.0) <= 0:
        return False
    return True


def evaluate_dalton_move_conditions(
    target: Any,
    automation: Mapping[str, Any],
    config: PriceTriggerConfig,
    current_price: float,
    now: datetime,
) -> bool:
    """Evaluate all conditions for a Dalton price-move automation reading ONLY
    from the isolated ``_dalton_move_refs`` store.
    """
    if not can_evaluate_automation(target, automation, config, now):
        return False

    symbol = config.symbol.upper()
    ref_entry = get_dalton_move_refs(target).get(symbol) or {}

    for condition in config.conditions:
        if condition.type == PriceConditionType.PRICE_ABOVE:
            if not (current_price > condition.value):
                return False
        elif condition.type == PriceConditionType.PRICE_BELOW:
            if not (current_price < condition.value):
                return False
        elif condition.type in (
            PriceConditionType.PCT_CHANGE_ABOVE,
            PriceConditionType.PCT_CHANGE_BELOW,
        ):
            ref_price = _positive(ref_entry.get(condition.reference)) or 0.0
            if ref_price <= 0:
                return False
            pct_change = ((current_price - ref_price) / ref_price) * 100.0
            if condition.type == PriceConditionType.PCT_CHANGE_ABOVE:
                if not (pct_change > condition.value):
                    return False
            else:
                if not (pct_change < -condition.value):
                    return False
        else:
            return False
    return True


async def evaluate_and_trigger_dalton_move(
    service: Any,
    automation: Dict[str, Any],
    config: PriceTriggerConfig,
    current_price: float,
    now: datetime,
) -> bool:
    """Evaluate a Dalton move automation against ``service._dalton_move_refs``
    and trigger at most once per trading day if all conditions hold.
    """
    if not evaluate_dalton_move_conditions(
        service, automation, config, current_price, now
    ):
        return False
    return await try_trigger_dalton_price_move(
        service, automation, config, current_price, now
    )


async def process_polled_snapshot(
    service: Any,
    bare_symbol: str,
    watched_market: Optional[MarketType],
    snapshot: Mapping[str, Any],
    now: datetime,
) -> Optional[list[Dict[str, Any]]]:
    """Process only Dalton price-move automations on ``bare_symbol`` and return
    the remaining non-Dalton automations for the upstream loop to handle.

    ``watched_market`` is the market of the listing the poll fetched the
    snapshot under (upstream keys ``_symbol_automations`` by
    ``(symbol, market)``).

    Returns ``list(automations)`` immediately when ``bare_symbol`` has no Dalton
    price-move automations. When ``bare_symbol`` has Dalton price-move
    automations, evaluates only ``dalton_autos`` against ``service._dalton_move_refs``
    and returns ``normal_autos`` if any exist, or ``None`` when all automations
    on ``bare_symbol`` are Dalton price-move automations.
    """
    automations = list(service._symbol_automations.get((bare_symbol, watched_market), []))
    dalton_autos = [a for a in automations if is_dalton_price_move_automation(a)]
    if not dalton_autos:
        get_dalton_move_refs(service).pop(bare_symbol.upper(), None)
        return automations

    normal_autos = [a for a in automations if not is_dalton_price_move_automation(a)]
    market = watched_market

    validated = extract_today_regular_snapshot(bare_symbol, market, snapshot, now)
    if validated is None:
        record_snapshot_reference(service, bare_symbol, snapshot, market, now)
        return normal_autos or None

    _, regular_price, _, _ = validated
    record_snapshot_reference(service, bare_symbol, snapshot, market, now)
    for auto in dalton_autos:
        try:
            await service._evaluate_and_trigger(auto, regular_price)
        except Exception:
            logger.error(
                "[DaltonPriceTrigger] Poll eval error for %s",
                auto.get("automation_id"),
                exc_info=True,
            )
    return normal_autos or None


def day_trigger_lock_key(
    user_id: str,
    symbol: str,
    direction: str,
    trading_date: str,
) -> str:
    """Per-user Redis key for ``(user_id, symbol, direction, trading_date)``."""
    return (
        f"dalton:price_trigger:{user_id}:{symbol.upper()}:"
        f"{direction.lower()}:{trading_date}"
    )


def day_trigger_lock_ttl(
    symbol: str,
    watched_market: Optional[MarketType],
    now: datetime,
) -> int:
    """Compute lock TTL in seconds covering the current trading day until at
    least the next regular session open.
    """
    try:
        clock = clock_for_ref(listing_ref(symbol, watched_market))
        until_next_open = int(clock.seconds_until_next_session_open(now))
        return max(until_next_open, _DEFAULT_LOCK_TTL_SECONDS, _MIN_LOCK_TTL_SECONDS)
    except Exception:
        return _DEFAULT_LOCK_TTL_SECONDS


async def _redis_lock_exists(lock_key: str) -> bool:
    """Fast pre-check in Redis: return True only when ``lock_key`` is known to exist."""
    try:
        from src.utils.cache.redis_cache import get_cache_client

        cache = get_cache_client()
        if (
            cache is not None
            and getattr(cache, "enabled", False)
            and getattr(cache, "client", None)
        ):
            get_fn = getattr(cache.client, "get", None)
            if callable(get_fn):
                val = await get_fn(lock_key)
                return val is not None and val is not False
    except Exception:
        logger.warning(
            "[DaltonPriceTrigger] Redis pre-check failed for %s; falling through to DB transaction",
            lock_key,
            exc_info=True,
        )
    return False


async def _set_day_lock_redis(
    lock_key: str,
    lock_value: str,
    lock_ttl: int,
) -> None:
    """Write ``lock_key`` to Redis after the DB transaction has committed."""
    try:
        from src.utils.cache.redis_cache import get_cache_client

        cache = get_cache_client()
        if (
            cache is not None
            and getattr(cache, "enabled", False)
            and getattr(cache, "client", None)
        ):
            await cache.client.set(lock_key, lock_value, nx=True, ex=lock_ttl)
    except Exception:
        logger.warning(
            "[DaltonPriceTrigger] Redis post-claim write failed for %s",
            lock_key,
            exc_info=True,
        )


async def claim_dalton_price_move_firing(
    *,
    automation_id: str,
    user_id: str,
    symbol: str,
    direction: str,
    trading_date_iso: str,
    server_id: str,
) -> Tuple[Optional[str], bool]:
    """Atomically deduplicate via ``dalton_price_move_firings`` and claim the
    firing in a single Postgres transaction.

    Inside one ``conn.transaction()``:
      1. ``INSERT INTO dalton_price_move_firings ... ON CONFLICT DO NOTHING RETURNING automation_id``;
      2. If no row is returned, return ``(None, True)`` (already fired today);
      3. Otherwise claim ``automations`` and insert into ``automation_executions``.

    Returns ``(execution_id, already_triggered_today)``.
    """
    from src.server.database.pool import get_db_connection

    sym_upper = symbol.upper()
    dir_lower = direction.lower()
    execution_id = str(uuid4())

    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO dalton_price_move_firings (
                        user_id, symbol, direction, trading_date, automation_id
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, symbol, direction, trading_date) DO NOTHING
                    RETURNING automation_id
                    """,
                    (
                        user_id,
                        sym_upper,
                        dir_lower,
                        trading_date_iso,
                        automation_id,
                    ),
                )
                if await cur.fetchone() is None:
                    return None, True

                await cur.execute(
                    """
                    UPDATE automations
                    SET status = 'executing', next_run_at = NULL, last_run_at = NOW()
                    WHERE automation_id = %s AND user_id = %s AND status = 'active'
                    """,
                    (automation_id, user_id),
                )
                if cur.rowcount == 0:
                    await cur.execute(
                        """
                        DELETE FROM dalton_price_move_firings
                        WHERE user_id = %s AND symbol = %s
                          AND direction = %s AND trading_date = %s
                        """,
                        (user_id, sym_upper, dir_lower, trading_date_iso),
                    )
                    return None, False

                await cur.execute(
                    """
                    INSERT INTO automation_executions (
                        automation_execution_id, automation_id,
                        status, scheduled_at, server_id, created_at,
                        heartbeat_at
                    )
                    VALUES (%s, %s, 'pending', NOW(), %s, NOW(), NOW())
                    """,
                    (execution_id, automation_id, server_id),
                )
    return execution_id, False


async def try_trigger_dalton_price_move(
    service: Any,
    automation: Dict[str, Any],
    config: PriceTriggerConfig,
    current_price: float,
    now: datetime,
) -> bool:
    """Attempt to trigger a Dalton price-move automation.

    Uses Redis only as a fast pre-check, then executes the
    ``dalton_price_move_firings`` unique-constraint insert + execution claim in
    a single Postgres transaction, and writes the Redis key after commit.
    """
    symbol = config.symbol.upper()
    today_iso = trading_session_date(symbol, config.market, now)
    if today_iso is None:
        return False

    if not can_evaluate_automation(service, automation, config, now):
        return False

    direction = classify_dalton_price_move(automation, config)
    if direction is None:
        return False

    user_id = str(automation.get("user_id") or "")
    lock_key = day_trigger_lock_key(user_id, symbol, direction, today_iso)
    lock_ttl = day_trigger_lock_ttl(symbol, config.market, now)
    automation_id = str(automation["automation_id"])
    lock_value = json.dumps(
        {
            "automation_id": automation_id,
            "user_id": user_id,
            "symbol": symbol,
            "direction": direction,
            "trading_date": today_iso,
            "price": current_price,
            "triggered_at": now.astimezone(timezone.utc).isoformat(),
        }
    )

    if await _redis_lock_exists(lock_key):
        return False

    from src.server.services.automation_scheduler import AutomationScheduler

    scheduler = AutomationScheduler.get_instance()
    try:
        execution_id, already_triggered = await claim_dalton_price_move_firing(
            automation_id=automation_id,
            user_id=user_id,
            symbol=symbol,
            direction=direction,
            trading_date_iso=today_iso,
            server_id=scheduler.server_id,
        )
    except Exception:
        logger.error(
            "[DaltonPriceTrigger] Failed to claim firing for %s",
            automation_id,
            exc_info=True,
        )
        return False

    if already_triggered:
        await _set_day_lock_redis(lock_key, lock_value, lock_ttl)
        return False

    if execution_id is None:
        logger.info(
            "[DaltonPriceTrigger] Not triggering %s: no longer active",
            automation_id,
        )
        return False

    await _set_day_lock_redis(lock_key, lock_value, lock_ttl)
    scheduler.dispatch(
        automation, execution_id, name=f"price_exec_{automation_id[:8]}"
    )
    return True
