#!/usr/bin/env python3
"""Entry point for Dalton's OpenClaw conversation archive (`run` and `backfill`).

Subcommands:
- `run`: Daily incremental extraction and per-company/Home Markdown digest rendering.
- `backfill --since YYYY-MM-DD`: Historical backfill in 7-day weekly batches (Phase 1 default:
  `today_et - 14 days`), paced by an hourly token budget (`HourlyTokenBudgetLimiter`), with
  unbounded time-based exponential backoff on 429 / quota exhaustion (never drops a segment
  by retry count), interruptible and resumable via session cursors and `content_hash`.
- `--dry-run`: Run export, segmentation, and extraction/stats without writing to Postgres or workspaces.

Exit codes:
- `0`: All segments completed.
- `2`: Another archive process currently holds the Postgres advisory lock (`ArchiveAlreadyRunningError`).
- `3`: One or more segments remained incomplete (still `in_flight` after deferred retries, a
  transient network failure after retries, or a non-retryable extraction error such as invalid
  JSON); safe to re-run because the prefix commit cursor was not advanced past incomplete segments.
- Other non-zero (e.g. `1`): Fatal error (such as `PermanentQuotaError`).
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import inspect
import json
import logging
import os
import re
import resource
import signal
import sys
import threading
import time
import tracemalloc
import uuid
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Coroutine
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.conversations.export_openclaw import (  # noqa: E402
    DEFAULT_AGENTS,
    DEFAULT_CURSORS_FILE,
    DEFAULT_EXPORT_CURSORS_FILE,
    DEFAULT_INBOX_DIR,
    DEFAULT_OCW_AGENTS_ROOT,
    SessionCursor,
    is_operational_or_chitchat,
    run_export,
    save_cursors_file,
)
from src.config.env import LOCAL_DEV_USER_ID  # noqa: E402
from src.dalton.conversations.embedder import embed_texts_via_bridge  # noqa: E402
from src.dalton.conversations.extractor import (  # noqa: E402
    DEFAULT_SEGMENT_DEADLINE_S,
    SegmentExtractionOutcome,
    deduplicate_candidate_cards,
    expand_event_chunks,
    extract_segment_cards,
    is_fallback_eligible,
    is_in_flight_error,
    is_permanent_quota_error,
    is_rate_limit_or_quota_error,
    is_transient_network_error,
    resolve_dedup_and_supersedes,
    segment_events,
)
from src.dalton.conversations.render import (  # noqa: E402
    archive_owner_user_id,
    render_and_write_conversation_digests,
)
from src.dalton.database import conversations as db_conv  # noqa: E402
from src.dalton.llm.complete import resolve_model_billing_and_provider  # noqa: E402
from src.dalton.llm.model_prefs import resolve_dalton_extract_model  # noqa: E402
from src.dalton.settings import get_dalton_settings  # noqa: E402
from src.server.database import pool as _db_pool_mod  # noqa: E402
from src.server.database.pool import get_or_create_pool, open_session_connection  # noqa: E402

_orig_open_session_connection = open_session_connection

logger = logging.getLogger("dalton.conversation_archive")

ET_TZ = ZoneInfo("America/New_York")
DEFAULT_FALLBACK_MODEL = "gemini-4-argon-medium-oauth"
DEFAULT_RESERVED_OUTPUT_TOKENS = 1024
DEFAULT_STALE_AFTER_S = 900
TRANSIENT_BACKOFF_DELAYS_S: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 32.0)
DEFAULT_DEFER_RETRY_MIN_S = 30.0
DEFAULT_DEFER_RETRY_MAX_S = 60.0
DEFAULT_DEFER_EXTRA_AFTER_STALE_S = 60.0
CANCEL_GRACE_TIMEOUT_S = 5.0
POOL_CLOSE_TIMEOUT_S = 10.0
SHUTDOWN_GRACE_TIMEOUT_S = 30.0
EXIT_CODE_OK = 0
EXIT_CODE_FATAL = 1

_ACTIVE_SEGMENT_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "_ACTIVE_SEGMENT_DEADLINE", default=None
)
_ABANDONED_EXTRACTION_TASKS: dict[asyncio.Task[Any], str] = {}


def _register_abandoned_task(task: asyncio.Task[Any], segment_id: str) -> None:
    _ABANDONED_EXTRACTION_TASKS[task] = segment_id

    def _on_done(t: asyncio.Task[Any]) -> None:
        _ABANDONED_EXTRACTION_TASKS.pop(t, None)
        if not t.cancelled():
            _ = t.exception()

    task.add_done_callback(_on_done)


def _get_live_abandoned_segment_ids() -> list[str]:
    for t in [task for task in list(_ABANDONED_EXTRACTION_TASKS) if task.done()]:
        _ABANDONED_EXTRACTION_TASKS.pop(t, None)
    return list(_ABANDONED_EXTRACTION_TASKS.values())


EXIT_CODE_LOCK_HELD = 2
EXIT_CODE_INCOMPLETE = 3
MEM_TRACE_ENV_VAR = "DALTON_ARCHIVE_MEM_TRACE"
MEM_TRACE_SEGMENT_INTERVAL = 20


def _is_mem_trace_enabled() -> bool:
    return os.environ.get(MEM_TRACE_ENV_VAR, "").strip() == "1"


def _maybe_start_tracemalloc() -> bool:
    if not _is_mem_trace_enabled():
        return False
    if not tracemalloc.is_tracing():
        tracemalloc.start(25)
    return True


def _log_tracemalloc_top10(segments_seen: int) -> str | None:
    """When `DALTON_ARCHIVE_MEM_TRACE=1`, log top 10 `tracemalloc` allocation sites (`file:line` and size)."""
    if not _is_mem_trace_enabled():
        return None
    if not tracemalloc.is_tracing():
        tracemalloc.start(25)
    snapshot = tracemalloc.take_snapshot()
    top_stats = snapshot.statistics("lineno")[:10]
    entries: list[str] = []
    for idx, stat in enumerate(top_stats, start=1):
        frame = stat.traceback[0] if stat.traceback else None
        loc = f"{frame.filename}:{frame.lineno}" if frame else "<unknown>:0"
        size_kib = stat.size / 1024.0
        entries.append(f"#{idx} {loc} size={stat.size}B ({size_kib:.1f}KiB, count={stat.count})")
    cur_bytes, peak_bytes = tracemalloc.get_traced_memory()
    msg = (
        f"[MEM_TRACE segments={segments_seen} current={cur_bytes}B peak={peak_bytes}B] "
        + "; ".join(entries)
    )
    logger.info("%s", msg)
    return msg


class _UnbufferedStderrHandler(logging.StreamHandler):
    """StreamHandler writing to `sys.stderr` and flushing after every record."""

    def __init__(self, stream: Any = None) -> None:
        self._explicit_stream = stream
        super().__init__(stream if stream is not None else sys.stderr)

    def emit(self, record: logging.LogRecord) -> None:
        if self._explicit_stream is None:
            self.stream = sys.stderr
        super().emit(record)
        self.flush()

    def flush(self) -> None:
        if self._explicit_stream is None:
            self.stream = sys.stderr
        try:
            super().flush()
        except Exception:
            pass


def configure_archive_logging() -> logging.Handler:
    """Configure root and `dalton.conversation_archive` loggers to write unbuffered to `sys.stderr`."""
    try:
        reconf = getattr(sys.stderr, "reconfigure", None)
        if callable(reconf):
            reconf(line_buffering=True, write_through=True)
    except Exception:
        pass
    handler = _UnbufferedStderrHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] conversation_archive: %(message)s")
    )
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler],
        force=True,
    )
    logger.setLevel(logging.INFO)
    return handler


class ArchiveAlreadyRunningError(RuntimeError):
    """Raised when another conversation archive run already holds the Postgres advisory lock."""

    def __init__(self, message: str = "已有归档在运行", *, code: int = 2) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.message} (exit_code={self.code})"


class SegmentExtractionError(RuntimeError):
    """Raised when a conversation segment fails extraction (non-429 error)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        if status_code is None:
            m = re.search(r"(?:http_|status[=_:\s]*)?([45]\d{2})\b", str(message or ""), re.IGNORECASE)
            if m:
                status_code = int(m.group(1))
        self.status_code = status_code


class PermanentQuotaError(RuntimeError):
    """Raised when a permanent quota or billing limit is hit, halting the batch."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        if status_code is None:
            m = re.search(r"(?:http_|status[=_:\s]*)?([45]\d{2})\b", str(message or ""), re.IGNORECASE)
            if m:
                status_code = int(m.group(1))
        self.status_code = status_code


def format_safe_run_error(exc: BaseException) -> str:
    """Format exception for `dalton_conversation_runs.error_text` using ONLY class name and status code."""
    cls_name = type(exc).__name__
    status_code = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if isinstance(status_code, int):
        return f"{cls_name}(status_code={status_code})"
    return cls_name


def estimate_segment_tokens(
    segment: list[dict[str, Any]],
    *,
    reserved_output_tokens: int = DEFAULT_RESERVED_OUTPUT_TOKENS,
    hourly_budget: int | None = None,
) -> int:
    """Estimate input + reserved output tokens for a segment before calling the LLM."""
    input_chars = sum(len(str(e.get("text") or "")) for e in segment)
    input_est = max(200, input_chars // 2)
    out_reserve = max(64, int(reserved_output_tokens))
    if hourly_budget is not None and hourly_budget > 0:
        out_reserve = min(out_reserve, max(64, int(hourly_budget) // 4))
    return input_est + out_reserve


class BudgetReservation:
    """Handle for an in-flight token budget reservation, settled at most once by ID."""

    __slots__ = (
        "reservation_id",
        "estimated_tokens",
        "waited_seconds",
        "settled",
        "settled_input_tokens",
        "settled_output_tokens",
        "settled_total_tokens",
    )

    def __init__(
        self,
        reservation_id: int,
        estimated_tokens: int,
        waited_seconds: float = 0.0,
    ) -> None:
        self.reservation_id = int(reservation_id)
        self.estimated_tokens = int(estimated_tokens)
        self.waited_seconds = float(waited_seconds)
        self.settled = False
        self.settled_input_tokens = 0
        self.settled_output_tokens = 0
        self.settled_total_tokens = 0

    def rebind(self, other: "BudgetReservation | Any") -> None:
        """Adopt a replacement reservation when re-reserving before a 429 retry attempt."""
        if isinstance(other, BudgetReservation):
            self.reservation_id = other.reservation_id
            self.estimated_tokens = other.estimated_tokens
            self.waited_seconds += other.waited_seconds
            self.settled = other.settled
        elif type(other) is int:
            self.reservation_id = other
            self.settled = False


def _extract_reservation_id(reservation: Any) -> int | None:
    if isinstance(reservation, BudgetReservation):
        return reservation.reservation_id
    if type(reservation) is int:
        return reservation
    return None


class HourlyTokenBudgetLimiter:
    """Concurrency-safe sliding 3600-second token budget limiter with ID-keyed pre-reservation and shared 429 pause."""

    def __init__(
        self,
        hourly_budget: int,
        *,
        window_seconds: float = 3600.0,
        clock_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
    ) -> None:
        self.hourly_budget = max(1, int(hourly_budget))
        self.window_seconds = float(window_seconds)
        self._clock = clock_fn
        self._uses_custom_clock = clock_fn is not time.monotonic
        self._sleep = sleep_fn
        self._history: deque[tuple[float, int]] = deque()
        self._reservations: deque[tuple[int, float, int]] = deque()
        self._next_reservation_id: int = 0
        self._settled_reservation_ids: set[int] = set()
        self._lock = asyncio.Lock()
        self._reservation_event = asyncio.Event()
        self._rate_limit_pause_until: float = 0.0
        self._rate_limit_clear_event = asyncio.Event()
        self._rate_limit_clear_event.set()
        self._virtual_time_offset: float = 0.0
        self.total_slept_seconds: float = 0.0

    def _now(self) -> float:
        if self._uses_custom_clock:
            return self._clock()
        return self._clock() + self._virtual_time_offset

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._history and self._history[0][0] <= cutoff:
            self._history.popleft()
        while self._reservations and self._reservations[0][1] <= cutoff:
            self._reservations.popleft()

    def current_window_tokens(self) -> int:
        now = self._now()
        self._prune(now)
        return sum(tok for _, tok in self._history) + sum(tok for _, _, tok in self._reservations)

    def notify_rate_limit_backoff(self, delay_seconds: float) -> float:
        """Pause all concurrent coroutines until at least `now + delay_seconds` when any coroutine hits 429."""
        now = self._now()
        until_ts = now + max(0.0, float(delay_seconds))
        if until_ts > self._rate_limit_pause_until:
            self._rate_limit_pause_until = until_ts
        if self._rate_limit_pause_until > now:
            self._rate_limit_clear_event.clear()
        return until_ts

    def clear_rate_limit_backoff(self, until_ts: float | None = None) -> bool:
        """Clear shared 429 pause only when the monotonic clock has reached `_rate_limit_pause_until`."""
        now = self._now()
        if self._rate_limit_pause_until <= 0.0:
            self._rate_limit_clear_event.set()
            return True
        if now + 1e-6 >= self._rate_limit_pause_until:
            self._rate_limit_pause_until = 0.0
            self._rate_limit_clear_event.set()
            self._reservation_event.set()
            return True
        return False

    async def wait_for_rate_limit_pause(self, *, deadline: float | None = None) -> float:
        """Wait if another coroutine is currently in a 429 rate-limit backoff window."""
        if self._rate_limit_pause_until <= 0.0:
            return 0.0
        eff_dl = deadline if deadline is not None else _ACTIVE_SEGMENT_DEADLINE.get()
        slept = 0.0
        while True:
            async with self._lock:
                now = self._now()
                target_until = self._rate_limit_pause_until
                if target_until <= now + 1e-6:
                    if self._rate_limit_pause_until > 0.0:
                        self._rate_limit_pause_until = 0.0
                        self._rate_limit_clear_event.set()
                    break
                raw_wait_s = max(0.01, target_until - now)
                if eff_dl is not None:
                    rem_s = eff_dl - now
                    if rem_s <= 0:
                        raise _SegmentDeadlineExceededError()
                    wait_s = min(raw_wait_s, rem_s)
                    clamped_to_dl = raw_wait_s >= rem_s - 1e-9
                else:
                    wait_s = raw_wait_s
                    clamped_to_dl = False
                t1 = self._clock()

            sleep_task = asyncio.create_task(self._sleep(wait_s))
            clear_task = asyncio.create_task(self._rate_limit_clear_event.wait())
            try:
                done, pending = await asyncio.wait(
                    {sleep_task, clear_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for p in pending:
                    p.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            finally:
                for t in (sleep_task, clear_task):
                    if not t.done():
                        t.cancel()

            async with self._lock:
                t2 = self._clock()
                actual_elapsed = max(0.0, t2 - t1)
                if sleep_task in done:
                    if not self._uses_custom_clock and actual_elapsed < wait_s * 0.5:
                        self._virtual_time_offset += wait_s - actual_elapsed
                    slept += wait_s
                    self.total_slept_seconds += wait_s
                else:
                    slept += actual_elapsed
                    self.total_slept_seconds += actual_elapsed
                if eff_dl is not None and (
                    self._now() >= eff_dl or (clamped_to_dl and sleep_task in done)
                ):
                    raise _SegmentDeadlineExceededError()
        return slept

    async def wait_for_budget(
        self,
        estimated_tokens: int = 1000,
        *,
        deadline: float | None = None,
    ) -> BudgetReservation:
        """Wait until the sliding 1-hour window has capacity for `estimated_tokens` and pre-reserve it by ID.

        Computes required wait inside `self._lock` and sleeps outside `self._lock` for at most
        `min(wait_needed, remaining_segment_time)`.
        Raises `PermanentQuotaError` if a single request's `estimated_tokens` exceeds `hourly_budget`.
        When the required budget wait (`raw_wait_s`) exceeds the segment's remaining deadline (`rem_s`),
        immediately raises `_SegmentDeadlineExceededError` to mark the segment incomplete without
        sleeping until the deadline first.
        """
        eff_dl = deadline if deadline is not None else _ACTIVE_SEGMENT_DEADLINE.get()
        slept = 0.0
        est = max(1, int(estimated_tokens))
        if est > self.hourly_budget:
            raise PermanentQuotaError(
                f"Estimated request tokens ({est}) exceed hourly_budget ({self.hourly_budget})"
            )
        while True:
            slept += await self.wait_for_rate_limit_pause(deadline=eff_dl)
            async with self._lock:
                now = self._now()
                if eff_dl is not None and now >= eff_dl:
                    raise _SegmentDeadlineExceededError()
                if self._rate_limit_pause_until > now + 1e-6:
                    continue
                self._prune(now)
                used_settled = sum(tok for _, tok in self._history)
                used_reserved = sum(tok for _, _, tok in self._reservations)
                used = used_settled + used_reserved
                if used + est <= self.hourly_budget:
                    self._next_reservation_id += 1
                    res_id = self._next_reservation_id
                    self._reservations.append((res_id, now, est))
                    return BudgetReservation(
                        reservation_id=res_id,
                        estimated_tokens=est,
                        waited_seconds=slept,
                    )

                combined = sorted(
                    [*self._history, *((ts, tok) for _, ts, tok in self._reservations)],
                    key=lambda item: item[0],
                )
                needed_to_free = (used + est) - self.hourly_budget
                freed = 0
                target_ts = combined[0][0] if combined else now
                for ts, tok in combined:
                    freed += tok
                    target_ts = ts
                    if freed >= needed_to_free:
                        break
                raw_wait_s = max(0.05, (target_ts + self.window_seconds) - now)
                wait_on_reservation = bool(
                    self._reservations and used_settled + est <= self.hourly_budget
                )
                if eff_dl is not None:
                    rem_s = eff_dl - now
                    # Fail fast when the required budget recovery wait exceeds the segment's remaining
                    # deadline: mark the segment incomplete immediately rather than sleeping to `eff_dl`.
                    if rem_s <= 0 or (not wait_on_reservation and raw_wait_s > rem_s):
                        logger.warning(
                            "Hourly token budget wait (%.1fs) exceeds remaining segment deadline (%.1fs); aborting segment",
                            raw_wait_s,
                            max(0.0, rem_s),
                        )
                        _flush_log_handlers()
                        raise _SegmentDeadlineExceededError()
                    wait_s = min(raw_wait_s, rem_s)
                    clamped_to_dl = raw_wait_s >= rem_s - 1e-9
                else:
                    wait_s = raw_wait_s
                    clamped_to_dl = False
                if wait_on_reservation:
                    self._reservation_event.clear()
                logger.info(
                    "Hourly token budget reached (used=%d, reserved=%d, est=%d, budget=%d); sleeping %.1fs",
                    used_settled,
                    used_reserved,
                    est,
                    self.hourly_budget,
                    wait_s,
                )
                _flush_log_handlers()
                t1 = self._clock()

            if wait_on_reservation:
                sleep_task = asyncio.create_task(self._sleep(wait_s))
                event_task = asyncio.create_task(self._reservation_event.wait())
                try:
                    done, pending = await asyncio.wait(
                        {sleep_task, event_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for p in pending:
                        p.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                finally:
                    for t in (sleep_task, event_task):
                        if not t.done():
                            t.cancel()
                sleep_completed = sleep_task in done
            else:
                await self._sleep(wait_s)
                sleep_completed = True

            async with self._lock:
                t2 = self._clock()
                actual_elapsed = max(0.0, t2 - t1)
                if sleep_completed:
                    if not self._uses_custom_clock and actual_elapsed < wait_s * 0.5:
                        self._virtual_time_offset += wait_s - actual_elapsed
                    slept += wait_s
                    self.total_slept_seconds += wait_s
                else:
                    slept += actual_elapsed
                    self.total_slept_seconds += actual_elapsed
                if eff_dl is not None and (
                    self._now() >= eff_dl or (clamped_to_dl and sleep_completed)
                ):
                    raise _SegmentDeadlineExceededError()

    def release_reservation(self, reservation: BudgetReservation | int | None = None) -> int:
        """Release a pre-reserved token estimate by reservation ID without recording actual usage."""
        if isinstance(reservation, BudgetReservation) and reservation.settled:
            return 0
        res_id = _extract_reservation_id(reservation)
        if res_id is None or res_id in self._settled_reservation_ids:
            if isinstance(reservation, BudgetReservation):
                reservation.settled = True
            return 0
        for idx, (rid, _ts, tok) in enumerate(self._reservations):
            if rid == res_id:
                del self._reservations[idx]
                self._settled_reservation_ids.add(res_id)
                if isinstance(reservation, BudgetReservation):
                    reservation.settled = True
                self._reservation_event.set()
                return tok
        return 0

    def record_tokens(
        self,
        tokens: int,
        *,
        reservation: BudgetReservation | int | None = None,
    ) -> None:
        """Reconcile a pre-reserved estimate by reservation ID with actual tokens consumed."""
        if isinstance(reservation, BudgetReservation) and reservation.settled:
            return
        res_id = _extract_reservation_id(reservation)
        if res_id is not None and res_id in self._settled_reservation_ids:
            if isinstance(reservation, BudgetReservation):
                reservation.settled = True
            return
        tok = max(0, int(tokens))
        now = self._now()
        self._prune(now)
        popped_reserved = 0
        if res_id is not None:
            for idx, (rid, _ts, r_tok) in enumerate(self._reservations):
                if rid == res_id:
                    popped_reserved = r_tok
                    del self._reservations[idx]
                    self._settled_reservation_ids.add(res_id)
                    break
        if isinstance(reservation, BudgetReservation):
            reservation.settled = True
        if tok > 0:
            self._history.append((now, tok))
        if tok < popped_reserved:
            self._reservation_event.set()


def iter_weekly_windows(since_date: date, until_date: date) -> list[tuple[date, date]]:
    """Split `[since_date, until_date]` into 7-day weekly `(window_start, window_end)` intervals."""
    if since_date > until_date:
        return [(until_date, until_date)]
    windows: list[tuple[date, date]] = []
    cur = since_date
    while cur <= until_date:
        w_end = min(until_date, cur + timedelta(days=6))
        windows.append((cur, w_end))
        cur = w_end + timedelta(days=1)
    return windows


def group_events_by_weekly_window(
    events: list[dict[str, Any]],
    *,
    since_date: date,
    until_date: date,
) -> list[tuple[date, date, list[dict[str, Any]]]]:
    """Partition exported events into chronological 7-day weekly batches.

    Expands `[since_date, until_date]` if `events` contain earlier historical dates
    (e.g. from a full-session rescan triggered by a generation watermark change).
    """
    if not events:
        return []
    event_dates = [
        date.fromisoformat(str(ev.get("date_et") or since_date.isoformat())[:10])
        for ev in events
    ]
    eff_start = min([since_date, *event_dates])
    eff_end = max([until_date, *event_dates])
    windows = iter_weekly_windows(eff_start, eff_end)
    batches: list[tuple[date, date, list[dict[str, Any]]]] = []
    for w_start, w_end in windows:
        w_events = [
            ev
            for ev in events
            if w_start <= date.fromisoformat(str(ev.get("date_et") or eff_start.isoformat())[:10]) <= w_end
        ]
        if w_events:
            batches.append((w_start, w_end, w_events))
    return batches


def archive_billing_user_id() -> str:
    """The account the archive's extraction calls are billed to: the first PM user.

    The archive is a system job over the owner's OpenClaw history; with Supabase
    sign-in on, DALTON_PM_USER_IDS names the owner's account.
    """
    return archive_owner_user_id()


def _did_journal_short_circuit_without_send(outcome: SegmentExtractionOutcome) -> bool:
    """Return True when the journal returned early or replayed a cached result without sending a new upstream LLM request."""
    if bool(getattr(outcome, "cached", False) or getattr(outcome, "replayed", False)):
        return True
    if outcome.total_tokens > 0 or outcome.input_tokens > 0 or outcome.output_tokens > 0:
        return False
    ek = str(outcome.error_kind or "").strip().lower()
    if ek in ("deadline", "deadline_exceeded"):
        return False
    if is_in_flight_error(outcome):
        return True
    status = str(outcome.status or "").lower()
    err = str(outcome.error or "")
    if status in ("in_flight", "conflict", "budget_exhausted") or ek in (
        "in_flight",
        "indeterminate",
        "conflict",
        "pre_send",
        "budget_exhausted",
        "budget_db_error",
    ):
        return True
    if (
        "正在由另一个进程执行中" in err
        or "上次调用在返回前中断" in err
        or "Journal state is " in err
        or "collided with mismatched request_hash" in err
        or "before the request was sent" in err
        or "未发起实际请求" in err
    ):
        return True
    return False


def _extraction_error_reason(outcome: SegmentExtractionOutcome) -> str:
    """Format `extraction_error:<异常类型>` for non-transient segment extraction failures."""
    err_text = str(outcome.error or "").strip()
    ek = str(outcome.error_kind or "").strip()
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:", err_text)
    prefix = m.group(1) if m else ""
    if prefix and (prefix.endswith(("Error", "Exception")) or not ek):
        exc_type = prefix
    elif ek:
        exc_type = ek
    elif prefix:
        exc_type = prefix
    else:
        exc_type = "SegmentExtractionError"
    return f"extraction_error:{exc_type}"


class _SegmentDeadlineExceededError(Exception):
    """Internal sentinel raised when a segment extraction exceeds `segment_deadline_s`."""

    def __init__(
        self,
        *,
        short_circuit: bool = False,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        super().__init__("segment_deadline_exceeded")
        self.short_circuit = bool(short_circuit)
        self.input_tokens = int(input_tokens)
        self.output_tokens = int(output_tokens)
        self.total_tokens = int(total_tokens)


def _format_segment_id(segment: list[dict[str, Any]]) -> str:
    """Format a compact human-readable segment identifier (`<session_id>:<first_event_id>[..<last_event_id>]`)."""
    if not segment:
        return "empty_segment"
    first_ev = segment[0] if isinstance(segment[0], dict) else {}
    last_ev = segment[-1] if isinstance(segment[-1], dict) else {}
    first_id = str(first_ev.get("event_id") or f"seq:{first_ev.get('seq', 0)}")
    last_id = str(last_ev.get("event_id") or f"seq:{last_ev.get('seq', 0)}")
    sid = str(first_ev.get("session_id") or "")
    base = first_id if first_id == last_id else f"{first_id}..{last_id}"
    return f"{sid}:{base}" if sid else base


async def extract_segment_with_infinite_backoff(
    segment: list[dict[str, Any]],
    *,
    extractor_model: str,
    prompt_version: str,
    extractor_fallback_model: str | None = None,
    segment_deadline_s: float | None = None,
    seg_deadline: float | None = None,
    user_id: str = LOCAL_DEV_USER_ID,
    llm_service: Any = None,
    initial_backoff_s: float = 30.0,
    max_backoff_s: float = 900.0,
    limiter: HourlyTokenBudgetLimiter | None = None,
    estimated_tokens: int = 1000,
    reservation: BudgetReservation | int | None = None,
    deadline: float | None = None,
    clock_fn: Callable[[], float] | None = None,
    dry_run: bool = False,
    extract_fn: Callable[..., Coroutine[Any, Any, SegmentExtractionOutcome]] = extract_segment_cards,
    sleep_fn: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
    on_deadline_extended: Callable[[float], None] | None = None,
) -> tuple[SegmentExtractionOutcome, int]:
    """Extract cards from `segment`, backing off indefinitely on 429, retrying once with `extractor_fallback_model`
    when a platform-key model fails with timeout/indeterminate/transient error, and retrying transient network/5xx
    errors up to 5 times (2->4->8->16->32s) when the primary model is OAuth.

    Enforces a single absolute deadline (`min(seg_deadline, deadline)`) across primary calls, fallback calls,
    budget waits, 429/transient backoff sleeps, and deferred retries.
    Only passes `resend_indeterminate=True` when the active model's `billing_type == "oauth"`.
    Settles each non-short-circuit attempt independently once, recording the exact same token count into both
    `limiter` and the segment/run token totals (`actual` if returned, otherwise `estimated_tokens`).
    Returns `(outcome, retries)`.
    """
    rl_attempt = 0
    transient_attempt = 0
    acc_input_tokens = 0
    acc_output_tokens = 0
    acc_total_tokens = 0
    needs_budget_recheck = False
    init_s = max(0.01, float(initial_backoff_s))
    cap_s = max(init_s, float(max_backoff_s))
    billing_type, _ = resolve_model_billing_and_provider(extractor_model)
    eff_resend_indeterminate = billing_type == "oauth"
    reextract_delegated_fallback = extractor_fallback_model is None and dry_run
    cfg_conv = get_dalton_settings().conversations
    if extractor_fallback_model is None:
        eff_fallback_model = (
            ""
            if reextract_delegated_fallback
            else str(
                getattr(
                    cfg_conv,
                    "extractor_fallback_model",
                    DEFAULT_FALLBACK_MODEL,
                )
                or ""
            ).strip()
        )
    else:
        eff_fallback_model = str(extractor_fallback_model).strip()
    sig = inspect.signature(extract_fn)
    supports_persist_raw = "persist_raw" in sig.parameters
    supports_skip_journal = "skip_journal" in sig.parameters
    supports_deadline = "deadline" in sig.parameters
    orig_reservation: BudgetReservation | None = (
        reservation if isinstance(reservation, BudgetReservation) else None
    )
    current_reservation: BudgetReservation | Any = (
        reservation
        if isinstance(reservation, BudgetReservation)
        else (BudgetReservation(reservation, estimated_tokens) if type(reservation) is int else None)
    )

    def _now_mono() -> float:
        if clock_fn is not None:
            return clock_fn()
        if type(limiter) is HourlyTokenBudgetLimiter:
            return limiter._now()
        return time.monotonic()

    if segment_deadline_s is not None and float(segment_deadline_s) > 0:
        eff_seg_deadline_s = float(segment_deadline_s)
    else:
        eff_seg_deadline_s = float(
            getattr(cfg_conv, "segment_deadline_s", DEFAULT_SEGMENT_DEADLINE_S)
            or DEFAULT_SEGMENT_DEADLINE_S
        )
    if seg_deadline is None:
        seg_deadline = _now_mono() + eff_seg_deadline_s
    eff_deadline: float = (
        min(seg_deadline, deadline) if deadline is not None else seg_deadline
    )

    async def _budget_wait_excluded() -> Any:
        # Budget waits are deliberate throttling, not a stall: they run with no
        # segment deadline, and the deadline moves forward by the time waited.
        nonlocal seg_deadline, eff_deadline
        assert limiter is not None
        # Exempt from the segment deadline only; a deferred attempt's window
        # (`deadline`) still bounds the wait, so it ends as soon as the window
        # cannot be met instead of sleeping past it.
        no_dl_token = _ACTIVE_SEGMENT_DEADLINE.set(deadline)
        wait_t0 = _now_mono()
        try:
            res = await limiter.wait_for_budget(estimated_tokens)
        finally:
            _ACTIVE_SEGMENT_DEADLINE.reset(no_dl_token)
        waited_s = max(0.0, _now_mono() - wait_t0)
        if waited_s > 0:
            # Only the segment's own deadline moves; the deferred in-flight
            # window (`deadline`) deliberately includes budget and call time.
            seg_deadline += waited_s
            eff_deadline = min(seg_deadline, deadline) if deadline is not None else seg_deadline
            _ACTIVE_SEGMENT_DEADLINE.set(eff_deadline)
            if on_deadline_extended is not None:
                on_deadline_extended(waited_s)
        return res

    def _release_current_reservation() -> None:
        nonlocal current_reservation
        if limiter is not None and current_reservation is not None:
            limiter.release_reservation(current_reservation)
        current_reservation = None

    def _settle_attempt(
        short_circuit: bool,
        in_tok: int = 0,
        out_tok: int = 0,
        tot_tok: int = 0,
    ) -> None:
        nonlocal acc_input_tokens, acc_output_tokens, acc_total_tokens, current_reservation
        if short_circuit:
            _release_current_reservation()
            return
        eff_in = max(0, int(in_tok))
        eff_out = max(0, int(out_tok))
        actual_tot = max(0, int(tot_tok or (eff_in + eff_out)))
        settled = actual_tot if actual_tot > 0 else max(0, int(estimated_tokens))
        if actual_tot <= 0:
            eff_in = 0
            eff_out = 0
        acc_input_tokens += eff_in
        acc_output_tokens += eff_out
        acc_total_tokens += settled
        if orig_reservation is not None:
            orig_reservation.settled_input_tokens = acc_input_tokens
            orig_reservation.settled_output_tokens = acc_output_tokens
            orig_reservation.settled_total_tokens = acc_total_tokens
        if limiter is not None:
            if current_reservation is not None:
                limiter.record_tokens(settled, reservation=current_reservation)
            else:
                limiter.record_tokens(settled)
        current_reservation = None

    def _with_accumulated_usage(out: SegmentExtractionOutcome) -> SegmentExtractionOutcome:
        out.input_tokens = acc_input_tokens
        out.output_tokens = acc_output_tokens
        out.total_tokens = acc_total_tokens
        return out

    def _segment_deadline_outcome(model_name: str) -> SegmentExtractionOutcome:
        return _with_accumulated_usage(
            SegmentExtractionOutcome(
                model_used=model_name,
                ok=False,
                status="failed",
                error_kind="segment_deadline_exceeded",
                in_flight=False,
                error=f"segment_deadline_exceeded ({eff_seg_deadline_s or 0.0:.1f}s)",
            )
        )

    async def _wait_task_or_timeout(
        task: asyncio.Task[Any],
        timeout_s: float,
    ) -> bool:
        """Wait for `task` to finish or `timeout_s` to elapse via `sleep_fn`. Returns True if `task` finished."""
        if task.done():
            return True
        eff_timeout = max(0.0, float(timeout_s))
        if sleep_fn is asyncio.sleep:
            done, _ = await asyncio.wait({task}, timeout=eff_timeout)
            return bool(done)

        async def _timeout_sleeper() -> None:
            for _ in range(5):
                if task.done():
                    return
                await asyncio.sleep(0)
            await sleep_fn(eff_timeout)

        timer_task = asyncio.create_task(_timeout_sleeper())
        try:
            done, _ = await asyncio.wait(
                {task, timer_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                return True
            for _ in range(5):
                if task.done():
                    return True
                await asyncio.sleep(0)
            return task.done()
        finally:
            if not timer_task.done():
                timer_task.cancel()
                timer_task.add_done_callback(
                    lambda t: None if t.cancelled() else t.exception()
                )

    async def _invoke_extract_and_classify(
        active_model: str,
        resend_flag: bool,
    ) -> SegmentExtractionOutcome:
        extra_kwargs: dict[str, Any] = {}
        if supports_persist_raw:
            # Strip card `quote` and raw event text before writing `dalton_llm_journal` (`replayable=False`)
            extra_kwargs["persist_raw"] = False
        if supports_skip_journal:
            extra_kwargs["skip_journal"] = dry_run
        if supports_deadline:
            extra_kwargs["deadline"] = (
                eff_deadline
                if clock_fn is None
                else (time.monotonic() + max(0.0, eff_deadline - _now_mono()))
            )

        rem_s = eff_deadline - _now_mono()
        if rem_s <= 0:
            raise _SegmentDeadlineExceededError(short_circuit=False)

        extract_coro = extract_fn(
            segment,
            extractor_model=active_model,
            prompt_version=prompt_version,
            user_id=user_id,
            llm_service=llm_service,
            resend_indeterminate=resend_flag,
            **extra_kwargs,
        )
        extract_task: asyncio.Task[SegmentExtractionOutcome] = asyncio.create_task(
            extract_coro
        )
        try:
            finished = await _wait_task_or_timeout(extract_task, rem_s)
            if not finished:
                extract_task.cancel()
                exited = await _wait_task_or_timeout(
                    extract_task, CANCEL_GRACE_TIMEOUT_S
                )
                if not exited:
                    seg_log_id = _format_segment_id(segment)
                    logger.warning(
                        "Segment %s extraction task (model=%s) did not exit within %.1fs after cancellation; abandoning task",
                        seg_log_id,
                        active_model,
                        CANCEL_GRACE_TIMEOUT_S,
                    )
                    _flush_log_handlers()
                    _register_abandoned_task(extract_task, seg_log_id)
                    raise _SegmentDeadlineExceededError()

                if extract_task.cancelled():
                    raise _SegmentDeadlineExceededError()
                exc_after_cancel = extract_task.exception()
                if exc_after_cancel is not None:
                    raise _SegmentDeadlineExceededError() from exc_after_cancel
                out = extract_task.result()
            else:
                out = await extract_task
        except _SegmentDeadlineExceededError:
            raise
        except asyncio.CancelledError:
            in_tok = 0
            out_tok = 0
            tot_tok = 0
            short_circuit = False
            if not extract_task.done():
                # Registered so the exit path sees it: if it is still cleaning
                # up (or swallowed the cancel) when the run ends, the process
                # exits through the abandoned-task path instead of closing the
                # loop under it.
                extract_task.cancel()
                _register_abandoned_task(extract_task, _format_segment_id(segment))
            elif not extract_task.cancelled() and extract_task.exception() is None:
                done_out = extract_task.result()
                short_circuit = _did_journal_short_circuit_without_send(done_out)
                if not short_circuit:
                    in_tok = int(done_out.input_tokens or 0)
                    out_tok = int(done_out.output_tokens or 0)
                    tot_tok = int(done_out.total_tokens or (in_tok + out_tok))
            _settle_attempt(short_circuit, in_tok, out_tok, tot_tok)
            raise
        except Exception as exc:
            if _now_mono() >= eff_deadline:
                raise _SegmentDeadlineExceededError() from exc
            if isinstance(exc, PermanentQuotaError) or is_permanent_quota_error(exc):
                _release_current_reservation()
                raise PermanentQuotaError(
                    type(exc).__name__,
                    status_code=getattr(exc, "status_code", None),
                ) from exc
            if is_in_flight_error(exc):
                out = SegmentExtractionOutcome(
                    model_used=active_model,
                    ok=False,
                    status="in_flight",
                    error_kind="in_flight",
                    in_flight=True,
                    error=f"{type(exc).__name__}: {exc}",
                )
            elif is_rate_limit_or_quota_error(exc):
                out = SegmentExtractionOutcome(
                    model_used=active_model,
                    ok=False,
                    rate_limited=True,
                    error=type(exc).__name__,
                )
            elif is_transient_network_error(exc):
                status_code = getattr(exc, "status_code", None) or getattr(
                    getattr(exc, "response", None), "status_code", None
                )
                err_kind = (
                    f"http_{status_code}"
                    if isinstance(status_code, int) and status_code in (502, 503, 504)
                    else ("timeout" if "timeout" in type(exc).__name__.lower() else "stream_cut")
                )
                out = SegmentExtractionOutcome(
                    model_used=active_model,
                    ok=False,
                    status="indeterminate",
                    error_kind=err_kind,
                    transient_error=True,
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
                out = SegmentExtractionOutcome(
                    model_used=active_model,
                    ok=False,
                    status="error",
                    error_kind=type(exc).__name__,
                    error=f"{type(exc).__name__}: {exc}",
                )

        out_ek = str(out.error_kind or "").strip().lower()
        if _now_mono() >= eff_deadline or out_ek in ("deadline", "deadline_exceeded"):
            short_circuit = _did_journal_short_circuit_without_send(out)
            in_tok = 0 if short_circuit else int(out.input_tokens or 0)
            out_tok = 0 if short_circuit else int(out.output_tokens or 0)
            tot_tok = (
                0
                if short_circuit
                else int(out.total_tokens or (in_tok + out_tok))
            )
            raise _SegmentDeadlineExceededError(
                short_circuit=short_circuit,
                input_tokens=in_tok,
                output_tokens=out_tok,
                total_tokens=tot_tok,
            )
        return out

    async def _sleep_bounded_by_seg_deadline(delay_s: float) -> None:
        rem_s = eff_deadline - _now_mono()
        if rem_s <= 0:
            raise _SegmentDeadlineExceededError()
        sleep_s = min(float(delay_s), rem_s)
        clamped = float(delay_s) >= rem_s - 1e-9
        await sleep_fn(sleep_s)
        if clamped or _now_mono() >= eff_deadline:
            raise _SegmentDeadlineExceededError()

    dl_token = _ACTIVE_SEGMENT_DEADLINE.set(eff_deadline)
    try:
        while True:
            if _now_mono() >= seg_deadline:
                _release_current_reservation()
                return _segment_deadline_outcome(extractor_model), rl_attempt + transient_attempt

            if deadline is not None and _now_mono() >= deadline:
                _release_current_reservation()
                return (
                    _with_accumulated_usage(
                        SegmentExtractionOutcome(
                            model_used=extractor_model,
                            ok=False,
                            status="in_flight",
                            error_kind="in_flight_timeout",
                            in_flight=True,
                            error="in_flight_timeout",
                        )
                    ),
                    rl_attempt + transient_attempt,
                )

            if limiter is not None:
                try:
                    if needs_budget_recheck:
                        new_res = await _budget_wait_excluded()
                        needs_budget_recheck = False
                        if current_reservation is not None:
                            current_reservation.rebind(new_res)
                        else:
                            current_reservation = (
                                new_res
                                if isinstance(new_res, BudgetReservation)
                                else BudgetReservation(
                                    int(new_res) if type(new_res) is int else 0,
                                    estimated_tokens,
                                )
                            )
                    else:
                        await limiter.wait_for_rate_limit_pause()
                except _SegmentDeadlineExceededError:
                    _release_current_reservation()
                    return _segment_deadline_outcome(extractor_model), rl_attempt + transient_attempt

            if _now_mono() >= seg_deadline:
                _release_current_reservation()
                return _segment_deadline_outcome(extractor_model), rl_attempt + transient_attempt

            if deadline is not None and _now_mono() >= deadline:
                _release_current_reservation()
                return (
                    _with_accumulated_usage(
                        SegmentExtractionOutcome(
                            model_used=extractor_model,
                            ok=False,
                            status="in_flight",
                            error_kind="in_flight_timeout",
                            in_flight=True,
                            error="in_flight_timeout",
                        )
                    ),
                    rl_attempt + transient_attempt,
                )

            try:
                outcome = await _invoke_extract_and_classify(
                    extractor_model,
                    eff_resend_indeterminate,
                )
            except _SegmentDeadlineExceededError as dl_exc:
                _settle_attempt(
                    dl_exc.short_circuit,
                    dl_exc.input_tokens,
                    dl_exc.output_tokens,
                    dl_exc.total_tokens,
                )
                return _segment_deadline_outcome(extractor_model), rl_attempt + transient_attempt

            if outcome.permanent_quota_error:
                _release_current_reservation()
                raise PermanentQuotaError(outcome.error or "permanent_quota_error")

            attempt_short_circuit = _did_journal_short_circuit_without_send(outcome)
            _settle_attempt(
                attempt_short_circuit,
                int(outcome.input_tokens or 0),
                int(outcome.output_tokens or 0),
                int(outcome.total_tokens or 0),
            )

            if outcome.ok:
                return _with_accumulated_usage(outcome), rl_attempt + transient_attempt

            if is_in_flight_error(outcome):
                outcome.in_flight = True
                outcome.status = outcome.status or "in_flight"
                outcome.error_kind = outcome.error_kind or "in_flight"
                return _with_accumulated_usage(outcome), rl_attempt + transient_attempt

            if outcome.rate_limited or is_rate_limit_or_quota_error(outcome):
                if limiter is not None:
                    needs_budget_recheck = True

                if _now_mono() >= seg_deadline:
                    return _segment_deadline_outcome(extractor_model), rl_attempt + transient_attempt

                if deadline is not None and _now_mono() >= deadline:
                    return (
                        _with_accumulated_usage(
                            SegmentExtractionOutcome(
                                model_used=extractor_model,
                                ok=False,
                                status="in_flight",
                                error_kind="in_flight_timeout",
                                in_flight=True,
                                error="in_flight_timeout",
                            )
                        ),
                        rl_attempt + transient_attempt,
                    )

                delay = min(cap_s, init_s * (2 ** min(rl_attempt, 8)))
                rl_attempt += 1
                rem_for_rl = max(0.0, eff_deadline - _now_mono())
                eff_rl_delay = min(delay, rem_for_rl)
                pause_until: float | None = None
                if limiter is not None and eff_rl_delay > 0:
                    pause_until = limiter.notify_rate_limit_backoff(eff_rl_delay)
                logger.warning(
                    "Rate limit / quota exhaustion on segment (attempt=%d); backing off for %.1fs before retry",
                    rl_attempt,
                    eff_rl_delay,
                )
                _flush_log_handlers()
                uses_custom_clock = (
                    getattr(limiter, "_uses_custom_clock", True) if limiter is not None else True
                )
                t_sleep0 = limiter._clock() if (limiter is not None and not uses_custom_clock) else 0.0
                try:
                    await _sleep_bounded_by_seg_deadline(delay)
                    if limiter is not None and not uses_custom_clock:
                        elapsed_sleep = max(0.0, limiter._clock() - t_sleep0)
                        if elapsed_sleep < eff_rl_delay * 0.5:
                            limiter._virtual_time_offset += eff_rl_delay - elapsed_sleep
                except _SegmentDeadlineExceededError:
                    return _segment_deadline_outcome(extractor_model), rl_attempt + transient_attempt
                finally:
                    if limiter is not None:
                        limiter.clear_rate_limit_backoff(pause_until)
                continue

            if (
                billing_type != "oauth"
                and not reextract_delegated_fallback
                and is_fallback_eligible(outcome)
            ):
                if not eff_fallback_model or eff_fallback_model == extractor_model:
                    return _with_accumulated_usage(outcome), rl_attempt + transient_attempt
                if _now_mono() >= seg_deadline:
                    return _segment_deadline_outcome(extractor_model), rl_attempt + transient_attempt
                if deadline is not None and _now_mono() >= deadline:
                    return (
                        _with_accumulated_usage(
                            SegmentExtractionOutcome(
                                model_used=extractor_model,
                                ok=False,
                                status="in_flight",
                                error_kind="in_flight_timeout",
                                in_flight=True,
                                error="in_flight_timeout",
                            )
                        ),
                        rl_attempt + transient_attempt,
                    )

                if limiter is not None:
                    try:
                        new_res = await _budget_wait_excluded()
                    except _SegmentDeadlineExceededError:
                        return _segment_deadline_outcome(eff_fallback_model), rl_attempt + transient_attempt
                    current_reservation = (
                        new_res
                        if isinstance(new_res, BudgetReservation)
                        else BudgetReservation(
                            int(new_res) if type(new_res) is int else 0,
                            estimated_tokens,
                        )
                    )
                    if _now_mono() >= seg_deadline:
                        _release_current_reservation()
                        return _segment_deadline_outcome(eff_fallback_model), rl_attempt + transient_attempt
                    if deadline is not None and _now_mono() >= deadline:
                        _release_current_reservation()
                        return (
                            _with_accumulated_usage(
                                SegmentExtractionOutcome(
                                    model_used=eff_fallback_model,
                                    ok=False,
                                    status="in_flight",
                                    error_kind="in_flight_timeout",
                                    in_flight=True,
                                    error="in_flight_timeout",
                                )
                            ),
                            rl_attempt + transient_attempt,
                        )

                fb_billing_type, _ = resolve_model_billing_and_provider(eff_fallback_model)
                fb_resend_indeterminate = fb_billing_type == "oauth"
                logger.warning(
                    "Primary model %s failed on segment (%s: %s); retrying once with fallback model %s",
                    extractor_model,
                    outcome.error_kind or outcome.status or "transient_error",
                    outcome.error,
                    eff_fallback_model,
                )
                _flush_log_handlers()
                transient_attempt += 1
                try:
                    fb_outcome = await _invoke_extract_and_classify(
                        eff_fallback_model,
                        fb_resend_indeterminate,
                    )
                except _SegmentDeadlineExceededError as dl_exc:
                    _settle_attempt(
                        dl_exc.short_circuit,
                        dl_exc.input_tokens,
                        dl_exc.output_tokens,
                        dl_exc.total_tokens,
                    )
                    return _segment_deadline_outcome(eff_fallback_model), rl_attempt + transient_attempt

                if fb_outcome.permanent_quota_error:
                    _release_current_reservation()
                    raise PermanentQuotaError(fb_outcome.error or "permanent_quota_error")

                fb_short_circuit = _did_journal_short_circuit_without_send(fb_outcome)
                _settle_attempt(
                    fb_short_circuit,
                    int(fb_outcome.input_tokens or 0),
                    int(fb_outcome.output_tokens or 0),
                    int(fb_outcome.total_tokens or 0),
                )

                if fb_outcome.ok:
                    fb_outcome.model_used = str(fb_outcome.model_used or eff_fallback_model)
                    for card in fb_outcome.cards:
                        if isinstance(card, dict):
                            card["extractor_model"] = fb_outcome.model_used
                elif is_in_flight_error(fb_outcome):
                    fb_outcome.in_flight = True
                    fb_outcome.status = fb_outcome.status or "in_flight"
                    fb_outcome.error_kind = fb_outcome.error_kind or "in_flight"

                return _with_accumulated_usage(fb_outcome), rl_attempt + transient_attempt

            if is_transient_network_error(outcome):
                outcome.transient_error = True
                if transient_attempt < len(TRANSIENT_BACKOFF_DELAYS_S):
                    if limiter is not None:
                        needs_budget_recheck = True
                    if _now_mono() >= seg_deadline:
                        return _segment_deadline_outcome(extractor_model), rl_attempt + transient_attempt
                    if deadline is not None and _now_mono() >= deadline:
                        return (
                            _with_accumulated_usage(
                                SegmentExtractionOutcome(
                                    model_used=extractor_model,
                                    ok=False,
                                    status="in_flight",
                                    error_kind="in_flight_timeout",
                                    in_flight=True,
                                    error="in_flight_timeout",
                                )
                            ),
                            rl_attempt + transient_attempt,
                        )
                    delay = TRANSIENT_BACKOFF_DELAYS_S[transient_attempt]
                    transient_attempt += 1
                    logger.warning(
                        "Transient network/upstream error on segment (retry=%d/%d, backoff=%.1fs): %s",
                        transient_attempt,
                        len(TRANSIENT_BACKOFF_DELAYS_S),
                        delay,
                        outcome.error,
                    )
                    _flush_log_handlers()
                    try:
                        await _sleep_bounded_by_seg_deadline(delay)
                    except _SegmentDeadlineExceededError:
                        return _segment_deadline_outcome(extractor_model), rl_attempt + transient_attempt
                    continue
                return _with_accumulated_usage(outcome), rl_attempt + transient_attempt

            return _with_accumulated_usage(outcome), rl_attempt + transient_attempt
    finally:
        _ACTIVE_SEGMENT_DEADLINE.reset(dl_token)


def read_inbox_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read sanitized conversation events from an inbox `.jsonl` file."""
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        obj = json.loads(s)
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _chunk_key(ev: dict[str, Any]) -> tuple[str, str, str, int]:
    """Return the `(agent, session_id, event_id, chunk_idx)` primary key for an event or split chunk."""
    ag = str(ev.get("agent") or "main")
    sid = str(ev.get("session_id") or "")
    eid = str(ev.get("event_id") or f"seq:{int(ev.get('seq', 0))}")
    cidx = int(ev.get("chunk_idx", 0))
    return (ag, sid, eid, cidx)


def _make_processed_row(ev: dict[str, Any], status: str) -> dict[str, Any]:
    """Build a row dict for `dalton_conversation_processed` (`status` in `{'extracted', 'discarded'}`)."""
    ag, sid, eid, cidx = _chunk_key(ev)
    raw_spans = ev.get("chunk_spans")
    spans = list(raw_spans) if isinstance(raw_spans, list) else []
    return {
        "agent": ag,
        "session_id": sid,
        "event_id": eid,
        "chunk_idx": cidx,
        "chunk_count": max(1, int(ev.get("chunk_count", len(spans) or 1))),
        "chunk_max_chars": max(1, int(ev.get("chunk_max_chars", 6000))),
        "chunk_spans": spans,
        "status": status,
    }


def _flush_log_handlers() -> None:
    for h in (*logger.handlers, *logging.getLogger().handlers):
        try:
            h.flush()
        except Exception:
            pass
    try:
        sys.stderr.flush()
    except Exception:
        pass


def _compact_card_for_dry_run_pool(card: dict[str, Any]) -> dict[str, Any]:
    """Keep only fields needed by `compute_batch_supersedes` in dry-run active pool, dropping raw quotes/text."""
    return {
        "card_id": card.get("card_id"),
        "status": card.get("status", "active"),
        "tickers": list(card.get("tickers") or []),
        "figures": [
            {
                "metric": f.get("metric"),
                "value": f.get("value"),
                "unit": f.get("unit"),
                "period": f.get("period"),
            }
            for f in (card.get("figures") or [])
            if isinstance(f, dict)
        ],
        "ts": card.get("ts"),
        "seq_range": list(card.get("seq_range") or []),
        "supersedes": card.get("supersedes"),
        "superseded_card_ids": list(card.get("superseded_card_ids") or []),
        "superseded_by_card_id": card.get("superseded_by_card_id"),
    }


def _compact_rendered_summary(rendered: dict[str, Any]) -> dict[str, Any]:
    """Return a summary of rendered files without retaining full per-card `card_locations` maps."""
    return {
        "company_files": list(rendered.get("company_files") or []),
        "topic_files": list(rendered.get("topic_files") or []),
        "removed_files": list(rendered.get("removed_files") or []),
        "unmapped_tickers": list(rendered.get("unmapped_tickers") or []),
        "rendered_dates": list(rendered.get("rendered_dates") or []),
    }


def _get_maxrss_stats() -> tuple[int, float]:
    """Return `(raw_ru_maxrss, maxrss_mib)` for the current process via `resource.getrusage`."""
    rusage = resource.getrusage(resource.RUSAGE_SELF)
    raw_rss = int(rusage.ru_maxrss)
    rss_kb = raw_rss / 1024.0 if sys.platform == "darwin" else float(raw_rss)
    return raw_rss, round(rss_kb / 1024.0, 2)


async def process_weekly_batch(
    batch_events: list[dict[str, Any]],
    *,
    mode: str,
    window_start: date,
    window_end: date,
    limiter: HourlyTokenBudgetLimiter,
    extractor_model: str,
    prompt_version: str,
    extractor_fallback_model: str | None = None,
    segment_deadline_s: float | None = None,
    dry_run: bool = False,
    initial_backoff_s: float = 30.0,
    max_backoff_s: float = 900.0,
    stale_after_s: int = DEFAULT_STALE_AFTER_S,
    defer_retry_min_s: float = DEFAULT_DEFER_RETRY_MIN_S,
    defer_retry_max_s: float = DEFAULT_DEFER_RETRY_MAX_S,
    extract_concurrency: int | None = None,
    concurrency: int | None = None,
    llm_service: Any = None,
    extract_fn: Callable[..., Coroutine[Any, Any, SegmentExtractionOutcome]] = extract_segment_cards,
    embed_fn: Callable[..., Coroutine[Any, Any, tuple[list[list[float]], str]]] = embed_texts_via_bridge,
    sleep_fn: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
    clock_fn: Callable[[], float] | None = None,
    backend_by_ticker: dict[str, Any] | None = None,
    home_backend: Any = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Process one 7-day weekly batch: segment -> concurrent extract -> ordered prefix persist -> render -> log run."""
    cfg = get_dalton_settings().conversations
    eff_concurrency = max(
        1,
        int(concurrency or extract_concurrency or getattr(cfg, "extract_concurrency", 1) or 1),
    )
    eff_fallback_model = (
        str(getattr(cfg, "extractor_fallback_model", DEFAULT_FALLBACK_MODEL) or "")
        if extractor_fallback_model is None
        else str(extractor_fallback_model)
    ).strip()
    eff_segment_deadline_s = float(
        segment_deadline_s
        if segment_deadline_s is not None
        else getattr(cfg, "segment_deadline_s", DEFAULT_SEGMENT_DEADLINE_S)
    )
    run_id = f"{mode}_{window_start.isoformat()}_{window_end.isoformat()}_{uuid.uuid4().hex[:8]}"
    embedding_model_used = cfg.embedding_model_preferred
    total_events_read = len(batch_events)

    if not dry_run:
        await db_conv.insert_run_start(
            run_id=run_id,
            mode=mode,
            extractor_model=extractor_model,
            prompt_version=prompt_version,
            embedding_model=embedding_model_used,
            window_start=window_start,
            window_end=window_end,
            conn=conn,
        )

    segments = segment_events(
        batch_events,
        max_gap_minutes=cfg.max_segment_gap_minutes,
        max_segment_chars=cfg.max_segment_chars,
        max_segment_events=cfg.max_segment_events,
    )
    batch_events.clear()
    batch_events = []
    total_segments = len(segments)
    logger.info(
        "Weekly batch started: run_id=%s window=%s..%s events=%d segments=%d model=%s fallback=%s",
        run_id,
        window_start.isoformat(),
        window_end.isoformat(),
        total_events_read,
        total_segments,
        extractor_model,
        eff_fallback_model or "none",
    )
    _flush_log_handlers()
    _maybe_start_tracemalloc()
    segments_trace_counter = 0

    cards_extracted = 0
    cards_accepted = 0
    accepted_dates: set[str] = set()
    processed_chunk_keys: list[tuple[str, str, str, int]] = []
    dry_run_active_pool: list[dict[str, Any]] = []
    dry_run_seen_hashes: set[str] = set()
    segments_flushed = 0
    quotes_rejected = 0
    segments_discarded = 0
    cards_flagged_unsupported_number = 0
    cards_flagged_unsupported_names = 0
    cards_flagged_unsupported_conclusion = 0
    soft_unsupported_numbers = 0
    figures_dropped = 0
    periods_cleared = 0
    currencies_stripped = 0
    tickers_dropped = 0
    tickers_dropped_private = 0
    tickers_dropped_channel = 0
    tickers_backfilled = 0
    types_corrected = 0
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    aborted_in_tokens = 0
    aborted_out_tokens = 0
    aborted_tot_tokens = 0
    rate_limit_retries = 0
    cards_inserted = 0
    deduped_count = 0
    superseded_count = 0

    completed_by_idx: dict[int, tuple[list[dict[str, Any]], SegmentExtractionOutcome, int]] = {}
    deferred_by_idx: dict[int, tuple[list[dict[str, Any]], SegmentExtractionOutcome, int]] = {}
    incomplete_by_idx: dict[int, dict[str, Any]] = {}
    # Set once the incomplete segments' usage is in the run totals, so a later
    # failure (rendering, run finish) does not add it again.
    incomplete_usage_rolled = False
    seg_deadline_by_idx: dict[int, float] = {}
    next_to_flush = 0
    flush_failed = False
    flush_lock = asyncio.Lock()
    sem = asyncio.Semaphore(eff_concurrency)
    abort_flag = False

    def _seg_now() -> float:
        if clock_fn is not None:
            return clock_fn()
        if type(limiter) is HourlyTokenBudgetLimiter:
            return limiter._now()
        return time.monotonic()

    def _make_incomplete_record(
        seg_idx: int,
        seg: list[dict[str, Any]],
        outcome: SegmentExtractionOutcome,
        retries_used: int,
        *,
        status: str = "failed",
        reason: str | None = None,
    ) -> dict[str, Any]:
        first_ev = seg[0] if seg else {}
        last_ev = seg[-1] if seg else {}
        inflight = is_in_flight_error(outcome)
        eff_reason = reason or (
            "in_flight_timeout"
            if inflight
            else (
                "blocked_by_incomplete_prefix"
                if status == "blocked"
                else (outcome.error_kind or "transient_error")
            )
        )
        return {
            "segment_index": seg_idx,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "agent": str(first_ev.get("agent") or ""),
            "session_id": str(first_ev.get("session_id") or ""),
            "first_event_id": str(first_ev.get("event_id") or ""),
            "last_event_id": str(last_ev.get("event_id") or ""),
            "seq_range": [int(first_ev.get("seq", 0)), int(last_ev.get("seq", 0))],
            "events_count": len(seg),
            "status": status,
            "reason": eff_reason,
            "error_kind": outcome.error_kind or eff_reason,
            "retries": int(retries_used),
            "input_tokens": int(outcome.input_tokens),
            "output_tokens": int(outcome.output_tokens),
            "total_tokens": int(outcome.total_tokens),
            "error": outcome.error or eff_reason,
        }

    async def _flush_ready_prefix() -> None:
        nonlocal next_to_flush, flush_failed, abort_flag, embedding_model_used
        nonlocal segments_flushed, quotes_rejected, segments_discarded
        nonlocal cards_flagged_unsupported_number, cards_flagged_unsupported_names, cards_flagged_unsupported_conclusion
        nonlocal soft_unsupported_numbers, figures_dropped, periods_cleared, currencies_stripped
        nonlocal tickers_dropped, tickers_dropped_private, tickers_dropped_channel, tickers_backfilled, types_corrected
        nonlocal input_tokens, output_tokens, total_tokens, rate_limit_retries
        nonlocal cards_extracted, cards_accepted, cards_inserted, deduped_count, superseded_count

        async with flush_lock:
            if flush_failed:
                return
            try:
                while next_to_flush in completed_by_idx:
                    if incomplete_by_idx and next_to_flush >= min(incomplete_by_idx):
                        break
                    ready_batch: list[tuple[int, list[dict[str, Any]], SegmentExtractionOutcome, int]] = []
                    peek_idx = next_to_flush
                    while peek_idx in completed_by_idx and len(ready_batch) < eff_concurrency:
                        if incomplete_by_idx and peek_idx >= min(incomplete_by_idx):
                            break
                        seg, outcome, rl_retries = completed_by_idx[peek_idx]
                        ready_batch.append((peek_idx, seg, outcome, rl_retries))
                        peek_idx += 1

                    if not ready_batch:
                        break

                    batch_segments = 0
                    batch_rl_retries = 0
                    batch_in_tokens = 0
                    batch_out_tokens = 0
                    batch_tot_tokens = 0
                    batch_quotes_rejected = 0
                    batch_segments_discarded = 0
                    batch_cards_flagged_unsupported_number = 0
                    batch_cards_flagged_unsupported_names = 0
                    batch_cards_flagged_unsupported_conclusion = 0
                    batch_soft_unsupported_numbers = 0
                    batch_figures_dropped = 0
                    batch_periods_cleared = 0
                    batch_currencies_stripped = 0
                    batch_tickers_dropped = 0
                    batch_tickers_dropped_private = 0
                    batch_tickers_dropped_channel = 0
                    batch_tickers_backfilled = 0
                    batch_types_corrected = 0
                    batch_candidate_cards: list[dict[str, Any]] = []
                    per_seg_items: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
                    for _seg_idx, seg, outcome, rl_retries in ready_batch:
                        batch_segments += 1
                        batch_rl_retries += rl_retries
                        batch_in_tokens += outcome.input_tokens
                        batch_out_tokens += outcome.output_tokens
                        batch_tot_tokens += outcome.total_tokens
                        batch_quotes_rejected += outcome.quotes_rejected
                        batch_cards_flagged_unsupported_number += int(
                            getattr(outcome, "cards_flagged_unsupported_number", 0)
                        )
                        batch_cards_flagged_unsupported_names += int(
                            getattr(outcome, "cards_flagged_unsupported_names", 0)
                        )
                        batch_cards_flagged_unsupported_conclusion += int(
                            getattr(outcome, "cards_flagged_unsupported_conclusion", 0)
                        )
                        batch_soft_unsupported_numbers += int(
                            getattr(outcome, "soft_unsupported_numbers", 0)
                        )
                        batch_figures_dropped += int(getattr(outcome, "figures_dropped", 0))
                        batch_periods_cleared += int(getattr(outcome, "periods_cleared", 0))
                        batch_currencies_stripped += int(
                            getattr(outcome, "currencies_stripped", 0)
                        )
                        batch_tickers_dropped += int(getattr(outcome, "tickers_dropped", 0))
                        batch_tickers_dropped_private += int(
                            getattr(outcome, "tickers_dropped_private", 0)
                        )
                        batch_tickers_dropped_channel += int(
                            getattr(outcome, "tickers_dropped_channel", 0)
                        )
                        batch_tickers_backfilled += int(
                            getattr(outcome, "tickers_backfilled", 0)
                        )
                        batch_types_corrected += int(getattr(outcome, "types_corrected", 0))
                        if outcome.discarded_segment:
                            batch_segments_discarded += 1
                            seg_status = "discarded"
                        else:
                            seg_status = "extracted"
                        seg_cards = list(outcome.cards)
                        seg_rows = [_make_processed_row(ev, seg_status) for ev in seg]
                        batch_candidate_cards.extend(seg_cards)
                        per_seg_items.append((seg_cards, seg_rows))

                    if dry_run:
                        acc_cards, dedup_cnt, sup_cnt = resolve_dedup_and_supersedes(
                            batch_candidate_cards,
                            existing_hashes=dry_run_seen_hashes,
                            existing_active_cards=dry_run_active_pool,
                        )
                        for c in acc_cards:
                            ch = c.get("content_hash")
                            if ch:
                                dry_run_seen_hashes.add(str(ch))
                            dry_run_active_pool.append(_compact_card_for_dry_run_pool(c))
                        dry_run_active_pool[:] = [
                            c for c in dry_run_active_pool if c.get("status") == "active"
                        ]
                        for seg_idx, seg_list, seg_outcome, _ in ready_batch:
                            completed_by_idx.pop(seg_idx, None)
                            seg_list.clear()
                            seg_outcome.cards.clear()
                            if 0 <= seg_idx < len(segments):
                                segments[seg_idx].clear()
                                segments[seg_idx] = []
                        ready_batch.clear()
                        next_to_flush = peek_idx
                        segments_flushed += batch_segments
                        rate_limit_retries += batch_rl_retries
                        input_tokens += batch_in_tokens
                        output_tokens += batch_out_tokens
                        total_tokens += batch_tot_tokens
                        quotes_rejected += batch_quotes_rejected
                        segments_discarded += batch_segments_discarded
                        cards_flagged_unsupported_number += batch_cards_flagged_unsupported_number
                        cards_flagged_unsupported_names += batch_cards_flagged_unsupported_names
                        cards_flagged_unsupported_conclusion += (
                            batch_cards_flagged_unsupported_conclusion
                        )
                        soft_unsupported_numbers += batch_soft_unsupported_numbers
                        figures_dropped += batch_figures_dropped
                        periods_cleared += batch_periods_cleared
                        currencies_stripped += batch_currencies_stripped
                        tickers_dropped += batch_tickers_dropped
                        tickers_dropped_private += batch_tickers_dropped_private
                        tickers_dropped_channel += batch_tickers_dropped_channel
                        tickers_backfilled += batch_tickers_backfilled
                        types_corrected += batch_types_corrected
                        cards_extracted += len(batch_candidate_cards)
                        cards_accepted += len(acc_cards)
                        deduped_count += dedup_cnt
                        superseded_count += sup_cnt
                        for _seg_cards, seg_rows in per_seg_items:
                            for r in seg_rows:
                                processed_chunk_keys.append(
                                    (r["agent"], r["session_id"], r["event_id"], int(r["chunk_idx"]))
                                )
                        continue

                    all_hashes = [
                        str(c["content_hash"])
                        for c in batch_candidate_cards
                        if c.get("content_hash")
                    ]
                    existing_hashes = (
                        set(await db_conv.find_existing_content_hashes(all_hashes, conn=conn))
                        if all_hashes
                        else set()
                    )
                    seen_hashes = set(existing_hashes)
                    per_seg_deduped: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
                    all_deduped_cards: list[dict[str, Any]] = []
                    batch_dedup_cnt = 0
                    for seg_cards, seg_rows in per_seg_items:
                        if seg_cards:
                            seg_deduped, seg_dedup_cnt = deduplicate_candidate_cards(
                                seg_cards,
                                existing_hashes=seen_hashes,
                            )
                            batch_dedup_cnt += seg_dedup_cnt
                            for c in seg_deduped:
                                ch = c.get("content_hash")
                                if ch:
                                    seen_hashes.add(str(ch))
                            all_deduped_cards.extend(seg_deduped)
                        else:
                            seg_deduped = []
                        per_seg_deduped.append((seg_deduped, seg_rows))

                    if all_deduped_cards:
                        search_texts = [str(c.get("search_text") or "") for c in all_deduped_cards]
                        all_embeddings, batch_emb_model = await embed_fn(
                            search_texts,
                            task_type="RETRIEVAL_DOCUMENT",
                        )
                    else:
                        all_embeddings = []
                        batch_emb_model = embedding_model_used

                    batch_cards_inserted = 0
                    batch_superseded_count = 0
                    batch_accepted_count = 0
                    batch_accepted_dates: set[str] = set()
                    batch_processed_keys: list[tuple[str, str, str, int]] = []

                    emb_offset = 0
                    async with db_conv.transaction_scope(conn) as tx_conn:
                        for seg_deduped, seg_rows in per_seg_deduped:
                            seg_len = len(seg_deduped)
                            seg_embs = all_embeddings[emb_offset : emb_offset + seg_len]
                            emb_offset += seg_len
                            if seg_deduped:
                                ins_cnt, sup_cnt, ins_cards = await db_conv.insert_cards_batch(
                                    seg_deduped,
                                    embeddings=seg_embs,
                                    conn=tx_conn,
                                )
                                batch_cards_inserted += ins_cnt
                                batch_superseded_count += sup_cnt
                                batch_accepted_count += len(ins_cards)
                                for c in ins_cards:
                                    d_et = str(c.get("date_et") or "")[:10]
                                    if d_et:
                                        batch_accepted_dates.add(d_et)
                            if seg_rows:
                                await db_conv.record_processed_event_chunks(
                                    seg_rows,
                                    conn=tx_conn,
                                )
                                for r in seg_rows:
                                    batch_processed_keys.append(
                                        (r["agent"], r["session_id"], r["event_id"], int(r["chunk_idx"]))
                                    )

                        await db_conv.update_run_progress(
                            run_id,
                            embedding_model=batch_emb_model,
                            events_read=total_events_read,
                            segments_processed=segments_flushed + batch_segments,
                            cards_extracted=cards_extracted + len(batch_candidate_cards),
                            cards_inserted=cards_inserted + batch_cards_inserted,
                            cards_deduped=deduped_count + batch_dedup_cnt,
                            cards_superseded=superseded_count + batch_superseded_count,
                            quotes_rejected=quotes_rejected + batch_quotes_rejected,
                            segments_discarded=segments_discarded + batch_segments_discarded,
                            input_tokens=input_tokens + batch_in_tokens,
                            output_tokens=output_tokens + batch_out_tokens,
                            total_tokens=total_tokens + batch_tot_tokens,
                            stats={
                                "rate_limit_retries": rate_limit_retries + batch_rl_retries,
                                "segments_total": total_segments,
                                "cards_flagged_unsupported_number": (
                                    cards_flagged_unsupported_number
                                    + batch_cards_flagged_unsupported_number
                                ),
                                "cards_flagged_unsupported_names": (
                                    cards_flagged_unsupported_names
                                    + batch_cards_flagged_unsupported_names
                                ),
                                "cards_flagged_unsupported_conclusion": (
                                    cards_flagged_unsupported_conclusion
                                    + batch_cards_flagged_unsupported_conclusion
                                ),
                                "soft_unsupported_numbers": (
                                    soft_unsupported_numbers + batch_soft_unsupported_numbers
                                ),
                                "figures_dropped": figures_dropped + batch_figures_dropped,
                                "periods_cleared": periods_cleared + batch_periods_cleared,
                                "currencies_stripped": (
                                    currencies_stripped + batch_currencies_stripped
                                ),
                                "tickers_dropped": tickers_dropped + batch_tickers_dropped,
                                "tickers_dropped_private": (
                                    tickers_dropped_private + batch_tickers_dropped_private
                                ),
                                "tickers_dropped_channel": (
                                    tickers_dropped_channel + batch_tickers_dropped_channel
                                ),
                                "tickers_backfilled": (
                                    tickers_backfilled + batch_tickers_backfilled
                                ),
                                "types_corrected": types_corrected + batch_types_corrected,
                            },
                            conn=tx_conn,
                        )

                    for seg_idx, seg_list, seg_outcome, _ in ready_batch:
                        completed_by_idx.pop(seg_idx, None)
                        seg_list.clear()
                        seg_outcome.cards.clear()
                        if 0 <= seg_idx < len(segments):
                            segments[seg_idx].clear()
                            segments[seg_idx] = []
                    ready_batch.clear()
                    next_to_flush = peek_idx
                    embedding_model_used = batch_emb_model
                    segments_flushed += batch_segments
                    rate_limit_retries += batch_rl_retries
                    input_tokens += batch_in_tokens
                    output_tokens += batch_out_tokens
                    total_tokens += batch_tot_tokens
                    quotes_rejected += batch_quotes_rejected
                    segments_discarded += batch_segments_discarded
                    cards_flagged_unsupported_number += batch_cards_flagged_unsupported_number
                    cards_flagged_unsupported_names += batch_cards_flagged_unsupported_names
                    cards_flagged_unsupported_conclusion += (
                        batch_cards_flagged_unsupported_conclusion
                    )
                    soft_unsupported_numbers += batch_soft_unsupported_numbers
                    figures_dropped += batch_figures_dropped
                    periods_cleared += batch_periods_cleared
                    currencies_stripped += batch_currencies_stripped
                    tickers_dropped += batch_tickers_dropped
                    tickers_dropped_private += batch_tickers_dropped_private
                    tickers_dropped_channel += batch_tickers_dropped_channel
                    tickers_backfilled += batch_tickers_backfilled
                    types_corrected += batch_types_corrected
                    cards_extracted += len(batch_candidate_cards)
                    deduped_count += batch_dedup_cnt
                    cards_inserted += batch_cards_inserted
                    superseded_count += batch_superseded_count
                    cards_accepted += batch_accepted_count
                    accepted_dates.update(batch_accepted_dates)
                    processed_chunk_keys.extend(batch_processed_keys)
            except BaseException:
                flush_failed = True
                abort_flag = True
                raise

    async def _run_segment_attempt(
        seg: list[dict[str, Any]],
        *,
        seg_deadline: float | None = None,
        deadline: float | None = None,
        seg_idx: int | None = None,
    ) -> tuple[SegmentExtractionOutcome, int, float]:
        nonlocal segments_trace_counter, aborted_in_tokens, aborted_out_tokens, aborted_tot_tokens

        def now_fn() -> float:
            if clock_fn is not None:
                return clock_fn()
            if type(limiter) is HourlyTokenBudgetLimiter:
                return limiter._now()
            return time.monotonic()

        est_tokens = estimate_segment_tokens(seg, hourly_budget=limiter.hourly_budget)

        def _extend_seg_deadline(waited_s: float) -> None:
            # The stored deadline is what the in_flight and deferred checks
            # read, so every budget wait moves it, not just this attempt's copy.
            if seg_idx is not None and seg_idx in seg_deadline_by_idx:
                seg_deadline_by_idx[seg_idx] += waited_s

        t0 = now_fn()
        eff_seg_dl = (
            seg_deadline
            if seg_deadline is not None
            else (t0 + eff_segment_deadline_s)
        )
        # Waiting for hourly budget is deliberate throttling, bounded by the
        # window, not a stall: it runs with no segment deadline, and the
        # segment's work clock (and the stored deadline a deferred retry reads)
        # moves forward by the time spent waiting.
        dl_token = _ACTIVE_SEGMENT_DEADLINE.set(deadline)
        wait_t0 = now_fn()
        try:
            reservation = await limiter.wait_for_budget(est_tokens)
            waited_s = max(0.0, now_fn() - wait_t0)
            if waited_s > 0:
                eff_seg_dl += waited_s
                _extend_seg_deadline(waited_s)
        except _SegmentDeadlineExceededError:
            return (
                SegmentExtractionOutcome(
                    model_used=extractor_model,
                    ok=False,
                    status="failed",
                    error_kind="segment_deadline_exceeded",
                    in_flight=False,
                    error=f"segment_deadline_exceeded ({eff_segment_deadline_s:.1f}s)",
                ),
                0,
                max(0.0, now_fn() - t0),
            )
        finally:
            _ACTIVE_SEGMENT_DEADLINE.reset(dl_token)

        if now_fn() >= eff_seg_dl:
            limiter.release_reservation(reservation)
            return (
                SegmentExtractionOutcome(
                    model_used=extractor_model,
                    ok=False,
                    status="failed",
                    error_kind="segment_deadline_exceeded",
                    in_flight=False,
                    error=f"segment_deadline_exceeded ({eff_segment_deadline_s:.1f}s)",
                ),
                0,
                max(0.0, now_fn() - t0),
            )
        if deadline is not None and now_fn() >= deadline:
            limiter.release_reservation(reservation)
            return (
                SegmentExtractionOutcome(
                    model_used=extractor_model,
                    ok=False,
                    status="in_flight",
                    error_kind="in_flight_timeout",
                    in_flight=True,
                    error="in_flight_timeout",
                ),
                0,
                max(0.0, now_fn() - t0),
            )
        try:
            outcome, rl_retries = await extract_segment_with_infinite_backoff(
                seg,
                extractor_model=extractor_model,
                prompt_version=prompt_version,
                extractor_fallback_model=eff_fallback_model,
                segment_deadline_s=eff_segment_deadline_s,
                seg_deadline=eff_seg_dl,
                user_id=archive_billing_user_id(),
                llm_service=llm_service,
                initial_backoff_s=initial_backoff_s,
                max_backoff_s=max_backoff_s,
                limiter=limiter,
                estimated_tokens=est_tokens,
                reservation=reservation,
                # The raw deferred window: the inner function takes the min with
                # the segment deadline it keeps moving on budget waits.
                deadline=deadline,
                clock_fn=clock_fn,
                dry_run=dry_run,
                extract_fn=extract_fn,
                sleep_fn=sleep_fn,
                on_deadline_extended=_extend_seg_deadline,
            )
        except BaseException:
            limiter.release_reservation(reservation)
            aborted_in_tokens += int(getattr(reservation, "settled_input_tokens", 0))
            aborted_out_tokens += int(getattr(reservation, "settled_output_tokens", 0))
            aborted_tot_tokens += int(getattr(reservation, "settled_total_tokens", 0))
            raise
        elapsed_s = max(0.0, now_fn() - t0)
        segments_trace_counter += 1
        if segments_trace_counter % MEM_TRACE_SEGMENT_INTERVAL == 0:
            _log_tracemalloc_top10(segments_trace_counter)
            _flush_log_handlers()
        return outcome, rl_retries, elapsed_s

    async def _extract_one_segment(seg_idx: int) -> None:
        nonlocal abort_flag, aborted_in_tokens, aborted_out_tokens, aborted_tot_tokens

        async with sem:
            if abort_flag:
                return
            seg = segments[seg_idx] if 0 <= seg_idx < len(segments) else []
            seg_deadline = _seg_now() + eff_segment_deadline_s
            seg_deadline_by_idx[seg_idx] = seg_deadline
            try:
                outcome, rl_retries, elapsed_s = await _run_segment_attempt(
                    seg,
                    seg_deadline=seg_deadline,
                    seg_idx=seg_idx,
                )
            except BaseException:
                abort_flag = True
                raise

            if outcome.permanent_quota_error:
                abort_flag = True
                aborted_in_tokens += int(outcome.input_tokens or 0)
                aborted_out_tokens += int(outcome.output_tokens or 0)
                aborted_tot_tokens += int(outcome.total_tokens or 0)
                raise PermanentQuotaError(outcome.error or "permanent_quota_error")
            if is_in_flight_error(outcome):
                if _seg_now() >= seg_deadline_by_idx.get(seg_idx, seg_deadline):
                    outcome.in_flight = False
                    outcome.status = "failed"
                    outcome.error_kind = "segment_deadline_exceeded"
                    outcome.error = f"segment_deadline_exceeded ({eff_segment_deadline_s:.1f}s)"
                else:
                    deferred_by_idx[seg_idx] = (seg, outcome, rl_retries)
                    seg = []
                    logger.warning(
                        "Segment %d/%d is in_flight in journal; queued for deferred retry after weekly batch",
                        seg_idx + 1,
                        total_segments,
                    )
                    _flush_log_handlers()
                    return
            if not outcome.ok:
                fail_reason = (
                    (outcome.error_kind or "transient_error")
                    if is_transient_network_error(outcome)
                    else _extraction_error_reason(outcome)
                )
                incomplete_by_idx[seg_idx] = _make_incomplete_record(
                    seg_idx,
                    seg,
                    outcome,
                    rl_retries,
                    status="failed",
                    reason=fail_reason,
                )
                seg.clear()
                if 0 <= seg_idx < len(segments):
                    segments[seg_idx] = []
                seg = []
                logger.warning(
                    "Segment %d/%d incomplete (%s, retries=%d): %s",
                    seg_idx + 1,
                    total_segments,
                    fail_reason,
                    rl_retries,
                    outcome.error,
                )
                _flush_log_handlers()
                return

            seg_tokens = outcome.total_tokens or (outcome.input_tokens + outcome.output_tokens)
            logger.info(
                "Segment %d/%d completed: tokens=%d (in=%d, out=%d) elapsed=%.2fs status=%s",
                seg_idx + 1,
                total_segments,
                seg_tokens,
                outcome.input_tokens,
                outcome.output_tokens,
                elapsed_s,
                "discarded" if outcome.discarded_segment else "extracted",
            )
            _flush_log_handlers()

            completed_by_idx[seg_idx] = (seg, outcome, rl_retries)
            seg = []
            await asyncio.shield(_flush_ready_prefix())

    tasks: list[asyncio.Task[None]] = []
    try:
        if segments:
            tasks = [
                asyncio.create_task(_extract_one_segment(idx))
                for idx in range(len(segments))
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            first_exc: BaseException | None = None
            for d in done:
                if not d.cancelled():
                    exc = d.exception()
                    if exc is not None:
                        first_exc = exc
                        break
            if first_exc is not None:
                abort_flag = True
                for p in pending:
                    p.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if not flush_failed:
                    await asyncio.shield(_flush_ready_prefix())
                raise first_exc

            await asyncio.shield(_flush_ready_prefix())

            if deferred_by_idx:
                max_defer_wait_s = max(
                    0.0, float(stale_after_s) + DEFAULT_DEFER_EXTRA_AFTER_STALE_S
                )
                min_interval_s = max(0.01, float(defer_retry_min_s))
                max_interval_s = max(min_interval_s, float(defer_retry_max_s))

                def _defer_now() -> float:
                    if clock_fn is not None:
                        return clock_fn()
                    if type(limiter) is HourlyTokenBudgetLimiter:
                        return limiter._now()
                    return time.monotonic()

                defer_start_mono = _defer_now()
                deadline = defer_start_mono + max_defer_wait_s
                defer_pass = 0

                while deferred_by_idx and _defer_now() < deadline:
                    for exp_idx in [
                        idx
                        for idx in list(deferred_by_idx)
                        if _defer_now()
                        >= seg_deadline_by_idx.get(
                            idx, defer_start_mono + eff_segment_deadline_s
                        )
                    ]:
                        exp_seg, exp_out, exp_retries = deferred_by_idx.pop(exp_idx)
                        exp_out.in_flight = False
                        exp_out.status = "failed"
                        exp_out.error_kind = "segment_deadline_exceeded"
                        exp_out.error = (
                            f"segment_deadline_exceeded ({eff_segment_deadline_s:.1f}s)"
                        )
                        incomplete_by_idx[exp_idx] = _make_incomplete_record(
                            exp_idx,
                            exp_seg,
                            exp_out,
                            exp_retries,
                            status="failed",
                            reason=_extraction_error_reason(exp_out),
                        )
                        exp_seg.clear()
                        if 0 <= exp_idx < len(segments):
                            segments[exp_idx] = []
                    if not deferred_by_idx:
                        break

                    raw_interval = min(
                        max_interval_s, min_interval_s * (2 ** min(defer_pass, 1))
                    )
                    earliest_seg_dl = min(
                        seg_deadline_by_idx.get(
                            idx, defer_start_mono + eff_segment_deadline_s
                        )
                        for idx in deferred_by_idx
                    )
                    remaining_s = min(deadline, earliest_seg_dl) - _defer_now()
                    if remaining_s <= 0.0:
                        if _defer_now() >= earliest_seg_dl:
                            continue
                        break
                    sleep_interval = min(raw_interval, remaining_s)
                    waited_s = max(0.0, _defer_now() - defer_start_mono)
                    logger.info(
                        "Waiting %.1fs before retrying %d deferred in_flight segment(s) (waited=%.1f/%.1fs)",
                        sleep_interval,
                        len(deferred_by_idx),
                        waited_s,
                        max_defer_wait_s,
                    )
                    _flush_log_handlers()
                    await sleep_fn(sleep_interval)
                    defer_pass += 1
                    if _defer_now() >= deadline:
                        break

                    for seg_idx, (seg, prev_outcome, prev_retries) in sorted(
                        list(deferred_by_idx.items())
                    ):
                        if _defer_now() >= deadline:
                            break
                        seg_dl = seg_deadline_by_idx.get(
                            seg_idx, defer_start_mono + eff_segment_deadline_s
                        )
                        if _defer_now() >= seg_dl:
                            deferred_by_idx.pop(seg_idx, None)
                            prev_outcome.in_flight = False
                            prev_outcome.status = "failed"
                            prev_outcome.error_kind = "segment_deadline_exceeded"
                            prev_outcome.error = (
                                f"segment_deadline_exceeded ({eff_segment_deadline_s:.1f}s)"
                            )
                            incomplete_by_idx[seg_idx] = _make_incomplete_record(
                                seg_idx,
                                seg,
                                prev_outcome,
                                prev_retries,
                                status="failed",
                                reason=_extraction_error_reason(prev_outcome),
                            )
                            seg.clear()
                            if 0 <= seg_idx < len(segments):
                                segments[seg_idx] = []
                            seg = []
                            continue
                        async with sem:
                            if _defer_now() >= deadline:
                                break
                            if _defer_now() >= seg_dl:
                                deferred_by_idx.pop(seg_idx, None)
                                prev_outcome.in_flight = False
                                prev_outcome.status = "failed"
                                prev_outcome.error_kind = "segment_deadline_exceeded"
                                prev_outcome.error = (
                                    f"segment_deadline_exceeded ({eff_segment_deadline_s:.1f}s)"
                                )
                                incomplete_by_idx[seg_idx] = _make_incomplete_record(
                                    seg_idx,
                                    seg,
                                    prev_outcome,
                                    prev_retries,
                                    status="failed",
                                    reason=_extraction_error_reason(prev_outcome),
                                )
                                seg.clear()
                                if 0 <= seg_idx < len(segments):
                                    segments[seg_idx] = []
                                seg = []
                                continue
                            outcome, rl_retries, elapsed_s = await _run_segment_attempt(
                                seg,
                                seg_deadline=seg_dl,
                                deadline=deadline,
                                seg_idx=seg_idx,
                            )
                            seg_dl = seg_deadline_by_idx.get(seg_idx, seg_dl)

                        outcome.input_tokens += prev_outcome.input_tokens
                        outcome.output_tokens += prev_outcome.output_tokens
                        outcome.total_tokens += prev_outcome.total_tokens
                        total_seg_retries = prev_retries + rl_retries
                        if outcome.permanent_quota_error:
                            abort_flag = True
                            aborted_in_tokens += int(outcome.input_tokens or 0)
                            aborted_out_tokens += int(outcome.output_tokens or 0)
                            aborted_tot_tokens += int(outcome.total_tokens or 0)
                            raise PermanentQuotaError(
                                outcome.error or "permanent_quota_error"
                            )
                        if outcome.ok:
                            deferred_by_idx.pop(seg_idx, None)
                            seg_tokens = outcome.total_tokens or (
                                outcome.input_tokens + outcome.output_tokens
                            )
                            logger.info(
                                "Segment %d/%d completed after deferred retry: tokens=%d (in=%d, out=%d) elapsed=%.2fs status=%s",
                                seg_idx + 1,
                                total_segments,
                                seg_tokens,
                                outcome.input_tokens,
                                outcome.output_tokens,
                                elapsed_s,
                                "discarded" if outcome.discarded_segment else "extracted",
                            )
                            _flush_log_handlers()
                            completed_by_idx[seg_idx] = (seg, outcome, total_seg_retries)
                            seg = []
                            await asyncio.shield(_flush_ready_prefix())
                        elif is_in_flight_error(outcome) and _defer_now() < seg_dl:
                            deferred_by_idx[seg_idx] = (seg, outcome, total_seg_retries)
                            seg = []
                        else:
                            deferred_by_idx.pop(seg_idx, None)
                            if is_in_flight_error(outcome) and _defer_now() >= seg_dl:
                                outcome.in_flight = False
                                outcome.status = "failed"
                                outcome.error_kind = "segment_deadline_exceeded"
                                outcome.error = (
                                    f"segment_deadline_exceeded ({eff_segment_deadline_s:.1f}s)"
                                )
                            fail_reason = (
                                (outcome.error_kind or "transient_error")
                                if is_transient_network_error(outcome)
                                else _extraction_error_reason(outcome)
                            )
                            incomplete_by_idx[seg_idx] = _make_incomplete_record(
                                seg_idx,
                                seg,
                                outcome,
                                total_seg_retries,
                                status="failed",
                                reason=fail_reason,
                            )
                            seg.clear()
                            if 0 <= seg_idx < len(segments):
                                segments[seg_idx] = []
                            seg = []

                if deferred_by_idx:
                    final_waited_s = max(0.0, _defer_now() - defer_start_mono)
                    for seg_idx, (seg, last_outcome, total_seg_retries) in sorted(
                        deferred_by_idx.items()
                    ):
                        seg_dl = seg_deadline_by_idx.get(
                            seg_idx, defer_start_mono + eff_segment_deadline_s
                        )
                        if _defer_now() >= seg_dl and seg_dl <= deadline:
                            last_outcome.in_flight = False
                            last_outcome.status = "failed"
                            last_outcome.error_kind = "segment_deadline_exceeded"
                            last_outcome.error = (
                                f"segment_deadline_exceeded ({eff_segment_deadline_s:.1f}s)"
                            )
                            rem_reason = _extraction_error_reason(last_outcome)
                        else:
                            rem_reason = "in_flight_timeout"
                        incomplete_by_idx[seg_idx] = _make_incomplete_record(
                            seg_idx,
                            seg,
                            last_outcome,
                            total_seg_retries,
                            status="failed",
                            reason=rem_reason,
                        )
                        seg.clear()
                        if 0 <= seg_idx < len(segments):
                            segments[seg_idx] = []
                        logger.warning(
                            "Segment %d/%d still in_flight after %.1fs deferred retries; marking incomplete (prefix cursor held at %d)",
                            seg_idx + 1,
                            total_segments,
                            final_waited_s,
                            next_to_flush,
                        )
                    _flush_log_handlers()
                    deferred_by_idx.clear()

                await asyncio.shield(_flush_ready_prefix())

        for seg_idx, (seg, outcome, rl_retries) in sorted(completed_by_idx.items()):
            cards_flagged_unsupported_number += int(
                getattr(outcome, "cards_flagged_unsupported_number", 0)
            )
            cards_flagged_unsupported_names += int(
                getattr(outcome, "cards_flagged_unsupported_names", 0)
            )
            cards_flagged_unsupported_conclusion += int(
                getattr(outcome, "cards_flagged_unsupported_conclusion", 0)
            )
            soft_unsupported_numbers += int(
                getattr(outcome, "soft_unsupported_numbers", 0)
            )
            figures_dropped += int(getattr(outcome, "figures_dropped", 0))
            periods_cleared += int(getattr(outcome, "periods_cleared", 0))
            currencies_stripped += int(getattr(outcome, "currencies_stripped", 0))
            tickers_dropped += int(getattr(outcome, "tickers_dropped", 0))
            tickers_dropped_private += int(getattr(outcome, "tickers_dropped_private", 0))
            tickers_dropped_channel += int(getattr(outcome, "tickers_dropped_channel", 0))
            tickers_backfilled += int(getattr(outcome, "tickers_backfilled", 0))
            types_corrected += int(getattr(outcome, "types_corrected", 0))
            incomplete_by_idx[seg_idx] = _make_incomplete_record(
                seg_idx,
                seg,
                outcome,
                rl_retries,
                status="blocked",
                reason="blocked_by_incomplete_prefix",
            )
            seg.clear()
            outcome.cards.clear()

        for inc_rec in incomplete_by_idx.values():
            input_tokens += int(inc_rec.get("input_tokens", 0))
            output_tokens += int(inc_rec.get("output_tokens", 0))
            total_tokens += int(inc_rec.get("total_tokens", 0))
            rate_limit_retries += int(inc_rec.get("retries", 0))
        incomplete_usage_rolled = True

        tasks.clear()
        completed_by_idx.clear()
        for rem_seg in segments:
            rem_seg.clear()
        segments.clear()

        rendered_summary: dict[str, Any] = {"company_files": [], "topic_files": []}
        if not dry_run:
            unrendered_dates = await db_conv.list_unrendered_dates(conn=conn)
            touched_dates = sorted(set(unrendered_dates) | accepted_dates)
            if touched_dates:
                date_cards = await db_conv.list_cards_for_dates(
                    touched_dates, active_only=False, conn=conn
                )
                raw_rendered = await render_and_write_conversation_digests(
                    date_cards,
                    target_dates=touched_dates,
                    user_id=archive_owner_user_id(),
                    backend_by_ticker=backend_by_ticker,
                    home_backend=home_backend,
                    conn=conn,
                )
                del date_cards
                rendered_dates = raw_rendered.get("rendered_dates")
                if rendered_dates is None:
                    rendered_dates = list(touched_dates)
                if rendered_dates:
                    await db_conv.mark_dates_rendered(
                        rendered_dates,
                        card_locations=raw_rendered.get("card_locations"),
                        conn=conn,
                    )
                rendered_summary = _compact_rendered_summary(raw_rendered)
                del raw_rendered

        incomplete_segments = [incomplete_by_idx[k] for k in sorted(incomplete_by_idx)]
        finish_status = "failed" if incomplete_segments else "completed"
        finish_error = (
            f"incomplete_segments={len(incomplete_segments)}"
            if incomplete_segments
            else None
        )
        maxrss_raw, maxrss_mib = _get_maxrss_stats()
        logger.info(
            "Weekly batch %s..%s finished: segments_processed=%d/%d incomplete=%d cards_inserted=%d maxrss=%d (%.2f MiB)",
            window_start.isoformat(),
            window_end.isoformat(),
            segments_flushed,
            total_segments,
            len(incomplete_segments),
            cards_accepted if dry_run else cards_inserted,
            maxrss_raw,
            maxrss_mib,
        )
        _flush_log_handlers()

        batch_summary = {
            "run_id": run_id,
            "status": finish_status,
            "error_text": finish_error,
            "mode": mode,
            "dry_run": dry_run,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "extractor_model": extractor_model,
            "embedding_model": embedding_model_used,
            "events_read": total_events_read,
            "segments_processed": segments_flushed,
            "segments_discarded": segments_discarded,
            "cards_flagged_unsupported_number": cards_flagged_unsupported_number,
            "cards_flagged_unsupported_names": cards_flagged_unsupported_names,
            "cards_flagged_unsupported_conclusion": cards_flagged_unsupported_conclusion,
            "soft_unsupported_numbers": soft_unsupported_numbers,
            "figures_dropped": figures_dropped,
            "periods_cleared": periods_cleared,
            "currencies_stripped": currencies_stripped,
            "tickers_dropped": tickers_dropped,
            "tickers_dropped_private": tickers_dropped_private,
            "tickers_dropped_channel": tickers_dropped_channel,
            "tickers_backfilled": tickers_backfilled,
            "types_corrected": types_corrected,
            "next_to_flush": next_to_flush,
            "incomplete_segments_count": len(incomplete_segments),
            "incomplete_segments": incomplete_segments,
            "cards_extracted": cards_extracted,
            "cards_accepted": cards_accepted,
            "cards_inserted": cards_accepted if dry_run else cards_inserted,
            "cards_deduped": deduped_count,
            "cards_superseded": superseded_count,
            "quotes_rejected": quotes_rejected,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "rate_limit_retries": rate_limit_retries,
            "maxrss": maxrss_raw,
            "maxrss_mib": maxrss_mib,
            "rendered": rendered_summary,
            "processed_chunk_keys": list(processed_chunk_keys),
        }

        if not dry_run:
            await db_conv.update_run_finish(
                run_id,
                status=finish_status,
                embedding_model=embedding_model_used,
                events_read=total_events_read,
                segments_processed=segments_flushed,
                cards_extracted=cards_extracted,
                cards_inserted=cards_inserted,
                cards_deduped=deduped_count,
                cards_superseded=superseded_count,
                quotes_rejected=quotes_rejected,
                segments_discarded=segments_discarded,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                stats={
                    "rate_limit_retries": rate_limit_retries,
                    "incomplete_segments_count": len(incomplete_segments),
                    "incomplete_segments": incomplete_segments,
                    "cards_flagged_unsupported_number": cards_flagged_unsupported_number,
                    "cards_flagged_unsupported_names": cards_flagged_unsupported_names,
                    "cards_flagged_unsupported_conclusion": cards_flagged_unsupported_conclusion,
                    "soft_unsupported_numbers": soft_unsupported_numbers,
                    "figures_dropped": figures_dropped,
                    "periods_cleared": periods_cleared,
                    "currencies_stripped": currencies_stripped,
                    "tickers_dropped": tickers_dropped,
                    "tickers_dropped_private": tickers_dropped_private,
                    "tickers_dropped_channel": tickers_dropped_channel,
                    "tickers_backfilled": tickers_backfilled,
                    "types_corrected": types_corrected,
                    "maxrss": maxrss_raw,
                    "rendered": rendered_summary,
                },
                error_text=finish_error,
                conn=conn,
            )

        return batch_summary

    except BaseException as exc:
        abort_flag = True
        pending_tasks = [t for t in tasks if not t.done()]
        for t in pending_tasks:
            t.cancel()
        if pending_tasks:
            try:
                await asyncio.shield(asyncio.gather(*pending_tasks, return_exceptions=True))
            except BaseException:
                pass
        if not flush_failed:
            try:
                await asyncio.shield(_flush_ready_prefix())
            except BaseException:
                pass

        for _seg, rem_outcome, _rl_retries in completed_by_idx.values():
            cards_flagged_unsupported_number += int(
                getattr(rem_outcome, "cards_flagged_unsupported_number", 0)
            )
            cards_flagged_unsupported_names += int(
                getattr(rem_outcome, "cards_flagged_unsupported_names", 0)
            )
            cards_flagged_unsupported_conclusion += int(
                getattr(rem_outcome, "cards_flagged_unsupported_conclusion", 0)
            )
            soft_unsupported_numbers += int(
                getattr(rem_outcome, "soft_unsupported_numbers", 0)
            )
            figures_dropped += int(getattr(rem_outcome, "figures_dropped", 0))
            periods_cleared += int(getattr(rem_outcome, "periods_cleared", 0))
            currencies_stripped += int(getattr(rem_outcome, "currencies_stripped", 0))
            tickers_dropped += int(getattr(rem_outcome, "tickers_dropped", 0))
            tickers_dropped_private += int(getattr(rem_outcome, "tickers_dropped_private", 0))
            tickers_dropped_channel += int(getattr(rem_outcome, "tickers_dropped_channel", 0))
            tickers_backfilled += int(getattr(rem_outcome, "tickers_backfilled", 0))
            types_corrected += int(getattr(rem_outcome, "types_corrected", 0))

        # Cancelled attempts were settled into the hourly budget whether or not
        # the flush failed, so the failed run's summary carries them too.
        input_tokens += aborted_in_tokens
        output_tokens += aborted_out_tokens
        total_tokens += aborted_tot_tokens
        if not flush_failed:
            for _seg, rem_outcome, _rl_retries in completed_by_idx.values():
                input_tokens += int(getattr(rem_outcome, "input_tokens", 0))
                output_tokens += int(getattr(rem_outcome, "output_tokens", 0))
                total_tokens += int(getattr(rem_outcome, "total_tokens", 0))
            for _seg, def_outcome, _def_retries in deferred_by_idx.values():
                input_tokens += int(getattr(def_outcome, "input_tokens", 0))
                output_tokens += int(getattr(def_outcome, "output_tokens", 0))
                total_tokens += int(getattr(def_outcome, "total_tokens", 0))
        if not incomplete_usage_rolled:
            for inc_rec in incomplete_by_idx.values():
                input_tokens += int(inc_rec.get("input_tokens", 0))
                output_tokens += int(inc_rec.get("output_tokens", 0))
                total_tokens += int(inc_rec.get("total_tokens", 0))

        if not dry_run:
            is_interrupt = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
            run_status = "interrupted" if is_interrupt else "failed"
            try:
                await asyncio.shield(
                    db_conv.update_run_finish(
                        run_id,
                        status=run_status,
                        embedding_model=embedding_model_used,
                        events_read=total_events_read,
                        segments_processed=segments_flushed,
                        cards_extracted=cards_extracted,
                        cards_inserted=cards_inserted,
                        cards_deduped=deduped_count,
                        cards_superseded=superseded_count,
                        quotes_rejected=quotes_rejected,
                        segments_discarded=segments_discarded,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        total_tokens=total_tokens,
                        stats={
                            "rate_limit_retries": rate_limit_retries,
                            "segments_total": total_segments,
                            "cards_flagged_unsupported_number": cards_flagged_unsupported_number,
                            "cards_flagged_unsupported_names": cards_flagged_unsupported_names,
                            "cards_flagged_unsupported_conclusion": cards_flagged_unsupported_conclusion,
                            "soft_unsupported_numbers": soft_unsupported_numbers,
                            "figures_dropped": figures_dropped,
                            "periods_cleared": periods_cleared,
                            "currencies_stripped": currencies_stripped,
                            "tickers_dropped": tickers_dropped,
                            "tickers_dropped_private": tickers_dropped_private,
                            "tickers_dropped_channel": tickers_dropped_channel,
                            "tickers_backfilled": tickers_backfilled,
                            "types_corrected": types_corrected,
                        },
                        error_text=format_safe_run_error(exc),
                        conn=conn,
                    )
                )
            except BaseException:
                pass
        raise


async def run_archive_pipeline(
    *,
    mode: str = "run",
    since_date: date | None = None,
    until_date: date | None = None,
    dry_run: bool = False,
    inbox_file: Path | None = None,
    inbox_dir: Path = DEFAULT_INBOX_DIR,
    cursors_file: Path | None = None,
    export_cursors_file: Path | None = None,
    ocw_agents_root: Path = DEFAULT_OCW_AGENTS_ROOT,
    agents: list[str] | tuple[str, ...] = DEFAULT_AGENTS,
    extractor_model: str | None = None,
    extractor_fallback_model: str | None = None,
    segment_deadline_s: float | None = None,
    hourly_token_budget: int | None = None,
    extract_concurrency: int | None = None,
    concurrency: int | None = None,
    stale_after_s: int = DEFAULT_STALE_AFTER_S,
    defer_retry_min_s: float = DEFAULT_DEFER_RETRY_MIN_S,
    defer_retry_max_s: float = DEFAULT_DEFER_RETRY_MAX_S,
    skip_host_export: bool = False,
    llm_service: Any = None,
    extract_fn: Callable[..., Coroutine[Any, Any, SegmentExtractionOutcome]] = extract_segment_cards,
    embed_fn: Callable[..., Coroutine[Any, Any, tuple[list[list[float]], str]]] = embed_texts_via_bridge,
    sleep_fn: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
    clock_fn: Callable[[], float] | None = None,
    backend_by_ticker: dict[str, Any] | None = None,
    home_backend: Any = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Execute the end-to-end conversation archive pipeline (`run` or `backfill`)."""
    cfg = get_dalton_settings().conversations
    today_et = datetime.now(ET_TZ).date()
    eff_since = since_date or (today_et - timedelta(days=int(cfg.default_backfill_days or 14)))
    eff_until = until_date or today_et
    eff_model = (extractor_model or "").strip()
    if not eff_model:
        eff_model = (await resolve_dalton_extract_model(archive_billing_user_id())).strip()
    eff_budget = int(hourly_token_budget or cfg.hourly_token_budget)
    eff_concurrency = max(
        1,
        int(concurrency or extract_concurrency or getattr(cfg, "extract_concurrency", 1) or 1),
    )
    eff_export_cursors = (
        export_cursors_file
        or cursors_file
        or Path(cfg.export_cursors_file or DEFAULT_EXPORT_CURSORS_FILE)
    )
    is_backfill = mode == "backfill"

    lock_conn: Any = None
    lock_acquired = False
    abandoned_runs_count = 0

    try:
        if not dry_run:
            open_sess_fn = (
                open_session_connection
                if open_session_connection is not _orig_open_session_connection
                else _db_pool_mod.open_session_connection
            )
            lock_conn = await open_sess_fn()
            lock_acquired = await db_conv.try_acquire_archive_run_lock(conn=lock_conn)
            if not lock_acquired:
                msg = "已有归档在运行"
                logger.error(msg)
                _flush_log_handlers()
                print(msg, file=sys.stderr, flush=True)
                raise ArchiveAlreadyRunningError(msg, code=2)
            abandoned_runs_count = await db_conv.mark_stale_running_runs_abandoned(conn=conn)
            if abandoned_runs_count > 0:
                logger.warning(
                    "Marked %d stale running row(s) in dalton_conversation_runs as abandoned",
                    abandoned_runs_count,
                )
                _flush_log_handlers()

        export_summary: dict[str, Any] = {}
        pending_inbox_files: list[Path] = []
        deferred_export_cursors: dict[tuple[str, str], SessionCursor] | None = None

        if inbox_file is not None:
            if inbox_file.is_file():
                pending_inbox_files = [inbox_file]
            export_summary = {"inbox_path": str(inbox_file)}
        else:
            if not skip_host_export and ocw_agents_root.is_dir():
                exp_res = run_export(
                    agents=agents,
                    ocw_agents_root=ocw_agents_root,
                    inbox_dir=inbox_dir,
                    cursors_file=eff_export_cursors,
                    since_date=eff_since,
                    until_date=eff_until,
                    persist_cursors=False,
                    ignore_cursors=is_backfill,
                )
                if not dry_run and not is_backfill:
                    deferred_export_cursors = exp_res.updated_cursors
                export_summary = {
                    "run_id": exp_res.run_id,
                    "inbox_path": str(exp_res.inbox_path),
                    "events_scanned": exp_res.events_scanned,
                    "events_exported": exp_res.events_exported,
                    "events_skipped_ops": exp_res.events_skipped_ops,
                    "events_skipped_chitchat": exp_res.events_skipped_chitchat,
                    "events_skipped_transcript": exp_res.events_skipped_transcript,
                    "events_parse_failed": exp_res.events_parse_failed,
                    "secrets_redacted": exp_res.secrets_redacted,
                }
                if is_backfill:
                    if exp_res.inbox_path.is_file():
                        pending_inbox_files = [exp_res.inbox_path]
                else:
                    pending_inbox_files = sorted(inbox_dir.glob("*.jsonl")) if inbox_dir.is_dir() else []
            else:
                pending_inbox_files = sorted(inbox_dir.glob("*.jsonl")) if inbox_dir.is_dir() else []

        limiter_kwargs: dict[str, Any] = {"sleep_fn": sleep_fn}
        if clock_fn is not None:
            limiter_kwargs["clock_fn"] = clock_fn
        limiter = HourlyTokenBudgetLimiter(
            eff_budget,
            **limiter_kwargs,
        )

        shared_llm_service = llm_service
        if shared_llm_service is None and not dry_run and extract_fn is extract_segment_cards:
            try:
                from ptc_agent.config import load_from_files
                from src.server.services.llm.service import LLMService

                agent_cfg = await load_from_files()
                if getattr(agent_cfg, "llm", None) is not None and hasattr(agent_cfg.llm, "fallback"):
                    agent_cfg.llm.fallback = []
                shared_llm_service = LLMService(agent_config=agent_cfg, logger=logger)
            except Exception as exc:
                logger.debug("Lazy shared LLMService init skipped: %s", exc)

        batch_results: list[dict[str, Any]] = []
        all_incomplete_segments: list[dict[str, Any]] = []
        events_total = 0
        files_consumed: list[str] = []

        for f_path in pending_inbox_files:
            raw_events = read_inbox_jsonl(f_path)
            # Consumer does NOT apply a secondary date-window filter: date window is applied only at export time.
            # Expand oversized events into deterministic chunks so per-chunk progress is tracked in `dalton_conversation_processed`.
            file_chunks = expand_event_chunks(raw_events, max_segment_chars=cfg.max_segment_chars)
            all_file_chunk_keys = sorted({_chunk_key(ch) for ch in file_chunks})

            persisted_plans: dict[tuple[str, str, str], dict[str, Any]] = {}
            processed_keys: set[tuple[str, str, str, int]] = (
                set(
                    await db_conv.fetch_processed_event_chunks(
                        all_file_chunk_keys,
                        conn=conn,
                        out_plans=persisted_plans,
                    )
                )
                if (not dry_run and all_file_chunk_keys)
                else set()
            )
            if persisted_plans:
                file_chunks = expand_event_chunks(
                    raw_events,
                    max_segment_chars=cfg.max_segment_chars,
                    persisted_plans=persisted_plans,
                )
                all_file_chunk_keys = sorted({_chunk_key(ch) for ch in file_chunks})
            raw_events.clear()
            del raw_events

            unprocessed_chunks: list[dict[str, Any]] = []
            rule_discarded_rows: list[dict[str, Any]] = []
            for ch in file_chunks:
                ckey = _chunk_key(ch)
                if ckey in processed_keys:
                    continue
                skip_rule, _reason = is_operational_or_chitchat(str(ch.get("text") or ""))
                if skip_rule:
                    rule_discarded_rows.append(_make_processed_row(ch, "discarded"))
                    continue
                unprocessed_chunks.append(ch)
            file_chunks.clear()
            del file_chunks

            if not dry_run and rule_discarded_rows:
                await db_conv.record_processed_event_chunks(rule_discarded_rows, conn=conn)
                for r in rule_discarded_rows:
                    processed_keys.add((r["agent"], r["session_id"], r["event_id"], int(r["chunk_idx"])))
            rule_discarded_rows.clear()
            del rule_discarded_rows

            events_total += len(unprocessed_chunks)
            weekly_batches = group_events_by_weekly_window(
                unprocessed_chunks,
                since_date=eff_since,
                until_date=eff_until,
            )
            unprocessed_chunks.clear()
            del unprocessed_chunks

            while weekly_batches:
                w_start, w_end, b_events = weekly_batches.pop(0)
                b_summary = await process_weekly_batch(
                    b_events,
                    mode=mode,
                    window_start=w_start,
                    window_end=w_end,
                    limiter=limiter,
                    extractor_model=eff_model,
                    prompt_version=cfg.prompt_version,
                    extractor_fallback_model=extractor_fallback_model,
                    segment_deadline_s=segment_deadline_s,
                    dry_run=dry_run,
                    initial_backoff_s=cfg.rate_limit_initial_backoff_seconds,
                    max_backoff_s=cfg.rate_limit_max_backoff_seconds,
                    stale_after_s=stale_after_s,
                    defer_retry_min_s=defer_retry_min_s,
                    defer_retry_max_s=defer_retry_max_s,
                    extract_concurrency=eff_concurrency,
                    llm_service=shared_llm_service,
                    extract_fn=extract_fn,
                    embed_fn=embed_fn,
                    sleep_fn=sleep_fn,
                    clock_fn=clock_fn,
                    backend_by_ticker=backend_by_ticker,
                    home_backend=home_backend,
                    conn=conn,
                )
                b_events.clear()
                del b_events
                for k in b_summary.pop("processed_chunk_keys", None) or []:
                    processed_keys.add(tuple(k))
                for inc_seg in b_summary.get("incomplete_segments") or []:
                    all_incomplete_segments.append(dict(inc_seg))
                batch_results.append(b_summary)

            if not dry_run and set(all_file_chunk_keys).issubset(processed_keys):
                f_path.unlink(missing_ok=True)
                files_consumed.append(f_path.name)

        if not dry_run and not is_backfill and deferred_export_cursors is not None and not all_incomplete_segments:
            save_cursors_file(deferred_export_cursors, eff_export_cursors)

        # Retry rendering for any previously stored cards whose digest files were not yet written
        rendered_retry_summary: dict[str, Any] | None = None
        if not dry_run:
            pending_dates = await db_conv.list_unrendered_dates(conn=conn)
            if pending_dates:
                date_cards = await db_conv.list_cards_for_dates(pending_dates, active_only=False, conn=conn)
                raw_retry_rendered = await render_and_write_conversation_digests(
                    date_cards,
                    target_dates=pending_dates,
                    user_id=archive_owner_user_id(),
                    backend_by_ticker=backend_by_ticker,
                    home_backend=home_backend,
                    conn=conn,
                )
                del date_cards
                retry_rendered_dates = raw_retry_rendered.get("rendered_dates")
                if retry_rendered_dates is None:
                    retry_rendered_dates = list(pending_dates)
                if retry_rendered_dates:
                    await db_conv.mark_dates_rendered(
                        retry_rendered_dates,
                        card_locations=raw_retry_rendered.get("card_locations"),
                        conn=conn,
                    )
                rendered_retry_summary = _compact_rendered_summary(raw_retry_rendered)
                del raw_retry_rendered

        has_incomplete = len(all_incomplete_segments) > 0
        return {
            "ok": not has_incomplete,
            "exit_code": EXIT_CODE_INCOMPLETE if has_incomplete else EXIT_CODE_OK,
            "mode": mode,
            "dry_run": dry_run,
            "since": eff_since.isoformat(),
            "until": eff_until.isoformat(),
            "extractor_model": eff_model,
            "hourly_token_budget": eff_budget,
            "extract_concurrency": eff_concurrency,
            "abandoned_runs_marked": abandoned_runs_count,
            "export": export_summary,
            "inbox_files_consumed": files_consumed,
            "rendered_retry": rendered_retry_summary,
            "batches_count": len(batch_results),
            "events_total": events_total,
            "incomplete_segments_total": len(all_incomplete_segments),
            "incomplete_segments": all_incomplete_segments,
            "cards_inserted_total": sum(int(b.get("cards_inserted", 0)) for b in batch_results),
            "cards_deduped_total": sum(int(b.get("cards_deduped", 0)) for b in batch_results),
            "cards_superseded_total": sum(int(b.get("cards_superseded", 0)) for b in batch_results),
            "quotes_rejected_total": sum(int(b.get("quotes_rejected", 0)) for b in batch_results),
            "cards_flagged_unsupported_number_total": sum(
                int(b.get("cards_flagged_unsupported_number", 0)) for b in batch_results
            ),
            "cards_flagged_unsupported_names_total": sum(
                int(b.get("cards_flagged_unsupported_names", 0)) for b in batch_results
            ),
            "cards_flagged_unsupported_conclusion_total": sum(
                int(b.get("cards_flagged_unsupported_conclusion", 0)) for b in batch_results
            ),
            "soft_unsupported_numbers_total": sum(
                int(b.get("soft_unsupported_numbers", 0)) for b in batch_results
            ),
            "figures_dropped_total": sum(int(b.get("figures_dropped", 0)) for b in batch_results),
            "periods_cleared_total": sum(int(b.get("periods_cleared", 0)) for b in batch_results),
            "currencies_stripped_total": sum(
                int(b.get("currencies_stripped", 0)) for b in batch_results
            ),
            "tickers_dropped_total": sum(int(b.get("tickers_dropped", 0)) for b in batch_results),
            "tickers_dropped_private_total": sum(
                int(b.get("tickers_dropped_private", 0)) for b in batch_results
            ),
            "tickers_dropped_channel_total": sum(
                int(b.get("tickers_dropped_channel", 0)) for b in batch_results
            ),
            "tickers_backfilled_total": sum(
                int(b.get("tickers_backfilled", 0)) for b in batch_results
            ),
            "types_corrected_total": sum(
                int(b.get("types_corrected", 0)) for b in batch_results
            ),
            "total_tokens": sum(int(b.get("total_tokens", 0)) for b in batch_results),
            "batches": batch_results,
        }
    finally:
        if lock_acquired and lock_conn is not None:
            try:
                await asyncio.shield(db_conv.release_archive_run_lock(conn=lock_conn))
            except BaseException:
                pass
        if lock_conn is not None:
            try:
                await asyncio.shield(lock_conn.close())
            except BaseException:
                pass


EXIT_CODE_EPILOG = """Exit codes:
  0  All segments completed
  2  Another archive run holds the Postgres advisory lock
  3  One or more segments remained incomplete (safe to re-run)
  1  Fatal error (e.g. PermanentQuotaError)
"""


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser supporting `run`, `backfill --since YYYY-MM-DD`, and `--dry-run`."""
    default_since = (datetime.now(ET_TZ).date() - timedelta(days=14)).isoformat()

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="Do not write to Postgres or workspaces")
    common.add_argument("--since", default=None, help=f"Start date YYYY-MM-DD in ET (default: {default_since})")
    common.add_argument("--until", default=None, help="End date YYYY-MM-DD in ET (default: today ET)")
    common.add_argument("--inbox-file", type=Path, default=None, help="Specific exported inbox .jsonl file")
    common.add_argument("--inbox-dir", type=Path, default=DEFAULT_INBOX_DIR)
    common.add_argument("--cursors-file", type=Path, default=DEFAULT_CURSORS_FILE)
    common.add_argument("--ocw-agents-root", type=Path, default=DEFAULT_OCW_AGENTS_ROOT)
    common.add_argument("--agents", default=",".join(DEFAULT_AGENTS))
    common.add_argument("--model", default=None, help="Override extractor LLM model key")
    common.add_argument("--hourly-token-budget", type=int, default=None, help="Override hourly token budget")
    common.add_argument("--concurrency", type=int, default=None, help="Override segment extraction concurrency")
    common.add_argument("--skip-host-export", action="store_true", help="Process existing inbox JSONL only")

    parser = argparse.ArgumentParser(
        description="Dalton OpenClaw Conversation Archive CLI (run / backfill)",
        epilog=EXIT_CODE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="subcommand")
    subparsers.add_parser(
        "run",
        parents=[common],
        help="Run incremental daily conversation extraction",
        epilog=EXIT_CODE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers.add_parser(
        "backfill",
        parents=[common],
        help="Backfill historical conversations in weekly batches with token budget pacing",
        epilog=EXIT_CODE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    return parser


async def _async_main(
    args: argparse.Namespace,
    *,
    on_finally: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    mode = args.subcommand or "run"
    today_et = datetime.now(ET_TZ).date()
    cfg = get_dalton_settings().conversations
    default_days = int(cfg.default_backfill_days or 14)
    since_d = (
        date.fromisoformat(args.since)
        if args.since
        else (today_et - timedelta(days=default_days))
    )
    until_d = date.fromisoformat(args.until) if args.until else today_et
    agents = [a.strip() for a in str(args.agents).split(",") if a.strip()]
    dry_run = bool(args.dry_run)
    concurrency = getattr(args, "concurrency", None)

    db_pool = None
    if not dry_run:
        db_pool = get_or_create_pool()
        await db_pool.open(wait=True)

    try:
        kwargs: dict[str, Any] = {
            "mode": mode,
            "since_date": since_d,
            "until_date": until_d,
            "dry_run": dry_run,
            "inbox_file": args.inbox_file,
            "inbox_dir": args.inbox_dir,
            "cursors_file": args.cursors_file,
            "ocw_agents_root": args.ocw_agents_root,
            "agents": agents,
            "extractor_model": args.model,
            "hourly_token_budget": args.hourly_token_budget,
            "skip_host_export": bool(args.skip_host_export),
        }
        if concurrency is not None:
            kwargs["concurrency"] = int(concurrency)
        return await run_archive_pipeline(**kwargs)
    finally:
        try:
            from src.server.services.workspace_manager import WorkspaceManager

            wm = WorkspaceManager.current()
            if wm is not None:
                await asyncio.wait_for(wm.shutdown(), timeout=POOL_CLOSE_TIMEOUT_S)
        except Exception:
            pass
        if db_pool is not None:
            try:
                await asyncio.wait_for(db_pool.close(), timeout=POOL_CLOSE_TIMEOUT_S)
            except Exception:
                logger.warning(
                    "Database connection pool did not close within %.1fs during shutdown; continuing exit",
                    POOL_CLOSE_TIMEOUT_S,
                )
                _flush_log_handlers()
        if on_finally is not None:
            res = on_finally()
            if inspect.isawaitable(res):
                await res


def main(
    argv: list[str] | None = None,
    *,
    exit_fn: Callable[[int], Any] | None = None,
    on_finally: Callable[[], Any] | None = None,
) -> int:
    configure_archive_logging()
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    _ABANDONED_EXTRACTION_TASKS.clear()
    loop = asyncio.new_event_loop()
    exit_code: int = EXIT_CODE_OK
    caught_exc: BaseException | None = None
    has_live_abandoned = False
    interrupted = False
    main_task: asyncio.Task[dict[str, Any]] = loop.create_task(
        _async_main(args, on_finally=on_finally)
    )
    prev_sigint_handler: Any = None
    sigint_installed = False

    grace_deadline: float | None = None

    def _cancel_once() -> None:
        nonlocal grace_deadline
        # One cancel only: a second one would interrupt the task's own cleanup
        # (run status, pool close). Its finally gets the grace period, then the
        # loop stops and the task is abandoned.
        if not main_task.done():
            main_task.cancel()
            grace_deadline = time.monotonic() + SHUTDOWN_GRACE_TIMEOUT_S
            loop.call_later(SHUTDOWN_GRACE_TIMEOUT_S, loop.stop)

    def _on_sigint(signum: int, frame: Any) -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            loop.call_soon_threadsafe(_cancel_once)

    if threading.current_thread() is threading.main_thread():
        try:
            prev_sigint_handler = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, _on_sigint)
            sigint_installed = True
        except ValueError:
            sigint_installed = False

    try:
        try:
            summary = loop.run_until_complete(main_task)
        except ArchiveAlreadyRunningError as exc:
            exit_code = int(exc.code if isinstance(exc.code, int) else EXIT_CODE_LOCK_HELD)
        except KeyboardInterrupt:
            interrupted = True
            exit_code = EXIT_CODE_FATAL
        except asyncio.CancelledError:
            interrupted = True
            exit_code = EXIT_CODE_FATAL
        except RuntimeError as exc:
            if interrupted and "Event loop stopped before Future completed" in str(exc):
                exit_code = EXIT_CODE_FATAL
            else:
                caught_exc = exc
                exit_code = EXIT_CODE_FATAL
        except BaseException as exc:
            caught_exc = exc
            exit_code = EXIT_CODE_FATAL
        else:
            if interrupted:
                exit_code = EXIT_CODE_FATAL
            else:
                print(json.dumps(summary, ensure_ascii=False, default=str), flush=True)
                if (
                    int(summary.get("incomplete_segments_total") or 0) > 0
                    or bool(summary.get("incomplete_segments"))
                    or summary.get("exit_code") == EXIT_CODE_INCOMPLETE
                ):
                    exit_code = EXIT_CODE_INCOMPLETE
                else:
                    exit_code = EXIT_CODE_OK

        if interrupted:
            # One grace period in total: what the stop timer already spent is
            # not granted again here.
            remaining = (
                SHUTDOWN_GRACE_TIMEOUT_S
                if grace_deadline is None
                else max(0.0, grace_deadline - time.monotonic())
            )
            if not main_task.done() and remaining > 0:
                try:
                    loop.run_until_complete(asyncio.wait({main_task}, timeout=remaining))
                except BaseException:
                    pass
            if not main_task.done():
                logger.warning(
                    "Main archive task did not finish cleanup within %.1fs after interrupt; abandoning task",
                    SHUTDOWN_GRACE_TIMEOUT_S,
                )
                _flush_log_handlers()
                _register_abandoned_task(main_task, "main_task")
            elif not main_task.cancelled():
                _ = main_task.exception()

        live_abandoned = _get_live_abandoned_segment_ids()
        if live_abandoned:
            has_live_abandoned = True
            logger.warning(
                "Archive run finished with %d abandoned extraction task(s) still alive (%s); forcing immediate exit with code %d",
                len(live_abandoned),
                ", ".join(live_abandoned),
                exit_code,
            )
            _flush_log_handlers()
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            eff_exit = exit_fn or os._exit
            eff_exit(exit_code)
            return exit_code
        if caught_exc is not None:
            raise caught_exc
        return exit_code
    finally:
        if sigint_installed and prev_sigint_handler is not None:
            try:
                signal.signal(signal.SIGINT, prev_sigint_handler)
            except Exception:
                pass
        if not has_live_abandoned:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()


if __name__ == "__main__":
    sys.exit(main())
