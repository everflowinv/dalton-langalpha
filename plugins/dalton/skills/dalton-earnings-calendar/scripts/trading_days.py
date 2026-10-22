#!/usr/bin/env python3
"""
trading_days.py — Trading-day offset calculator for Dalton earnings calendar (PS.7).

Usage:
  python3 trading_days.py --date 2026-10-28 --minus 3 --exchange XNYS
  python3 trading_days.py --date 2026-10-28 --plus 1 --exchange XNYS --at 20:00 --timezone America/New_York --json

Priority:
  1. exchange_calendars (if installed in sandbox)
  2. pandas_market_calendars (if installed in sandbox)
  3. Built-in stdlib NYSE/US market holiday calendar (XNYS/XNAS); for XHKG/XSHG/XSHE, stdlib only covers a subset of Gregorian fixed holidays (excludes lunar holidays, mainland lieu-day adjustments, and HK make-up holidays — prioritize external exchange calendars when scheduling HK/A-share dates)
"""

import argparse
from datetime import date, datetime, time, timedelta
import json
import sys
from zoneinfo import ZoneInfo


DEFAULT_EXCHANGE_TZ = {
    "XNYS": "America/New_York",
    "XNAS": "America/New_York",
    "XHKG": "Asia/Hong_Kong",
    "XSHG": "Asia/Shanghai",
    "XSHE": "Asia/Shanghai",
}


def _easter_sunday(year: int) -> date:
    """Compute Easter Sunday for a given Gregorian year (Anonymous Gregorian algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the n-th (1-based) occurrence of weekday (0=Mon..6=Sun) in (year, month)."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of weekday (0=Mon..6=Sun) in (year, month)."""
    if month == 12:
        next_first = date(year + 1, 1, 1)
    else:
        next_first = date(year, month + 1, 1)
    last_day = next_first - timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _observed(d: date, allow_prior_year: bool = False) -> date | None:
    """NYSE weekend observance rule: Saturday -> Friday, Sunday -> Monday."""
    if d.weekday() == 5:  # Saturday
        obs = d - timedelta(days=1)
        if obs.year < d.year and not allow_prior_year:
            # NYSE rule: when Jan 1 falls on Saturday, markets do NOT close on Dec 31.
            return None
        return obs
    if d.weekday() == 6:  # Sunday
        return d + timedelta(days=1)
    return d


def us_nyse_holidays(year: int) -> set[date]:
    """Return the exact set of NYSE full-day holidays for `year` using stdlib."""
    holidays: set[date] = set()

    # 1. New Year's Day (Jan 1)
    nyd = _observed(date(year, 1, 1), allow_prior_year=False)
    if nyd:
        holidays.add(nyd)

    # 2. Martin Luther King, Jr. Day (3rd Monday in January, observed by NYSE since 1998)
    if year >= 1998:
        holidays.add(_nth_weekday(year, 1, 0, 3))

    # 3. Washington's Birthday / Presidents' Day (3rd Monday in February)
    holidays.add(_nth_weekday(year, 2, 0, 3))

    # 4. Good Friday (2 days before Easter Sunday)
    holidays.add(_easter_sunday(year) - timedelta(days=2))

    # 5. Memorial Day (last Monday in May)
    holidays.add(_last_weekday(year, 5, 0))

    # 6. Juneteenth National Independence Day (June 19, observed by NYSE since 2022)
    if year >= 2022:
        jt = _observed(date(year, 6, 19))
        if jt:
            holidays.add(jt)

    # 7. Independence Day (July 4)
    ind = _observed(date(year, 7, 4))
    if ind:
        holidays.add(ind)

    # 8. Labor Day (1st Monday in September)
    holidays.add(_nth_weekday(year, 9, 0, 1))

    # 9. Thanksgiving Day (4th Thursday in November)
    holidays.add(_nth_weekday(year, 11, 3, 4))

    # 10. Christmas Day (December 25)
    xmas = _observed(date(year, 12, 25))
    if xmas:
        holidays.add(xmas)

    return holidays


def hk_fixed_holidays(year: int) -> set[date]:
    """Return a subset of Gregorian fixed HKEX holidays for `year` (stdlib fallback only).

    Note: Excludes lunar holidays (Lunar New Year, Ching Ming, Buddha's Birthday,
    Dragon Boat, Mid-Autumn, Chung Yeung) and HK make-up holidays (补假). Cannot
    be used as a complete exchange calendar; prioritize external exchange calendars
    when scheduling HK dates.
    """
    easter = _easter_sunday(year)
    holidays: set[date] = {
        date(year, 1, 1),   # New Year's Day
        easter - timedelta(days=2),  # Good Friday
        easter + timedelta(days=1),  # Easter Monday
        date(year, 5, 1),   # Labour Day
        date(year, 7, 1),   # HKSAR Establishment Day
        date(year, 10, 1),  # National Day
        date(year, 12, 25), # Christmas Day
        date(year, 12, 26), # First weekday after Christmas Day
    }
    return holidays


def cn_fixed_holidays(year: int) -> set[date]:
    """Return a subset of Gregorian fixed A-share (SSE/SZSE) holidays for `year` (stdlib fallback only).

    Note: Excludes lunar holidays (Spring Festival, Qingming, Dragon Boat, Mid-Autumn)
    and State Council lieu-day/bridge adjustments (调休/补假). Cannot be used as a
    complete exchange calendar; prioritize external exchange calendars when scheduling
    A-share dates.
    """
    return {
        date(year, 1, 1),   # New Year's Day
        date(year, 5, 1),   # Labour Day
        date(year, 10, 1),  # National Day (Oct 1-3 fixed Gregorian subset)
        date(year, 10, 2),
        date(year, 10, 3),
    }


def is_trading_day_stdlib(d: date, exchange: str = "XNYS") -> bool:
    if d.weekday() >= 5:
        return False
    ex = exchange.upper()
    if ex in ("XNYS", "XNAS", "NYSE", "NASDAQ", "US"):
        return d not in us_nyse_holidays(d.year)
    # HK/CN stdlib fallback only checks a subset of Gregorian fixed holidays (no lunar/调休/补假);
    # use external exchange calendars first when scheduling HK/A-share dates.
    if ex in ("XHKG", "HKEX", "HK"):
        return d not in hk_fixed_holidays(d.year)
    if ex in ("XSHG", "XSHE", "SSE", "SZSE", "CN"):
        return d not in cn_fixed_holidays(d.year)
    return True


def _try_external_calendar(target: date, offset: int, exchange: str) -> tuple[date | None, str | None]:
    """Try exchange_calendars or pandas_market_calendars if installed."""
    span_days = max(abs(offset) * 5 + 30, 60)
    start_d = target - timedelta(days=span_days)
    end_d = target + timedelta(days=span_days)

    # 1. Try exchange_calendars
    try:
        import exchange_calendars as xcals  # type: ignore

        cal = xcals.get_calendar(exchange.upper())
        sessions = cal.sessions_in_range(start_d.isoformat(), end_d.isoformat())
        session_dates = [s.date() for s in sessions]
        return _step_sessions(session_dates, target, offset), "exchange_calendars"
    except Exception:
        pass

    # 2. Try pandas_market_calendars
    try:
        import pandas_market_calendars as mcal  # type: ignore

        cal = mcal.get_calendar(exchange.upper())
        sched = cal.schedule(start_date=start_d.isoformat(), end_date=end_d.isoformat())
        session_dates = [ts.date() for ts in sched.index]
        return _step_sessions(session_dates, target, offset), "pandas_market_calendars"
    except Exception:
        pass

    return None, None


def _step_sessions(session_dates: list[date], anchor: date, offset: int) -> date:
    """Step `offset` trading sessions from `anchor` given an ordered list of trading dates."""
    if offset == 0:
        for d in session_dates:
            if d >= anchor:
                return d
        raise ValueError(f"No session found on or after {anchor}")

    if offset < 0:
        # Strict trading days before `anchor`
        prior = [d for d in session_dates if d < anchor]
        need = abs(offset)
        if len(prior) < need:
            raise ValueError(f"Insufficient prior sessions before {anchor}")
        return prior[-need]
    else:
        # Strict trading days after `anchor`
        after = [d for d in session_dates if d > anchor]
        if len(after) < offset:
            raise ValueError(f"Insufficient subsequent sessions after {anchor}")
        return after[offset - 1]


def compute_trading_day(anchor: date, offset: int, exchange: str = "XNYS") -> tuple[date, str]:
    """Compute anchor +/- offset trading days. Returns (result_date, backend_name)."""
    ext_date, backend = _try_external_calendar(anchor, offset, exchange)
    if ext_date is not None and backend is not None:
        return ext_date, backend

    ex = exchange.upper()
    backend_name = "stdlib_nyse" if ex in ("XNYS", "XNAS", "NYSE", "NASDAQ", "US") else "stdlib_weekday"
    if offset == 0:
        cur = anchor
        while not is_trading_day_stdlib(cur, ex):
            cur += timedelta(days=1)
        return cur, backend_name

    step = -1 if offset < 0 else 1
    remaining = abs(offset)
    cur = anchor
    while remaining > 0:
        cur += timedelta(days=step)
        if is_trading_day_stdlib(cur, ex):
            remaining -= 1
    return cur, backend_name


def format_iso_at(d: date, hhmm: str, tz_name: str) -> str:
    """Combine date and HH:MM in tz_name to produce an ISO-8601 timestamp with DST-accurate UTC offset."""
    parts = hhmm.strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"Invalid --at time format '{hhmm}', expected HH:MM or HH:MM:SS")
    hour = int(parts[0])
    minute = int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    tz = ZoneInfo(tz_name)
    dt = datetime.combine(d, time(hour, minute, second), tzinfo=tz)
    return dt.isoformat(timespec="seconds")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Compute T-N or T+N trading days for Dalton earnings automations (PS.7)."
    )
    p.add_argument("--date", required=True, help="Anchor date in YYYY-MM-DD format (e.g. earnings release date)")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--minus", type=int, help="Number of trading days before --date (e.g. 3 for tm3)")
    group.add_argument("--plus", type=int, help="Number of trading days after --date (e.g. 1 for tp1)")
    group.add_argument("--offset", type=int, help="Signed trading-day offset (e.g. -3 or +1)")
    p.add_argument("--exchange", default="XNYS", help="Exchange MIC code (default: XNYS)")
    p.add_argument("--at", default=None, help="Optional local time HH:MM to emit full timezone-aware ISO timestamp")
    p.add_argument("--timezone", default=None, help="Optional IANA timezone (default: inferred from exchange)")
    p.add_argument("--json", action="store_true", help="Output structured JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        anchor = date.fromisoformat(args.date)
    except ValueError as exc:
        err = {"ok": False, "error": f"Invalid --date '{args.date}': {exc}", "hint": "Use YYYY-MM-DD"}
        if args.json:
            print(json.dumps(err, ensure_ascii=False))
        else:
            print(f"Error: {err['error']}", file=sys.stderr)
        return 1

    if args.minus is not None:
        if args.minus < 0:
            print("Error: --minus must be non-negative", file=sys.stderr)
            return 1
        offset = -args.minus
    elif args.plus is not None:
        if args.plus < 0:
            print("Error: --plus must be non-negative", file=sys.stderr)
            return 1
        offset = args.plus
    else:
        offset = args.offset

    exchange = args.exchange.upper()
    tz_name = args.timezone or DEFAULT_EXCHANGE_TZ.get(exchange, "America/New_York")

    try:
        result_date, backend = compute_trading_day(anchor, offset, exchange=exchange)
        iso_dt = format_iso_at(result_date, args.at, tz_name) if args.at else None
    except Exception as exc:
        err = {"ok": False, "error": str(exc)}
        if args.json:
            print(json.dumps(err, ensure_ascii=False))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "ok": True,
            "anchor_date": anchor.isoformat(),
            "offset": offset,
            "exchange": exchange,
            "trading_date": result_date.isoformat(),
            "timezone": tz_name,
            "next_run_at": iso_dt,
            "calendar_backend": backend,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if iso_dt:
            print(iso_dt)
        else:
            print(result_date.isoformat())
    return 0


if __name__ == "__main__":
    sys.exit(main())
