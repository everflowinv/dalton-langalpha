"""Default tier monitoring policies, DST-aware cron validation, and outbox DB helpers for Personal Watchlist."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from croniter import croniter
from psycopg.rows import dict_row

from src.dalton.coverage.models import (
    VALID_EARNINGS_STAGES,
    VALID_TIERS,
    normalize_task_overrides,
    normalize_tier,
)
from src.server.database.pool import get_db_connection

# Default schedules aligned with src/dalton/autonomy/schedule.py and calendar_state.py (§5.1)
WATCH_CRON_CORE_DEFAULT = "*/30 6-20 * * 1-5"
WATCH_CRON_WATCH_DEFAULT = "30 7 * * 1-5"
WATCH_CRON_RADAR_TEMPLATE = "30 7 * * 1-5"

DELTA_TIMEZONE_DEFAULT = "Asia/Shanghai"
DELTA_CRON_CORE_DEFAULT = "0 11 * * 2-6"
DELTA_CRON_WATCH_DEFAULT = "0 11 * * 6"
DELTA_CRON_RADAR_TEMPLATE = "0 11 * * 6"

MOVE_THRESHOLD_MIN_PCT = 1.0
MOVE_THRESHOLD_MAX_PCT = 30.0
MIN_WATCH_INTERVAL_SECONDS = 30 * 60  # 30 minutes
MIN_DELTA_INTERVAL_SECONDS = 23 * 3600  # 23 hours (DST spring-forward safe)
CRON_VALIDATION_HORIZON_DAYS = 370  # covers full year + DST transitions


@dataclass(frozen=True)
class TierPolicy:
    """Resolved monitoring policy for one personal watchlist tier."""

    tier: str
    watch_enabled: bool
    watch_cron: str
    delta_enabled: bool
    delta_cron: str
    delta_timezone: str
    move_enabled: bool
    move_threshold_pct: float
    earnings_stages: tuple[str, ...]
    policy_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["earnings_stages"] = list(self.earnings_stages)
        raw["move_threshold_pct"] = float(self.move_threshold_pct)
        return raw


DEFAULT_TIER_POLICIES: dict[str, TierPolicy] = {
    "core": TierPolicy(
        tier="core",
        watch_enabled=True,
        watch_cron=WATCH_CRON_CORE_DEFAULT,
        delta_enabled=True,
        delta_cron=DELTA_CRON_CORE_DEFAULT,
        delta_timezone=DELTA_TIMEZONE_DEFAULT,
        move_enabled=True,
        move_threshold_pct=4.0,
        earnings_stages=("tm3", "t0", "tp1"),
        policy_version=1,
    ),
    "watch": TierPolicy(
        tier="watch",
        watch_enabled=True,
        watch_cron=WATCH_CRON_WATCH_DEFAULT,
        delta_enabled=True,
        delta_cron=DELTA_CRON_WATCH_DEFAULT,
        delta_timezone=DELTA_TIMEZONE_DEFAULT,
        move_enabled=True,
        move_threshold_pct=4.0,
        earnings_stages=("t0",),
        policy_version=1,
    ),
    "radar": TierPolicy(
        tier="radar",
        watch_enabled=False,
        watch_cron=WATCH_CRON_RADAR_TEMPLATE,
        delta_enabled=False,
        delta_cron=DELTA_CRON_RADAR_TEMPLATE,
        delta_timezone=DELTA_TIMEZONE_DEFAULT,
        move_enabled=False,
        move_threshold_pct=6.0,
        earnings_stages=(),
        policy_version=1,
    ),
}

_UNKNOWN_TIER_DISABLED_POLICY = TierPolicy(
    tier="unknown",
    watch_enabled=False,
    watch_cron=WATCH_CRON_RADAR_TEMPLATE,
    delta_enabled=False,
    delta_cron=DELTA_CRON_RADAR_TEMPLATE,
    delta_timezone=DELTA_TIMEZONE_DEFAULT,
    move_enabled=False,
    move_threshold_pct=6.0,
    earnings_stages=(),
    policy_version=1,
)


def get_default_tier_policy(tier: str | None) -> TierPolicy:
    """Return the default policy for ``tier``; unknown tiers resolve to an all-disabled policy."""
    norm = str(tier or "").strip().lower()
    return DEFAULT_TIER_POLICIES.get(norm, _UNKNOWN_TIER_DISABLED_POLICY)


def policy_from_mapping(tier: str, data: Mapping[str, Any] | None) -> TierPolicy:
    """Build a ``TierPolicy`` from a DB row or mapping, falling back to tier defaults."""
    base = get_default_tier_policy(tier)
    if not isinstance(data, Mapping):
        return base
    raw_stages = data.get("earnings_stages")
    if isinstance(raw_stages, str):
        try:
            raw_stages = json.loads(raw_stages)
        except Exception:
            raw_stages = list(base.earnings_stages)
    if isinstance(raw_stages, (list, tuple)):
        stage_set = {str(s or "").strip().lower() for s in raw_stages if str(s or "").strip()}
        stages = tuple(s for s in VALID_EARNINGS_STAGES if s in stage_set)
    else:
        stages = base.earnings_stages
    return TierPolicy(
        tier=str(data.get("tier") or base.tier).strip().lower(),
        watch_enabled=bool(data.get("watch_enabled", base.watch_enabled)),
        watch_cron=str(data.get("watch_cron") or base.watch_cron).strip(),
        delta_enabled=bool(data.get("delta_enabled", base.delta_enabled)),
        delta_cron=str(data.get("delta_cron") or base.delta_cron).strip(),
        delta_timezone=str(data.get("delta_timezone") or base.delta_timezone).strip()
        or DELTA_TIMEZONE_DEFAULT,
        move_enabled=bool(data.get("move_enabled", base.move_enabled)),
        move_threshold_pct=float(
            data.get("move_threshold_pct")
            if data.get("move_threshold_pct") is not None
            else base.move_threshold_pct
        ),
        earnings_stages=stages,
        policy_version=int(data.get("policy_version") or base.policy_version),
    )


def resolve_effective_company_policy(
    tier_policy: TierPolicy,
    task_overrides: Mapping[str, Any] | None = None,
) -> TierPolicy:
    """Merge optional per-company ``task_overrides`` on top of ``tier_policy``."""
    norm_ov = normalize_task_overrides(task_overrides)
    if not norm_ov:
        return tier_policy
    return TierPolicy(
        tier=tier_policy.tier,
        watch_enabled=bool(norm_ov.get("watch_enabled", tier_policy.watch_enabled)),
        watch_cron=tier_policy.watch_cron,
        delta_enabled=bool(norm_ov.get("delta_enabled", tier_policy.delta_enabled)),
        delta_cron=tier_policy.delta_cron,
        delta_timezone=tier_policy.delta_timezone,
        move_enabled=bool(norm_ov.get("move_enabled", tier_policy.move_enabled)),
        move_threshold_pct=tier_policy.move_threshold_pct,
        earnings_stages=tuple(
            norm_ov["earnings_stages"]
            if "earnings_stages" in norm_ov
            else tier_policy.earnings_stages
        ),
        policy_version=tier_policy.policy_version,
    )


def _validate_5field_cron(expr: str, field_name: str) -> str:
    cleaned = " ".join(str(expr or "").strip().split())
    parts = cleaned.split(" ") if cleaned else []
    if len(parts) != 5:
        raise ValueError(f"{field_name} 必须是 5 段标准 Cron 表达式（当前：{expr!r}）。")
    if not croniter.is_valid(cleaned):
        raise ValueError(f"{field_name} Cron 表达式语法无效：{expr!r}。")
    return cleaned


def _iter_cron_datetimes(
    expr: str,
    tz_name: str,
    *,
    start_utc: datetime | None = None,
    horizon_days: int = CRON_VALIDATION_HORIZON_DAYS,
) -> list[datetime]:
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:
        raise ValueError(f"无效时区：{tz_name!r}") from exc
    base_utc = start_utc or datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    if base_utc.tzinfo is None:
        base_utc = base_utc.replace(tzinfo=UTC)
    base_local = base_utc.astimezone(tz)
    end_utc = base_utc + timedelta(days=horizon_days)
    it = croniter(expr, base_local)
    fires: list[datetime] = []
    # Cap iteration count defensively (e.g. every-minute cron) — fail fast on first violation
    while True:
        nxt = it.get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=tz)
        nxt_utc = nxt.astimezone(UTC)
        if nxt_utc > end_utc:
            break
        fires.append(nxt)
        if len(fires) > 25_000:
            break
    return fires


def validate_watch_cron(
    expr: str,
    *,
    timezones: Sequence[str] = ("America/New_York", "Asia/Shanghai"),
    start_utc: datetime | None = None,
) -> str:
    """Validate 5-field watch cron across market timezones (including DST): Δt >= 30 min."""
    cleaned = _validate_5field_cron(expr, "watch_cron")
    for tz_name in timezones:
        fires = _iter_cron_datetimes(cleaned, tz_name, start_utc=start_utc)
        if not fires:
            raise ValueError(f"watch_cron 在未来 {CRON_VALIDATION_HORIZON_DAYS} 天内无触发时间（时区 {tz_name}）。")
        prev_utc: datetime | None = None
        for dt in fires:
            cur_utc = dt.astimezone(UTC)
            if prev_utc is not None:
                gap = (cur_utc - prev_utc).total_seconds()
                if gap < MIN_WATCH_INTERVAL_SECONDS:
                    raise ValueError(
                        f"盘中盯盘相邻触发间隔不得小于 30 分钟（时区 {tz_name} 检测到 {int(gap // 60)} 分钟间隔）。"
                    )
            prev_utc = cur_utc
    return cleaned


def validate_delta_cron(
    expr: str,
    timezone_name: str = DELTA_TIMEZONE_DEFAULT,
    *,
    start_utc: datetime | None = None,
) -> tuple[str, str]:
    """Validate 5-field delta cron in ``timezone_name`` (DST-aware): max 1/local-day and Δt >= 23h."""
    cleaned = _validate_5field_cron(expr, "delta_cron")
    tz_clean = str(timezone_name or DELTA_TIMEZONE_DEFAULT).strip() or DELTA_TIMEZONE_DEFAULT
    fires = _iter_cron_datetimes(cleaned, tz_clean, start_utc=start_utc)
    if not fires:
        raise ValueError(f"delta_cron 在未来 {CRON_VALIDATION_HORIZON_DAYS} 天内无触发时间（时区 {tz_clean}）。")
    seen_local_days: set[str] = set()
    prev_utc: datetime | None = None
    for dt in fires:
        local_day = dt.date().isoformat()
        if local_day in seen_local_days:
            raise ValueError(f"论点增量每日最多触发 1 次（时区 {tz_clean} 在 {local_day} 多次触发）。")
        seen_local_days.add(local_day)
        cur_utc = dt.astimezone(UTC)
        if prev_utc is not None:
            gap = (cur_utc - prev_utc).total_seconds()
            if gap < MIN_DELTA_INTERVAL_SECONDS:
                raise ValueError(
                    f"论点增量相邻触发间隔不得小于 23 小时（时区 {tz_clean} 检测到 {gap / 3600:.2f} 小时间隔）。"
                )
        prev_utc = cur_utc
    return cleaned, tz_clean


def validate_move_threshold_pct(value: Any) -> float:
    """Validate price move threshold percentage in [1.0, 30.0]."""
    if isinstance(value, bool):
        raise ValueError("异动涨跌幅阈值必须为数值（1.0%–30.0%）。")
    try:
        pct = round(float(value), 2)
    except (TypeError, ValueError) as exc:
        raise ValueError("异动涨跌幅阈值必须为数值（1.0%–30.0%）。") from exc
    if pct < MOVE_THRESHOLD_MIN_PCT or pct > MOVE_THRESHOLD_MAX_PCT:
        raise ValueError("异动涨跌幅阈值须介于 1.0% 至 30.0% 之间。")
    return pct


def validate_earnings_stages(stages: Any) -> tuple[str, ...]:
    """Validate ordered subset of ('tm3', 't0', 'tp1')."""
    if not isinstance(stages, (list, tuple, set)):
        raise ValueError("earnings_stages 必须为阶段列表。")
    raw_set = {str(s or "").strip().lower() for s in stages if str(s or "").strip()}
    invalid = sorted(raw_set - set(VALID_EARNINGS_STAGES))
    if invalid:
        raise ValueError(f"不支持的财报写稿阶段：{', '.join(invalid)}")
    return tuple(s for s in VALID_EARNINGS_STAGES if s in raw_set)


def default_tier_policies_yaml_dict() -> dict[str, dict[str, Any]]:
    """Return canonical dict of the 3 default tier policies for YAML serialization."""
    return {
        tier: {
            "watch_enabled": p.watch_enabled,
            "watch_cron": p.watch_cron,
            "delta_enabled": p.delta_enabled,
            "delta_cron": p.delta_cron,
            "delta_timezone": p.delta_timezone,
            "move_enabled": p.move_enabled,
            "move_threshold_pct": p.move_threshold_pct,
            "earnings_stages": list(p.earnings_stages),
            "policy_version": p.policy_version,
        }
        for tier, p in DEFAULT_TIER_POLICIES.items()
    }


# ---------------------------------------------------------------------------
# DB persistence helpers: dalton_watchlist_tier_policies & reconcile outbox
# ---------------------------------------------------------------------------


def _ensure_json(val: Any, default: Any) -> Any:
    if val is None:
        return default
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return default
    return val


async def get_user_tier_policies(
    user_id: str,
    *,
    conn: Any = None,
) -> dict[str, TierPolicy]:
    """Return effective ``TierPolicy`` for ``core``, ``watch``, and ``radar`` for ``user_id``.

    Falls back to ``DEFAULT_TIER_POLICIES`` for any tier without a custom DB row
    (or when running in environments without custom policy rows yet).
    """
    uid = str(user_id or "").strip()
    resolved: dict[str, TierPolicy] = dict(DEFAULT_TIER_POLICIES)
    if not uid:
        return resolved

    try:
        async with get_db_connection(conn) as c:
            async with c.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        user_id, tier, watch_enabled, watch_cron,
                        delta_enabled, delta_cron, delta_timezone,
                        move_enabled, move_threshold_pct, earnings_stages,
                        policy_version, updated_at
                    FROM dalton_watchlist_tier_policies
                    WHERE user_id = %s
                    """,
                    (uid,),
                )
                rows = await cur.fetchall()
    except Exception as exc:
        msg = str(exc).lower()
        if any(
            token in msg
            for token in (
                "pool is not open",
                "not initialized",
                "connection pool",
                "no running event loop",
                "does not exist",
            )
        ):
            return resolved
        raise

    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        t = str(row.get("tier") or "").strip().lower()
        if t in VALID_TIERS:
            resolved[t] = policy_from_mapping(t, row)
    return resolved


async def get_user_tier_policy(
    user_id: str,
    tier: str,
    *,
    conn: Any = None,
) -> TierPolicy:
    """Return the effective ``TierPolicy`` for ``(user_id, tier)``."""
    norm = str(tier or "").strip().lower()
    if norm not in VALID_TIERS:
        return _UNKNOWN_TIER_DISABLED_POLICY
    policies = await get_user_tier_policies(user_id, conn=conn)
    return policies.get(norm, get_default_tier_policy(norm))


def _normalize_reconcile_job_row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    if out.get("job_id") is not None:
        out["job_id"] = str(out["job_id"])
    out["target_policy_versions"] = dict(
        _ensure_json(out.get("target_policy_versions"), {}) or {}
    )
    out["pending_tickers"] = list(_ensure_json(out.get("pending_tickers"), []) or [])
    out["completed_tickers"] = list(_ensure_json(out.get("completed_tickers"), []) or [])
    out["failed_items"] = list(_ensure_json(out.get("failed_items"), []) or [])
    out["override_user_pause"] = bool(out.get("override_user_pause", False))
    out["job_context"] = dict(_ensure_json(out.get("job_context"), {}) or {})
    out["claim_token"] = (
        str(out["claim_token"]).strip()
        if out.get("claim_token") is not None and str(out["claim_token"]).strip()
        else None
    )
    out["roster_revision"] = int(out.get("roster_revision") or 0)
    out["yaml_synced"] = bool(out.get("yaml_synced", False))
    return out


_RECONCILE_JOB_SELECT_COLS = """
    job_id::text AS job_id, user_id, trigger_action,
    target_policy_versions, pending_tickers, completed_tickers,
    failed_items, override_user_pause, job_context,
    claim_token, claim_expires_at, roster_revision,
    yaml_synced, status, created_at, updated_at
"""

CONFIRM_SNAPSHOT_TTL_SECONDS = 900
DEFAULT_OUTBOX_LEASE_SECONDS = 300


def _is_pool_unavailable_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "pool is not open",
            "not initialized",
            "connection pool",
            "no running event loop",
            "does not exist",
        )
    )


# ---------------------------------------------------------------------------
# Server-issued confirmation snapshots & user roster revision helpers
# ---------------------------------------------------------------------------


async def insert_confirm_snapshot(
    user_id: str,
    payload: Mapping[str, Any],
    *,
    ttl_seconds: int = CONFIRM_SNAPSHOT_TTL_SECONDS,
    snapshot_id: str | None = None,
    conn: Any = None,
) -> str:
    """Persist a server-issued, single-use confirmation snapshot and return its UUID."""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    sid = str(snapshot_id or uuid.uuid4())
    payload_json = json.dumps(dict(payload or {}), ensure_ascii=False, default=str)
    ttl = max(30, int(ttl_seconds))

    async def _run(c: Any) -> str:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_watchlist_confirm_snapshots (
                    snapshot_id, user_id, payload, expires_at, created_at
                )
                VALUES (
                    %s::uuid, %s, %s::jsonb, NOW() + (%s * INTERVAL '1 second'), NOW()
                )
                RETURNING snapshot_id::text AS snapshot_id
                """,
                (sid, uid, payload_json, ttl),
            )
            row = await cur.fetchone()
            return str((row or {}).get("snapshot_id") or sid)

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as c, c.transaction():
        return await _run(c)


async def get_confirm_snapshot(
    user_id: str,
    snapshot_id: str,
    *,
    for_update: bool = False,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Fetch a server-issued confirmation snapshot belonging to ``user_id``."""
    uid = str(user_id or "").strip()
    sid = str(snapshot_id or "").strip()
    if not uid or not sid:
        return None
    try:
        uuid.UUID(sid)
    except ValueError:
        return None

    lock_sql = " FOR UPDATE" if for_update else ""
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT
                    snapshot_id::text AS snapshot_id,
                    user_id,
                    payload,
                    expires_at,
                    consumed_at,
                    (expires_at <= NOW()) AS is_expired,
                    created_at
                FROM dalton_watchlist_confirm_snapshots
                WHERE user_id = %s AND snapshot_id = %s::uuid
                {lock_sql}
                """,
                (uid, sid),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            out = dict(row)
            out["payload"] = dict(_ensure_json(out.get("payload"), {}) or {})
            out["is_expired"] = bool(out.get("is_expired", False))
            return out


async def consume_confirm_snapshot(
    user_id: str,
    snapshot_id: str,
    *,
    conn: Any = None,
) -> bool:
    """Atomically mark a confirmation snapshot as consumed; returns False if already used or expired."""
    uid = str(user_id or "").strip()
    sid = str(snapshot_id or "").strip()
    if not uid or not sid:
        return False
    try:
        uuid.UUID(sid)
    except ValueError:
        return False

    async def _run(c: Any) -> bool:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE dalton_watchlist_confirm_snapshots
                SET consumed_at = NOW()
                WHERE user_id = %s
                  AND snapshot_id = %s::uuid
                  AND consumed_at IS NULL
                  AND expires_at > NOW()
                RETURNING snapshot_id::text AS snapshot_id
                """,
                (uid, sid),
            )
            return (await cur.fetchone()) is not None

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as c, c.transaction():
        return await _run(c)


async def get_user_roster_state(
    user_id: str,
    *,
    conn: Any = None,
) -> dict[str, int]:
    """Return ``{roster_revision, last_yaml_synced_revision}`` for ``user_id``."""
    uid = str(user_id or "").strip()
    default_state = {"roster_revision": 0, "last_yaml_synced_revision": 0}
    if not uid:
        return default_state
    try:
        async with get_db_connection(conn) as c:
            async with c.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT roster_revision, last_yaml_synced_revision
                    FROM dalton_watchlist_user_state
                    WHERE user_id = %s
                    """,
                    (uid,),
                )
                row = await cur.fetchone()
                if row is None:
                    return default_state
                return {
                    "roster_revision": int(row.get("roster_revision") or 0),
                    "last_yaml_synced_revision": int(row.get("last_yaml_synced_revision") or 0),
                }
    except Exception as exc:
        if _is_pool_unavailable_error(exc):
            return default_state
        raise


async def bump_user_roster_revision(
    user_id: str,
    *,
    conn: Any = None,
) -> int:
    """Monotonically increment ``roster_revision`` for ``user_id`` inside the caller's tx."""
    uid = str(user_id or "").strip()
    if not uid:
        return 0

    async def _run(c: Any) -> int:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_watchlist_user_state (
                    user_id, roster_revision, last_yaml_synced_revision, updated_at
                )
                VALUES (%s, 1, 0, NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET roster_revision = dalton_watchlist_user_state.roster_revision + 1,
                    updated_at = NOW()
                RETURNING roster_revision
                """,
                (uid,),
            )
            row = await cur.fetchone()
            return int((row or {}).get("roster_revision") or 1)

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as c, c.transaction():
        return await _run(c)


async def record_yaml_synced_revision(
    user_id: str,
    revision: int,
    *,
    conn: Any = None,
) -> bool:
    """Advance ``last_yaml_synced_revision`` iff ``revision >= last_yaml_synced_revision``."""
    uid = str(user_id or "").strip()
    rev = max(0, int(revision))
    if not uid:
        return False

    async def _run(c: Any) -> bool:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_watchlist_user_state (
                    user_id, roster_revision, last_yaml_synced_revision, updated_at
                )
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id) DO NOTHING
                """,
                (uid, rev, rev),
            )
            await cur.execute(
                """
                UPDATE dalton_watchlist_user_state
                SET last_yaml_synced_revision = %s,
                    updated_at = NOW()
                WHERE user_id = %s
                  AND last_yaml_synced_revision <= %s
                RETURNING last_yaml_synced_revision
                """,
                (rev, uid, rev),
            )
            return (await cur.fetchone()) is not None

    try:
        if conn is not None:
            return await _run(conn)
        async with get_db_connection() as c, c.transaction():
            return await _run(c)
    except Exception as exc:
        if _is_pool_unavailable_error(exc):
            return True
        raise


async def insert_reconcile_job(
    user_id: str,
    *,
    trigger_action: str,
    target_policy_versions: Mapping[str, int],
    pending_tickers: Sequence[str],
    override_user_pause: bool = False,
    job_context: Mapping[str, Any] | None = None,
    roster_revision: int = 0,
    job_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Insert an outbox row into ``dalton_watchlist_reconcile_jobs`` inside the caller's tx."""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    jid = str(job_id or uuid.uuid4())
    cleaned_tickers = list(
        dict.fromkeys(
            str(t or "").strip().upper()
            for t in pending_tickers
            if str(t or "").strip()
        )
    )
    versions_json = json.dumps(
        {str(k): int(v) for k, v in (target_policy_versions or {}).items()},
        ensure_ascii=False,
    )
    pending_json = json.dumps(cleaned_tickers, ensure_ascii=False)
    context_json = json.dumps(dict(job_context or {}), ensure_ascii=False)

    async def _run(c: Any) -> dict[str, Any]:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO dalton_watchlist_reconcile_jobs (
                    job_id, user_id, trigger_action, target_policy_versions,
                    pending_tickers, completed_tickers, failed_items,
                    override_user_pause, job_context, roster_revision,
                    yaml_synced, status, created_at, updated_at
                )
                VALUES (
                    %s::uuid, %s, %s, %s::jsonb,
                    %s::jsonb, '[]'::jsonb, '[]'::jsonb,
                    %s, %s::jsonb, %s,
                    FALSE, 'pending', NOW(), NOW()
                )
                RETURNING {_RECONCILE_JOB_SELECT_COLS}
                """,
                (
                    jid,
                    uid,
                    str(trigger_action or "mutate")[:32],
                    versions_json,
                    pending_json,
                    bool(override_user_pause),
                    context_json,
                    int(roster_revision or 0),
                ),
            )
            row = await cur.fetchone()
            norm = _normalize_reconcile_job_row(row)
            assert norm is not None
            return norm

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as c, c.transaction():
        return await _run(c)


async def get_reconcile_job(
    user_id: str,
    job_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Fetch a single outbox job belonging to ``user_id``."""
    uid = str(user_id or "").strip()
    jid = str(job_id or "").strip()
    if not uid or not jid:
        return None
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_RECONCILE_JOB_SELECT_COLS}
                FROM dalton_watchlist_reconcile_jobs
                WHERE user_id = %s AND job_id = %s::uuid
                """,
                (uid, jid),
            )
            return _normalize_reconcile_job_row(await cur.fetchone())


async def claim_reconcile_job(
    user_id: str,
    job_id: str,
    *,
    stale_seconds: int = DEFAULT_OUTBOX_LEASE_SECONDS,
    lease_seconds: int | None = None,
    force_claim: bool = False,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Atomically claim one outbox job with an exclusive lease token."""
    uid = str(user_id or "").strip()
    jid = str(job_id or "").strip()
    if not uid or not jid:
        return None
    eff_lease = max(1, int(lease_seconds if lease_seconds is not None else stale_seconds))
    token = str(uuid.uuid4())

    async def _run(c: Any) -> dict[str, Any] | None:
        async with c.cursor(row_factory=dict_row) as cur:
            if force_claim:
                await cur.execute(
                    f"""
                    UPDATE dalton_watchlist_reconcile_jobs
                    SET status = 'running',
                        claim_token = %s,
                        claim_expires_at = NOW() + (%s * INTERVAL '1 second'),
                        updated_at = NOW()
                    WHERE user_id = %s
                      AND job_id = %s::uuid
                      AND (status IN ('pending', 'running', 'partial') OR yaml_synced = FALSE)
                    RETURNING {_RECONCILE_JOB_SELECT_COLS}
                    """,
                    (token, eff_lease, uid, jid),
                )
            else:
                await cur.execute(
                    f"""
                    UPDATE dalton_watchlist_reconcile_jobs
                    SET status = 'running',
                        claim_token = %s,
                        claim_expires_at = NOW() + (%s * INTERVAL '1 second'),
                        updated_at = NOW()
                    WHERE user_id = %s
                      AND job_id = %s::uuid
                      AND (
                          (
                              status IN ('pending', 'partial')
                              AND (claim_token IS NULL OR claim_expires_at IS NULL OR claim_expires_at <= NOW())
                          )
                          OR (
                              status = 'running'
                              AND (
                                  (claim_expires_at IS NOT NULL AND claim_expires_at <= NOW())
                                  OR (claim_expires_at IS NULL AND updated_at < NOW() - (%s * INTERVAL '1 second'))
                              )
                          )
                          OR (
                              status = 'completed'
                              AND yaml_synced = FALSE
                              AND (claim_token IS NULL OR claim_expires_at IS NULL OR claim_expires_at <= NOW())
                          )
                      )
                    RETURNING {_RECONCILE_JOB_SELECT_COLS}
                    """,
                    (token, eff_lease, uid, jid, eff_lease),
                )
            return _normalize_reconcile_job_row(await cur.fetchone())

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as c, c.transaction():
        return await _run(c)


async def claim_unfinished_reconcile_jobs_for_startup(
    *,
    limit: int = 100,
    exclude_job_ids: Sequence[str] | None = None,
    lease_seconds: int = DEFAULT_OUTBOX_LEASE_SECONDS,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Atomically claim claimable outbox jobs across all users without stealing active leases."""
    eff_lease = max(1, int(lease_seconds))
    excl: list[str] = []
    for raw_id in exclude_job_ids or ():
        s = str(raw_id or "").strip()
        if not s:
            continue
        try:
            uuid.UUID(s)
            excl.append(s)
        except ValueError:
            continue

    async def _run(c: Any) -> list[dict[str, Any]]:
        async with c.cursor(row_factory=dict_row) as cur:
            excl_clause = "AND NOT (job_id = ANY(%s::uuid[]))" if excl else ""
            params: list[Any] = [eff_lease]
            if excl:
                params.append(excl)
            params.extend([max(1, int(limit)), eff_lease])
            await cur.execute(
                f"""
                WITH picked AS (
                    SELECT job_id
                    FROM dalton_watchlist_reconcile_jobs
                    WHERE (
                        (
                            status IN ('pending', 'partial')
                            AND (claim_token IS NULL OR claim_expires_at IS NULL OR claim_expires_at <= NOW())
                        )
                        OR (
                            status = 'running'
                            AND (
                                (claim_expires_at IS NOT NULL AND claim_expires_at <= NOW())
                                OR (claim_expires_at IS NULL AND updated_at < NOW() - (%s * INTERVAL '1 second'))
                            )
                        )
                        OR (
                            status = 'completed'
                            AND yaml_synced = FALSE
                            AND (claim_token IS NULL OR claim_expires_at IS NULL OR claim_expires_at <= NOW())
                        )
                    )
                    {excl_clause}
                    ORDER BY created_at ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE dalton_watchlist_reconcile_jobs j
                SET status = 'running',
                    claim_token = gen_random_uuid()::text,
                    claim_expires_at = NOW() + (%s * INTERVAL '1 second'),
                    updated_at = NOW()
                FROM picked
                WHERE j.job_id = picked.job_id
                RETURNING
                    j.job_id::text AS job_id, j.user_id, j.trigger_action,
                    j.target_policy_versions, j.pending_tickers, j.completed_tickers,
                    j.failed_items, j.override_user_pause, j.job_context,
                    j.claim_token, j.claim_expires_at, j.roster_revision,
                    j.yaml_synced, j.status, j.created_at, j.updated_at
                """,
                tuple(params),
            )
            rows = await cur.fetchall()
            return [
                norm
                for r in (rows or [])
                if (norm := _normalize_reconcile_job_row(r)) is not None
            ]

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as c, c.transaction():
        return await _run(c)


async def list_unfinished_reconcile_jobs(
    user_id: str,
    *,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Return unfinished or YAML-unsynced reconcile jobs for ``user_id`` ordered oldest-first."""
    uid = str(user_id or "").strip()
    if not uid:
        return []
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_RECONCILE_JOB_SELECT_COLS}
                FROM dalton_watchlist_reconcile_jobs
                WHERE user_id = %s
                  AND (status IN ('pending', 'running', 'partial') OR yaml_synced = FALSE)
                ORDER BY created_at ASC
                """,
                (uid,),
            )
            rows = await cur.fetchall()
            return [
                norm
                for r in (rows or [])
                if (norm := _normalize_reconcile_job_row(r)) is not None
            ]


async def renew_reconcile_job_lease(
    user_id: str,
    job_id: str,
    *,
    claim_token: str,
    lease_seconds: int = DEFAULT_OUTBOX_LEASE_SECONDS,
    conn: Any = None,
) -> bool:
    """Verify & extend an active claim lease BEFORE running side effects.

    Returns True iff ``claim_token`` still owns ``job_id`` and its lease has not expired.
    """
    uid = str(user_id or "").strip()
    jid = str(job_id or "").strip()
    tok = str(claim_token or "").strip()
    if not uid or not jid or not tok:
        return False
    eff_lease = max(1, int(lease_seconds))

    async def _run(c: Any) -> bool:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE dalton_watchlist_reconcile_jobs
                SET claim_expires_at = NOW() + (%s * INTERVAL '1 second'),
                    updated_at = NOW()
                WHERE user_id = %s
                  AND job_id = %s::uuid
                  AND claim_token = %s
                  AND (claim_expires_at IS NULL OR claim_expires_at > NOW())
                RETURNING job_id::text AS job_id
                """,
                (eff_lease, uid, jid, tok),
            )
            return (await cur.fetchone()) is not None

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as c, c.transaction():
        return await _run(c)


async def update_reconcile_job_progress(
    user_id: str,
    job_id: str,
    *,
    pending_tickers: Sequence[str] | None = None,
    completed_tickers: Sequence[str] | None = None,
    failed_items: Sequence[Mapping[str, Any]] | None = None,
    yaml_synced: bool | None = None,
    status: str | None = None,
    claim_token: str | None = None,
    release_claim: bool = False,
    only_unclaimed_completed: bool = False,
    lease_seconds: int = DEFAULT_OUTBOX_LEASE_SECONDS,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Update outbox job progress fields atomically, enforcing ``claim_token`` when supplied."""
    uid = str(user_id or "").strip()
    jid = str(job_id or "").strip()
    if not uid or not jid:
        return None

    sets: list[str] = ["updated_at = NOW()"]
    params: list[Any] = []
    if pending_tickers is not None:
        sets.append("pending_tickers = %s::jsonb")
        params.append(json.dumps(list(pending_tickers), ensure_ascii=False))
    if completed_tickers is not None:
        sets.append("completed_tickers = %s::jsonb")
        params.append(json.dumps(list(completed_tickers), ensure_ascii=False))
    if failed_items is not None:
        sets.append("failed_items = %s::jsonb")
        params.append(json.dumps([dict(x) for x in failed_items], ensure_ascii=False))
    if yaml_synced is not None:
        sets.append("yaml_synced = %s")
        params.append(bool(yaml_synced))
    if status is not None:
        if status not in ("pending", "running", "completed", "partial"):
            raise ValueError(f"Invalid reconcile job status {status!r}")
        sets.append("status = %s")
        params.append(status)
    if release_claim:
        sets.append("claim_token = NULL")
        sets.append("claim_expires_at = NULL")
    elif claim_token is not None:
        sets.append("claim_expires_at = NOW() + (%s * INTERVAL '1 second')")
        params.append(max(1, int(lease_seconds)))

    where_clauses = ["user_id = %s", "job_id = %s::uuid"]
    params.extend([uid, jid])
    if claim_token is not None:
        where_clauses.append("claim_token = %s")
        params.append(str(claim_token))
    elif only_unclaimed_completed:
        where_clauses.append("claim_token IS NULL")
        where_clauses.append("status IN ('pending', 'partial', 'completed')")
        where_clauses.append("jsonb_array_length(pending_tickers) = 0")
        where_clauses.append("jsonb_array_length(failed_items) = 0")

    async def _run(c: Any) -> dict[str, Any] | None:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_watchlist_reconcile_jobs
                SET {", ".join(sets)}
                WHERE {" AND ".join(where_clauses)}
                RETURNING {_RECONCILE_JOB_SELECT_COLS}
                """,
                tuple(params),
            )
            return _normalize_reconcile_job_row(await cur.fetchone())

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as c, c.transaction():
        return await _run(c)


__all__ = [
    "CONFIRM_SNAPSHOT_TTL_SECONDS",
    "CRON_VALIDATION_HORIZON_DAYS",
    "DEFAULT_OUTBOX_LEASE_SECONDS",
    "DEFAULT_TIER_POLICIES",
    "DELTA_CRON_CORE_DEFAULT",
    "DELTA_CRON_RADAR_TEMPLATE",
    "DELTA_CRON_WATCH_DEFAULT",
    "DELTA_TIMEZONE_DEFAULT",
    "MIN_DELTA_INTERVAL_SECONDS",
    "MIN_WATCH_INTERVAL_SECONDS",
    "MOVE_THRESHOLD_MAX_PCT",
    "MOVE_THRESHOLD_MIN_PCT",
    "TierPolicy",
    "WATCH_CRON_CORE_DEFAULT",
    "WATCH_CRON_RADAR_TEMPLATE",
    "WATCH_CRON_WATCH_DEFAULT",
    "bump_user_roster_revision",
    "claim_reconcile_job",
    "claim_unfinished_reconcile_jobs_for_startup",
    "consume_confirm_snapshot",
    "default_tier_policies_yaml_dict",
    "get_confirm_snapshot",
    "get_default_tier_policy",
    "get_reconcile_job",
    "get_user_roster_state",
    "get_user_tier_policies",
    "get_user_tier_policy",
    "insert_confirm_snapshot",
    "insert_reconcile_job",
    "list_unfinished_reconcile_jobs",
    "normalize_tier",
    "policy_from_mapping",
    "record_yaml_synced_revision",
    "renew_reconcile_job_lease",
    "resolve_effective_company_policy",
    "update_reconcile_job_progress",
    "validate_delta_cron",
    "validate_earnings_stages",
    "validate_move_threshold_pct",
    "validate_watch_cron",
]
