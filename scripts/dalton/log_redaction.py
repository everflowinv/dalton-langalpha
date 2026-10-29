#!/usr/bin/env python3
"""Shared secret redaction and log-field hygiene for Dalton (P0 fix1, 2026-10-28).

One pure-standard-library module, imported by both the researchdata bridge
(``scripts/dalton/researchdata_bridge.py``), the host-side HTTP bridges, and
the review-window export script — never ``src.*``, never ``.env``.

The secret patterns were lifted verbatim from the researchdata bridge's
muse-verified stack (``_STDERR_SECRET_PATTERNS`` + the D-r2 failure-log
layer); the export extras (``/mcp/<token>`` paths, AWS key ids, Slack tokens)
layer on top. Fail-closed: a line that cannot be processed still goes out
redacted, never raw.

CLI (used by ``scripts/dalton/export_review_window.sh``):

    python3 log_redaction.py INFILE OUTFILE [--since TS] [--until TS]
    python3 log_redaction.py --normalize-window '2h'        # prints 'YYYY-MM-DD HH:MM:SS'

``--since``/``--until`` keep only lines whose leading ``YYYY-MM-DD HH:MM:SS``
timestamp (the Python logging default both bridges write) falls in the window;
lines with no parseable timestamp are kept (fail-open) so a stack trace or
continuation line is never dropped silently.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ============================================================================
# Secret patterns — the researchdata bridge's muse-verified stack, verbatim
# ============================================================================

# D-r2 failure-log layer: URL query values, leftover query pairs, whole
# Cookie/Set-Cookie strings (to end of line), emails, mainland-China phones.
FAILURE_LOG_URL_RE = re.compile(r"(?i)(?:https?|ftp)://[^\s\"'<>]+")
FAILURE_LOG_QUERY_PAIR_RE = re.compile(r"([?&][^\s=&#?\"'<>]+=)[^\s&#\"'<>]*")
FAILURE_LOG_COOKIE_RE = re.compile(r"(?i)(\b(?:set-)?cookie\b\s*[:=]\s*)[^\r\n]*")
FAILURE_LOG_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
FAILURE_LOG_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def redact_url_query(url: str) -> str:
    """A URL keeping its parameter names but no values, fragment cut too."""
    head, sep, query = url.partition("?")
    if not sep:
        return url
    names = [f"{part.split('=', 1)[0]}=…" for part in query.split("&") if part]
    return f"{head}?{'&'.join(names)}" if names else f"{head}?"


_SECRET_KEY_BODY_PAT = (
    r"(?:"
    r"[A-Za-z0-9_.\-]*(?:api[_-]?key|apikey|token|secret|password|passwd|cookie|set-cookie|"
    r"authorization|proxy-authorization|x-api-key|auth|credential|session[_-]?id|signature|"
    r"xq_[a-z0-9_]+)[A-Za-z0-9_.\-]*"
    r"|(?:[A-Za-z0-9_.\-]*[_-])?(?:key|sig)"
    r")"
)

# The bridge's stderr pattern stack, single source of truth for both sides.
STDERR_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?im)^([^\r\n]*?\b(?:set-)?cookie\s*:\s*)[^\r\n]+$"),
        r"\1[REDACTED]",
    ),
    # P0 fix2 B: backslash-escaped JSON key/value pairs (e.g. \"token\": \"…\")
    # when a JSON payload is embedded inside another string or repr.
    (
        re.compile(
            rf"""(?i)((?:\\+['"])?\b{_SECRET_KEY_BODY_PAT}(?:\\+['"])?\s*[:=]\s*)(\\+['"])(?:(?!\2).)*\2"""
        ),
        r"\1\2[REDACTED]\2",
    ),
    (
        re.compile(
            rf"""(?i)(['"]?\b{_SECRET_KEY_BODY_PAT}['"]?\s*[:=]\s*)(['"])(?:\\.|(?!\2).)*\2"""
        ),
        r"\1\2[REDACTED]\2",
    ),
    (
        re.compile(
            r"(?i)(\b(?:proxy-)?authorization\s*[:=]\s*)(?:(?:bearer|basic|digest|token)\s+)?(?!['\"]?\[REDACTED\])[^\s;,'\"}\]\r\n]+"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(?i)((?<![A-Za-z0-9])bearer\s+)(?!\[REDACTED\]|bearer\b)[A-Za-z0-9._\-+/=~]{4,}"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            rf"(?i)(['\"]?\b{_SECRET_KEY_BODY_PAT}['\"]?\s*[:=]\s*)(?!['\"]?\[REDACTED\]|…)[^\s&;,'\"}}""\\]\r\n]+"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(?<![0-9A-Za-z_\-])(sk-ant-[A-Za-z0-9_\-]{8,})(?![0-9A-Za-z_\-])"), "[REDACTED]"),
    (re.compile(r"(?<![0-9A-Za-z_\-])(sk-[A-Za-z0-9_\-]{8,})(?![0-9A-Za-z_\-])"), "[REDACTED]"),
    (re.compile(r"(?<![0-9A-Za-z_\-])(gsk_[A-Za-z0-9_\-]{8,})(?![0-9A-Za-z_\-])"), "[REDACTED]"),
    (re.compile(r"(?<![0-9A-Za-z_\-])(AIza[0-9A-Za-z_\-]{35})(?![0-9A-Za-z_\-])"), "[REDACTED]"),
    (re.compile(r"(?<![0-9A-Za-z_\-])(xai-[A-Za-z0-9_\-]{8,})(?![0-9A-Za-z_\-])"), "[REDACTED]"),
    (re.compile(r"(?<![0-9A-Za-z_\-])(tvly-[A-Za-z0-9_\-]{8,})(?![0-9A-Za-z_\-])"), "[REDACTED]"),
    (re.compile(r"(?<![0-9A-Za-z_\-])(fc-[A-Za-z0-9_\-]{8,})(?![0-9A-Za-z_\-])"), "[REDACTED]"),
    (re.compile(r"(?<![0-9A-Za-z_\-])(ghp_[A-Za-z0-9_\-]{8,})(?![0-9A-Za-z_\-])"), "[REDACTED]"),
    (re.compile(r"(?<![0-9A-Za-z_\-])(github_pat_[A-Za-z0-9_\-]{8,})(?![0-9A-Za-z_\-])"), "[REDACTED]"),
    (re.compile(r"(?<![0-9A-Za-z_\-])(xox[baprs]-[A-Za-z0-9\-]{8,})(?![0-9A-Za-z_\-])"), "[REDACTED]"),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://[REDACTED]@"),
    (re.compile(r"\b[0-9a-fA-F]{40,}\b"), "[REDACTED]"),
    (
        re.compile(
            r"(?<![A-Za-z0-9_.\-/])(?=[A-Za-z0-9+/_-]*[A-Za-z0-9])[A-Za-z0-9+/_-]{40,}={0,2}(?![A-Za-z0-9_.\-/])"
        ),
        "[REDACTED]",
    ),
)

# ============================================================================
# Export-only extras layered on the bridge stack
# ============================================================================

# /mcp/<token> — the bridge token rides the request path; keep the /mcp/
# prefix visible (it names the route) and lose the token.
MCP_PATH_RE = re.compile(r"/mcp/[^/\s\"'?&#]+")

# AWS access-key ids and Slack bot/user tokens (fixed-shape, no key name
# beside them, so the key-value patterns never see them).
AWS_KEY_ID_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
SLACK_TOKEN_RE = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}\b")

_EXPORT_EXTRA_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (MCP_PATH_RE, "/mcp/<redacted>"),
    (AWS_KEY_ID_RE, "[REDACTED]"),
    (SLACK_TOKEN_RE, "[REDACTED]"),
)


def apply_failure_log_redaction(raw: str) -> str:
    """The bridge failure-log chain, untruncated: D-r2 layer first, then the
    stderr secret patterns and export extras — one shared rule set for both
    the bridge and the export script."""
    text = FAILURE_LOG_URL_RE.sub(lambda m: redact_url_query(m.group(0)), raw)
    text = FAILURE_LOG_QUERY_PAIR_RE.sub(lambda m: m.group(1) + "…", text)
    text = FAILURE_LOG_COOKIE_RE.sub(r"\1[REDACTED]", text)
    text = FAILURE_LOG_EMAIL_RE.sub("[REDACTED]", text)
    text = FAILURE_LOG_PHONE_RE.sub("[REDACTED]", text)
    text = MCP_PATH_RE.sub("/mcp/<redacted>", text)
    for pattern, replacement in STDERR_SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    for pattern, replacement in _EXPORT_EXTRA_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_text(text: str) -> str:
    """Apply every redaction rule — the bridge stack plus the export extras."""
    return apply_failure_log_redaction(text)


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def clean_log_field(value: object, *, max_len: int) -> str:
    """An externally controlled string destined for a log line: redacted,
    stripped of newlines/control characters (no forged log lines), truncated.

    The empty string for None/empty so callers can substitute their own dash."""
    if value is None:
        return ""
    raw = str(value)
    if not raw:
        return ""
    text = redact_text(raw)
    text = _CONTROL_CHARS_RE.sub(" ", text)
    return text.strip()[:max_len]


# ============================================================================
# HTTP status extraction from external error text (shared by the bridges)
# ============================================================================

_HTTP_STATUS_IN_TEXT_RES: tuple[re.Pattern[str], ...] = (
    # "HTTP 429", "http_502", "HTTP-403", "HTTP/1.1 503"
    re.compile(r"(?i)\bhttp(?:/\d(?:\.\d)?)?[ _/-]+([1-5]\d{2})\b"),
    # "status_code": 403 / 'status_code': 403 / status_code=429 — quoted or
    # bare, so a dict stringified into the error text still yields its status.
    re.compile(
        r"(?i)\b(?:status[_-]?code|http[_-]?status|upstream[_-]?status)['\"]?\s*[:=]\s*['\"]?([1-5]\d{2})\b"
    ),
    # "status 503", "status=503", "status: 503", "'status': 503"
    re.compile(r"(?i)\bstatus['\"]?\s*[ _=:\-]+\s*['\"]?([1-5]\d{2})\b"),
    # "429 Too Many Requests" — bare status followed by its reason phrase.
    re.compile(
        r"\b([1-5]\d{2})[ ](?:Too Many Requests|Bad Request|Unauthorized|"
        r"Forbidden|Not Found|Request Timeout|Conflict|Unprocessable Entity|"
        r"Internal Server Error|Bad Gateway|Service Unavailable|"
        r"Gateway Timeout)\b"
    ),
)


def http_status_from_text(text: str) -> int | None:
    """First explicit HTTP status marker in ``text``: ``HTTP <ddd>``,
    ``status[_-]?code`` (bare or inside a stringified JSON/repr dict),
    ``status=ddd``, or ``<ddd> <reason phrase>``. None when none says one."""
    for pattern in _HTTP_STATUS_IN_TEXT_RES:
        match = pattern.search(text)
        if match is not None:
            status = int(match.group(1))
            if 100 <= status <= 599:
                return status
    return None


# ============================================================================
# Review-window timestamp parsing (shared with the export script)
# ============================================================================

# docker logs accepts Go duration strings ("2h", "1h30m", "90m", "10s", "1d").
_DOCKER_RELATIVE_RE = re.compile(
    r"^(?P<days>\d+d)?(?P<hours>\d+h)?(?P<minutes>\d+m)?(?P<seconds>\d+s)?$"
)
_RFC3339_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})"
    r"(?:\.\d+)?(?P<tz>Z|[+-]\d{2}:?\d{2})?$"
)


def _local_tz(now: datetime | None = None):
    """Resolve the local timezone as a DST-aware ``ZoneInfo`` (so winter/summer
    offsets follow the target timestamp's date rather than ``datetime.now()``'s
    fixed offset; P0 fix2 C). Falls back to ``None`` (which tells
    ``aware.astimezone(None)`` to query the OS localtime table for the target
    instant)."""
    if now is not None and now.tzinfo is not None:
        return now.tzinfo
    tz_env = os.environ.get("TZ", "").strip()
    if tz_env:
        try:
            return ZoneInfo(tz_env.lstrip(":"))
        except Exception:
            pass
    try:
        localtime_path = Path("/etc/localtime")
        if localtime_path.exists():
            resolved = str(localtime_path.resolve())
            if "zoneinfo/" in resolved:
                return ZoneInfo(resolved.split("zoneinfo/", 1)[1])
    except Exception:
        pass
    return None


def _to_local_naive(aware: datetime, *, now: datetime | None = None) -> datetime:
    tz = _local_tz(now)
    return aware.astimezone(tz).replace(tzinfo=None)


def parse_window_timestamp(raw: str, *, now: datetime | None = None) -> datetime:
    """Parse a ``--since``/``--until`` value for line filtering.

    Accepts RFC3339 (``2026-10-27T23:00:00Z``, ``+08:00`` offsets), plain
    ``YYYY-MM-DD[ HH:MM[:SS]]``, and docker's relative syntax (``2h``, ``1d30m``
    — resolved against ``now``, like ``docker logs --since``). Returns a naive
    local datetime (both bridges' log lines carry naive local timestamps).
    Raises ValueError for anything else — the export script must fail loudly
    rather than silently export everything."""
    text = raw.strip()
    if not text:
        raise ValueError("empty timestamp")

    match = _DOCKER_RELATIVE_RE.match(text)
    if match is not None and any(
        match.group(g) for g in ("days", "hours", "minutes", "seconds")
    ):
        delta = timedelta(
            days=int((match.group("days") or "0d")[:-1]),
            hours=int((match.group("hours") or "0h")[:-1]),
            minutes=int((match.group("minutes") or "0m")[:-1]),
            seconds=int((match.group("seconds") or "0s")[:-1]),
        )
        # Subtract in UTC from an aware "now", then read the instant on the local
        # clock by that date's own rules, so a window crossing a DST change is
        # neither an hour long nor short.
        if now is None:
            aware_now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            aware_now = now.replace(tzinfo=_local_tz(now))
        else:
            aware_now = now
        return _to_local_naive(aware_now.astimezone(timezone.utc) - delta, now=now)

    rfc = _RFC3339_RE.match(text)
    if rfc is not None:
        naive = datetime.strptime(
            f"{rfc.group(1)} {rfc.group(2)}", "%Y-%m-%d %H:%M:%S"
        )
        tz = rfc.group("tz")
        if tz is None:
            return naive
        offset = _tz_offset(tz)
        aware = naive.replace(tzinfo=timezone(offset))
        return _to_local_naive(aware, now=now)

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognized timestamp {raw!r}")


def _tz_offset(tz: str) -> timedelta:
    if tz == "Z":
        return timedelta(0)
    sign = 1 if tz[0] == "+" else -1
    body = tz[1:].replace(":", "")
    hours = int(body[:2])
    minutes = int(body[2:])
    if hours > 23 or minutes > 59:
        raise ValueError(f"invalid timezone offset {tz!r}")
    return sign * timedelta(hours=hours, minutes=minutes)


# ``2026-10-27 23:00:01,123`` (Python logging default asctime) or RFC3339
# ``2026-10-27T23:00:01.123456789Z`` (docker logs --timestamps).
_LINE_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:[,.]\d+)?(?P<tz>Z|[+-]\d{2}:?\d{2})?"
)


def _parse_line_timestamp(line: str) -> datetime | None:
    match = _LINE_TIMESTAMP_RE.match(line)
    if match is None:
        return None
    try:
        naive = datetime.strptime(
            f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H:%M:%S"
        )
        tz = match.group("tz")
        if tz is not None:
            aware = naive.replace(tzinfo=timezone(_tz_offset(tz)))
            return _to_local_naive(aware)
        return naive
    except ValueError:
        return None


def redact_lines(
    lines: list[str],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    sort_by_timestamp: bool = False,
) -> list[str]:
    """Redact every line; with a window, keep only timestamped lines inside it
    (timestamp-less lines stay attached to their preceding entry so stack traces
    are not silently dropped or scrambled when ``sort_by_timestamp=True``)."""
    if sort_by_timestamp:
        groups: list[tuple[datetime | None, int, list[str]]] = []
        for idx, line in enumerate(lines):
            stamp = _parse_line_timestamp(line)
            if stamp is not None or not groups:
                groups.append((stamp, idx, [line]))
            else:
                groups[-1][2].append(line)
        groups.sort(key=lambda g: (g[0] is not None, g[0] or datetime.min, g[1]))
        lines = [line for _, _, grp in groups for line in grp]

    kept: list[str] = []
    for line in lines:
        if since is not None or until is not None:
            stamp = _parse_line_timestamp(line)
            if stamp is not None:
                if since is not None and stamp < since:
                    continue
                if until is not None and stamp > until:
                    continue
        kept.append(redact_text(line))
    return kept


def _parse_window_arg(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    try:
        return parse_window_timestamp(raw)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("infile", nargs="?", help="log file to redact ('-' for stdin)")
    parser.add_argument("outfile", nargs="?", help="where to write the redacted copy ('-' for stdout)")
    parser.add_argument("--since", help="keep lines timestamped at/after this")
    parser.add_argument("--until", help="keep lines timestamped at/before this")
    parser.add_argument(
        "--sort",
        action="store_true",
        help="stably sort timestamped entries (keeping continuation lines attached)",
    )
    parser.add_argument(
        "--normalize-window",
        metavar="TS",
        help="print TS normalized to 'YYYY-MM-DD HH:MM:SS' (for the shell script) and exit",
    )
    args = parser.parse_args(argv)

    if args.normalize_window is not None:
        try:
            normalized = parse_window_timestamp(args.normalize_window)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(normalized.strftime("%Y-%m-%d %H:%M:%S"))
        return 0

    if not args.infile or not args.outfile:
        parser.error("infile and outfile are required without --normalize-window")

    since = _parse_window_arg(args.since)
    until = _parse_window_arg(args.until)

    if args.infile == "-":
        text = sys.stdin.read()
    else:
        text = Path(args.infile).read_text(encoding="utf-8", errors="replace")
    redacted = redact_lines(
        text.splitlines(keepends=False),
        since=since,
        until=until,
        sort_by_timestamp=args.sort,
    )
    payload = "\n".join(redacted) + ("\n" if redacted else "")
    if args.outfile == "-":
        sys.stdout.write(payload)
    else:
        Path(args.outfile).write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
