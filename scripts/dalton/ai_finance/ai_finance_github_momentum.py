#!/usr/bin/env python3
"""Official GitHub daily star history and deterministic breakout lifecycle.

Counts use GitHub's date buckets, not an invented rolling 24-hour window.
Never use a still-open bucket to cool down a tracked repository.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.request import Request, urlopen

_BLOCKED_PATH_PARTS = frozenset({".openclaw", ".feishu-daily-ai-finance"})
_GH_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "GH_CONFIG_DIR",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_HOST",
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "all_proxy",
    "no_proxy",
    "NO_PROXY",
)


def resolve_explicit_path(path: Path) -> Path:
    """Resolve one caller-supplied path and refuse host assistant state directories."""
    if not isinstance(path, Path):
        path = Path(path)
    resolved = path.expanduser()
    resolved = resolved.resolve() if resolved.is_absolute() else (Path.cwd() / resolved).resolve()
    if any(part in _BLOCKED_PATH_PARTS for part in resolved.parts):
        raise ValueError("refusing a path under host assistant state")
    return resolved


def _github_subprocess_env() -> dict[str, str]:
    env = {key: value for key in _GH_ENV_KEYS if (value := os.environ.get(key))}
    env.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    return env


def _redact_process_text(text: str) -> str:
    redacted = text or ""
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        secret = os.environ.get(key) or ""
        if len(secret) >= 6:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted

ENTRY_DAY = 1000
ENTRY_WEEK = 5000
SLOWDOWN_RATIO = 0.80
ACCELERATION_RATIO = 1.10
REPORT_LIMIT = 8


def significance(curve: dict[str, Any], current_stars: int, previously_reported: bool = False) -> dict[str, Any]:
    """Absolute flow AND size-adjusted flow, followed by bounded acceleration.

    Use current total as an explicit denominator, not a reconstructed historical
    baseline: daily history and the live total have different cut-off times.
    This ratio is a share of current stars, NOT a period-return percentage.
    """
    if type(current_stars) is not int or current_stars <= 0:
        raise ValueError("A positive verified current star total is required")
    day, week = curve["daily_gain"], curve["gain_7d"]
    day_pct, week_pct = 100 * day / current_stars, 100 * week / current_stars
    continuing = previously_reported and curve["phase"] != "reaccelerating"
    # A lower retention threshold avoids flickering near the entry boundary.
    day_floor, week_floor, day_share, week_share = (300, 2000, 0.5, 3.0) if continuing else (1000, 5000, 1.0, 5.0)
    meaningful = (day >= day_floor and day_pct >= day_share) or (week >= week_floor and week_pct >= week_share)
    # Same scoring scale for first coverage and follow-ups. Geometric mean
    # rewards both absolute flow and relative materiality; acceleration can
    # adjust the score by at most 20%, never rescue immaterial growth.
    base = max(math.sqrt((day / 1000) * (min(day_pct, 100) / 1)),
               math.sqrt((week / 5000) * (min(week_pct, 100) / 5)))
    ratio = curve.get("velocity_ratio")
    slope_factor = 1.2 if ratio is None and curve["daily_velocity_3d"] > 0 else min(1.2, max(0.8, ratio or 0))
    return {
        "meaningful": meaningful,
        "threshold_profile": "continuing" if continuing else "entry",
        "daily_gain_share_pct": round(day_pct, 3),
        "gain_7d_share_pct": round(week_pct, 3),
        "relative_basis": "official completed-period gain / verified current total stars",
        "breakout_score": round(base * slope_factor, 6),
    }


def report_rank(item: dict[str, Any]) -> tuple[float, float, int]:
    curve = item["momentum"]
    return (item["significance"]["breakout_score"], curve["daily_velocity_3d"], curve["gain_7d"])


def github_json(endpoint: str) -> Any:
    # Reuse the host gh credential store. The child gets a minimal environment and
    # this process never prints that environment or reads a credential file.
    if shutil.which("gh"):
        try:
            result = subprocess.run(
                ["gh", "api", "--hostname", "github.com", endpoint],
                capture_output=True,
                text=True,
                timeout=40,
                env=_github_subprocess_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"GitHub API timed out for {endpoint}") from exc
        if result.returncode:
            detail = _redact_process_text(result.stderr).strip()
            raise RuntimeError(f"GitHub API failed for {endpoint}: {detail[:240]}")
        return json.loads(result.stdout)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Eve-AI-Finance-Tracker/2.0"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urlopen(Request(f"https://api.github.com/{endpoint}", headers=headers), timeout=30) as response:
        return json.load(response)


def complete_through(now: datetime) -> date:
    # GitHub does not promise UTC-aligned day boundaries. At the scheduled 12:00
    # UTC run, yesterday has ended even in UTC-12. Earlier manual runs lag one
    # additional bucket, instead of treating partial-day counts as a slowdown.
    return (now.astimezone(timezone.utc) - timedelta(hours=12)).date() - timedelta(days=1)


def history_days(history: Any, through: date, created_at: str) -> dict[date, int]:
    if not isinstance(history, list) or not history:
        raise ValueError("GitHub star history is empty or unavailable")
    days: dict[date, int] = {}
    for week in history:
        counts = week.get("days")
        if not isinstance(counts, list) or len(counts) != 7:
            raise ValueError("GitHub star history has an incomplete week")
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("GitHub star history has invalid counts")
        if sum(counts) != week.get("total"):
            raise ValueError("GitHub star history weekly total does not match days")
        start = datetime.fromtimestamp(week["week"], timezone.utc).date()
        for offset, count in enumerate(counts):
            day = start + timedelta(days=offset)
            if day <= through:
                days[day] = count
    created = date.fromisoformat(created_at[:10])
    for offset in range(14):
        day = through - timedelta(days=offset)
        if day not in days:
            if day < created:
                days[day] = 0
            else:
                raise ValueError(f"GitHub star history missing completed date {day}")
    return days


def _speed(days: dict[date, int], end: date) -> tuple[float, float, float | None]:
    recent = sum(days[end - timedelta(days=i)] for i in range(3)) / 3
    baseline = sum(days[end - timedelta(days=i)] for i in range(3, 6)) / 3
    ratio = recent / baseline if baseline else None
    return recent, baseline, ratio


def momentum(days: dict[date, int], through: date, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    previous = previous or {}
    recent, baseline, ratio = _speed(days, through)
    _, _, prior_ratio = _speed(days, through - timedelta(days=1))
    gain_day = days[through]
    gain_week = sum(days[through - timedelta(days=i)] for i in range(7))
    entry = gain_day >= ENTRY_DAY or (gain_week >= ENTRY_WEEK and recent >= 300)
    slow_today = ratio is not None and ratio < SLOWDOWN_RATIO
    slow_yesterday = prior_ratio is not None and prior_ratio < SLOWDOWN_RATIO
    slowing_confirmed = slow_today and slow_yesterday
    was_active = bool(previous.get("momentum_active"))
    needs_reacceleration = bool(previous.get("needs_reacceleration") or previous.get("phase") == "cooling")
    accelerating = (ratio is None and recent > 0) or (ratio is not None and ratio >= ACCELERATION_RATIO)
    if slowing_confirmed or recent == 0:
        # Quiet days before a repository's first breakout are not a prior
        # cooling cycle, and must not label its first entry as reacceleration.
        phase, active = ("cooling" if was_active or needs_reacceleration else "watch"), False
    elif was_active:
        phase, active = ("slowing_unconfirmed" if slow_today else "accelerating" if accelerating else "sustained"), True
    elif entry and (not needs_reacceleration or accelerating):
        phase, active = ("reaccelerating" if needs_reacceleration else "new_breakout"), True
    else:
        phase, active = "watch", False
    return {
        "source": "GitHub stargazers/history",
        "complete_through": through.isoformat(),
        "daily_gain": gain_day, "gain_7d": gain_week,
        "daily_velocity_3d": round(recent, 2),
        "prior_daily_velocity_3d": round(baseline, 2),
        "velocity_ratio": round(ratio, 4) if ratio is not None else None,
        "velocity_change_pct": round((ratio - 1) * 100, 1) if ratio is not None else None,
        "previous_day_velocity_ratio": round(prior_ratio, 4) if prior_ratio is not None else None,
        "slowdown_confirmed": slowing_confirmed,
        "entry_threshold_met": entry, "phase": phase, "momentum_active": active,
        "needs_reacceleration": False if active else (needs_reacceleration or phase == "cooling"),
        "daily_history": [{"date": (through-timedelta(days=i)).isoformat(), "stars": days[through-timedelta(days=i)]} for i in reversed(range(10))],
    }


def evaluate_history(days: dict[date, int], through: date, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Replay missed dates and bootstrap old seen records through actual cooling.

    An old one-day spike followed by a collapse cannot be grandfathered as active
    merely because today's tiny gain is stable. Same-date reruns are idempotent.
    """
    previous = dict(previous or {})
    first_possible = min(days) + timedelta(days=9)
    if previous.get("complete_through"):
        prior_day = date.fromisoformat(previous["complete_through"])
        if prior_day > through:
            raise ValueError("Refusing to rewind momentum state to an older completed date")
        start = max(first_possible, min(through, prior_day + timedelta(days=1)))
    else:
        start = first_possible
        # No migration shortcut from previously_reported to active.
        previous.pop("momentum_active", None)
    if start > through:
        raise ValueError("Insufficient official history to evaluate momentum")
    for offset in range((through - start).days + 1):
        current = start + timedelta(days=offset)
        if not all(current-timedelta(days=i) in days for i in range(10)):
            raise ValueError("Missing dates inside the momentum replay window")
        previous = momentum(days, current, previous)
    return previous
