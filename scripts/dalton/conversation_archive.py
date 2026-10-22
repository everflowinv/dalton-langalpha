#!/usr/bin/env python3
"""Entry point for Dalton's OpenClaw conversation archive (`run` and `backfill`).

Subcommands:
- `run`: Daily incremental extraction and per-company/Home Markdown digest rendering.
- `backfill --since YYYY-MM-DD`: Historical backfill in 7-day weekly batches (Phase 1 default:
  `today_et - 14 days`), paced by an hourly token budget (`HourlyTokenBudgetLimiter`), with
  unbounded time-based exponential backoff on 429 / quota exhaustion (never drops a segment
  by retry count), interruptible and resumable via session cursors and `content_hash`.
- `--dry-run`: Run export, segmentation, and extraction/stats without writing to Postgres or workspaces.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import re
import sys
import time
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
    SegmentExtractionOutcome,
    deduplicate_candidate_cards,
    expand_event_chunks,
    extract_segment_cards,
    is_permanent_quota_error,
    is_rate_limit_or_quota_error,
    resolve_dedup_and_supersedes,
    segment_events,
)
from src.dalton.conversations.render import (  # noqa: E402
    archive_owner_user_id,
    render_and_write_conversation_digests,
)
from src.dalton.database import conversations as db_conv  # noqa: E402
from src.dalton.settings import get_dalton_settings  # noqa: E402
from src.server.database import pool as _db_pool_mod  # noqa: E402
from src.server.database.pool import get_or_create_pool, open_session_connection  # noqa: E402

_orig_open_session_connection = open_session_connection

logger = logging.getLogger("dalton.conversation_archive")

ET_TZ = ZoneInfo("America/New_York")
DEFAULT_RESERVED_OUTPUT_TOKENS = 1024


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

    __slots__ = ("reservation_id", "estimated_tokens", "waited_seconds", "settled")

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

    async def wait_for_rate_limit_pause(self) -> float:
        """Wait if another coroutine is currently in a 429 rate-limit backoff window."""
        slept = 0.0
        while True:
            now = self._now()
            target_until = self._rate_limit_pause_until
            if target_until <= now + 1e-6:
                if self._rate_limit_pause_until > 0.0:
                    self._rate_limit_pause_until = 0.0
                    self._rate_limit_clear_event.set()
                break
            wait_s = max(0.01, target_until - now)
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
        return slept

    async def wait_for_budget(self, estimated_tokens: int = 1000) -> BudgetReservation:
        """Wait until the sliding 1-hour window has capacity for `estimated_tokens` and pre-reserve it by ID.

        Raises `PermanentQuotaError` if a single request's `estimated_tokens` exceeds `hourly_budget`.
        """
        slept = 0.0
        est = max(1, int(estimated_tokens))
        if est > self.hourly_budget:
            raise PermanentQuotaError(
                f"Estimated request tokens ({est}) exceed hourly_budget ({self.hourly_budget})"
            )
        async with self._lock:
            while True:
                slept += await self.wait_for_rate_limit_pause()
                now = self._now()
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
                wait_s = max(0.05, (target_ts + self.window_seconds) - now)
                logger.info(
                    "Hourly token budget reached (used=%d, reserved=%d, est=%d, budget=%d); sleeping %.1fs",
                    used_settled,
                    used_reserved,
                    est,
                    self.hourly_budget,
                    wait_s,
                )
                t1 = self._clock()
                if self._reservations and used_settled + est <= self.hourly_budget:
                    self._reservation_event.clear()
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
                else:
                    await self._sleep(wait_s)
                    t2 = self._clock()
                    actual_elapsed = max(0.0, t2 - t1)
                    if not self._uses_custom_clock and actual_elapsed < wait_s * 0.5:
                        self._virtual_time_offset += wait_s - actual_elapsed
                    slept += wait_s
                    self.total_slept_seconds += wait_s

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


async def extract_segment_with_infinite_backoff(
    segment: list[dict[str, Any]],
    *,
    extractor_model: str,
    prompt_version: str,
    user_id: str = LOCAL_DEV_USER_ID,
    llm_service: Any = None,
    initial_backoff_s: float = 30.0,
    max_backoff_s: float = 900.0,
    limiter: HourlyTokenBudgetLimiter | None = None,
    estimated_tokens: int = 1000,
    reservation: BudgetReservation | int | None = None,
    dry_run: bool = False,
    extract_fn: Callable[..., Coroutine[Any, Any, SegmentExtractionOutcome]] = extract_segment_cards,
    sleep_fn: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
) -> tuple[SegmentExtractionOutcome, int]:
    """Extract cards from `segment`, backing off by time indefinitely on transient 429 / quota exhaustion.

    Re-checks the hourly token budget before every retry and stops immediately on permanent quota errors.
    Returns `(outcome, rate_limit_retries)`.
    """
    attempt = 0
    init_s = max(0.01, float(initial_backoff_s))
    cap_s = max(init_s, float(max_backoff_s))
    sig = inspect.signature(extract_fn)
    supports_persist_raw = "persist_raw" in sig.parameters
    supports_skip_journal = "skip_journal" in sig.parameters
    current_reservation: BudgetReservation | Any = (
        reservation
        if isinstance(reservation, BudgetReservation)
        else (BudgetReservation(reservation, estimated_tokens) if type(reservation) is int else None)
    )

    while True:
        if limiter is not None:
            if attempt > 0:
                new_res = await limiter.wait_for_budget(estimated_tokens)
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

        extra_kwargs: dict[str, Any] = {}
        if supports_persist_raw:
            # Strip card `quote` and raw event text before writing `dalton_llm_journal` (`replayable=False`)
            extra_kwargs["persist_raw"] = False
        if supports_skip_journal:
            extra_kwargs["skip_journal"] = dry_run

        try:
            outcome = await extract_fn(
                segment,
                extractor_model=extractor_model,
                prompt_version=prompt_version,
                user_id=user_id,
                llm_service=llm_service,
                resend_indeterminate=True,
                **extra_kwargs,
            )
        except Exception as exc:
            if isinstance(exc, PermanentQuotaError) or is_permanent_quota_error(exc):
                if limiter is not None and current_reservation is not None:
                    limiter.release_reservation(current_reservation)
                raise PermanentQuotaError(
                    type(exc).__name__,
                    status_code=getattr(exc, "status_code", None),
                ) from exc
            if not is_rate_limit_or_quota_error(exc):
                if limiter is not None and current_reservation is not None:
                    limiter.release_reservation(current_reservation)
                raise
            outcome = SegmentExtractionOutcome(
                model_used=extractor_model,
                ok=False,
                rate_limited=True,
                error=type(exc).__name__,
            )

        if outcome.permanent_quota_error:
            if limiter is not None and current_reservation is not None:
                limiter.release_reservation(current_reservation)
            raise PermanentQuotaError(outcome.error or "permanent_quota_error")

        if not outcome.rate_limited:
            return outcome, attempt

        if limiter is not None:
            rl_tokens = outcome.total_tokens or (outcome.input_tokens + outcome.output_tokens) or estimated_tokens
            limiter.record_tokens(rl_tokens, reservation=current_reservation)

        delay = min(cap_s, init_s * (2 ** min(attempt, 8)))
        attempt += 1
        pause_until: float | None = None
        if limiter is not None:
            pause_until = limiter.notify_rate_limit_backoff(delay)
        logger.warning(
            "Rate limit / quota exhaustion on segment (attempt=%d); backing off for %.1fs before retry",
            attempt,
            delay,
        )
        uses_custom_clock = getattr(limiter, "_uses_custom_clock", True) if limiter is not None else True
        t_sleep0 = limiter._clock() if (limiter is not None and not uses_custom_clock) else 0.0
        try:
            await sleep_fn(delay)
            if limiter is not None and not uses_custom_clock:
                elapsed_sleep = max(0.0, limiter._clock() - t_sleep0)
                if elapsed_sleep < delay * 0.5:
                    limiter._virtual_time_offset += delay - elapsed_sleep
        finally:
            if limiter is not None:
                limiter.clear_rate_limit_backoff(pause_until)


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


async def process_weekly_batch(
    batch_events: list[dict[str, Any]],
    *,
    mode: str,
    window_start: date,
    window_end: date,
    limiter: HourlyTokenBudgetLimiter,
    extractor_model: str,
    prompt_version: str,
    dry_run: bool = False,
    initial_backoff_s: float = 30.0,
    max_backoff_s: float = 900.0,
    extract_concurrency: int | None = None,
    concurrency: int | None = None,
    extract_fn: Callable[..., Coroutine[Any, Any, SegmentExtractionOutcome]] = extract_segment_cards,
    embed_fn: Callable[..., Coroutine[Any, Any, tuple[list[list[float]], str]]] = embed_texts_via_bridge,
    sleep_fn: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
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
    run_id = f"{mode}_{window_start.isoformat()}_{window_end.isoformat()}_{uuid.uuid4().hex[:8]}"
    embedding_model_used = cfg.embedding_model_preferred

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
    total_segments = len(segments)

    candidate_cards: list[dict[str, Any]] = []
    accepted_cards: list[dict[str, Any]] = []
    completed_processed_rows: list[dict[str, Any]] = []
    segments_flushed = 0
    quotes_rejected = 0
    segments_discarded = 0
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    rate_limit_retries = 0
    cards_inserted = 0
    deduped_count = 0
    superseded_count = 0

    completed_by_idx: dict[int, tuple[list[dict[str, Any]], SegmentExtractionOutcome, int]] = {}
    next_to_flush = 0
    flush_failed = False
    flush_lock = asyncio.Lock()
    sem = asyncio.Semaphore(eff_concurrency)
    abort_flag = False

    async def _flush_ready_prefix() -> None:
        nonlocal next_to_flush, flush_failed, abort_flag, embedding_model_used
        nonlocal segments_flushed, quotes_rejected, segments_discarded
        nonlocal input_tokens, output_tokens, total_tokens, rate_limit_retries
        nonlocal cards_inserted, deduped_count, superseded_count

        async with flush_lock:
            if flush_failed:
                return
            try:
                while next_to_flush in completed_by_idx:
                    ready_batch: list[tuple[int, list[dict[str, Any]], SegmentExtractionOutcome, int]] = []
                    peek_idx = next_to_flush
                    while peek_idx in completed_by_idx and len(ready_batch) < eff_concurrency:
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
                    batch_candidate_cards: list[dict[str, Any]] = []
                    per_seg_items: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
                    for _seg_idx, seg, outcome, rl_retries in ready_batch:
                        batch_segments += 1
                        batch_rl_retries += rl_retries
                        batch_in_tokens += outcome.input_tokens
                        batch_out_tokens += outcome.output_tokens
                        batch_tot_tokens += outcome.total_tokens
                        batch_quotes_rejected += outcome.quotes_rejected
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
                            existing_hashes=set(),
                            existing_active_cards=list(accepted_cards),
                        )
                        for seg_idx, _, _, _ in ready_batch:
                            completed_by_idx.pop(seg_idx, None)
                        next_to_flush = peek_idx
                        segments_flushed += batch_segments
                        rate_limit_retries += batch_rl_retries
                        input_tokens += batch_in_tokens
                        output_tokens += batch_out_tokens
                        total_tokens += batch_tot_tokens
                        quotes_rejected += batch_quotes_rejected
                        segments_discarded += batch_segments_discarded
                        candidate_cards.extend(batch_candidate_cards)
                        accepted_cards.extend(acc_cards)
                        deduped_count += dedup_cnt
                        superseded_count += sup_cnt
                        for _seg_cards, seg_rows in per_seg_items:
                            completed_processed_rows.extend(seg_rows)
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
                    batch_accepted_cards: list[dict[str, Any]] = []
                    batch_processed_rows: list[dict[str, Any]] = []

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
                                batch_accepted_cards.extend(ins_cards)
                            if seg_rows:
                                await db_conv.record_processed_event_chunks(
                                    seg_rows,
                                    conn=tx_conn,
                                )
                                batch_processed_rows.extend(seg_rows)

                        await db_conv.update_run_progress(
                            run_id,
                            embedding_model=batch_emb_model,
                            events_read=len(batch_events),
                            segments_processed=segments_flushed + batch_segments,
                            cards_extracted=len(candidate_cards) + len(batch_candidate_cards),
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
                            },
                            conn=tx_conn,
                        )

                    for seg_idx, _, _, _ in ready_batch:
                        completed_by_idx.pop(seg_idx, None)
                    next_to_flush = peek_idx
                    embedding_model_used = batch_emb_model
                    segments_flushed += batch_segments
                    rate_limit_retries += batch_rl_retries
                    input_tokens += batch_in_tokens
                    output_tokens += batch_out_tokens
                    total_tokens += batch_tot_tokens
                    quotes_rejected += batch_quotes_rejected
                    segments_discarded += batch_segments_discarded
                    candidate_cards.extend(batch_candidate_cards)
                    deduped_count += batch_dedup_cnt
                    cards_inserted += batch_cards_inserted
                    superseded_count += batch_superseded_count
                    accepted_cards.extend(batch_accepted_cards)
                    completed_processed_rows.extend(batch_processed_rows)
            except BaseException:
                flush_failed = True
                abort_flag = True
                raise

    async def _extract_one_segment(seg_idx: int, seg: list[dict[str, Any]]) -> None:
        nonlocal abort_flag

        async with sem:
            if abort_flag:
                return
            est_tokens = estimate_segment_tokens(seg, hourly_budget=limiter.hourly_budget)
            t0 = time.monotonic()
            reservation = await limiter.wait_for_budget(est_tokens)
            try:
                outcome, rl_retries = await extract_segment_with_infinite_backoff(
                    seg,
                    extractor_model=extractor_model,
                    prompt_version=prompt_version,
                    user_id=archive_billing_user_id(),
                    initial_backoff_s=initial_backoff_s,
                    max_backoff_s=max_backoff_s,
                    limiter=limiter,
                    estimated_tokens=est_tokens,
                    reservation=reservation,
                    dry_run=dry_run,
                    extract_fn=extract_fn,
                    sleep_fn=sleep_fn,
                )
                consumed = (
                    outcome.total_tokens
                    or (outcome.input_tokens + outcome.output_tokens)
                    or est_tokens
                )
                limiter.record_tokens(consumed, reservation=reservation)
            except BaseException:
                limiter.release_reservation(reservation)
                abort_flag = True
                raise

            if outcome.permanent_quota_error:
                abort_flag = True
                raise PermanentQuotaError(outcome.error or "permanent_quota_error")
            if not outcome.ok:
                abort_flag = True
                raise SegmentExtractionError(outcome.error or "segment_extraction_failed")

            elapsed_s = max(0.0, time.monotonic() - t0)
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
            await asyncio.shield(_flush_ready_prefix())

    tasks: list[asyncio.Task[None]] = []
    try:
        if segments:
            tasks = [
                asyncio.create_task(_extract_one_segment(idx, seg))
                for idx, seg in enumerate(segments)
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

        rendered_summary: dict[str, Any] = {"company_files": [], "topic_files": []}
        if not dry_run:
            unrendered_dates = await db_conv.list_unrendered_dates(conn=conn)
            touched_dates = sorted(
                set(unrendered_dates)
                | {str(c.get("date_et") or "")[:10] for c in accepted_cards if c.get("date_et")}
            )
            if touched_dates:
                date_cards = await db_conv.list_cards_for_dates(
                    touched_dates, active_only=False, conn=conn
                )
                rendered_summary = await render_and_write_conversation_digests(
                    date_cards,
                    target_dates=touched_dates,
                    user_id=archive_owner_user_id(),
                    backend_by_ticker=backend_by_ticker,
                    home_backend=home_backend,
                    conn=conn,
                )
                rendered_dates = rendered_summary.get("rendered_dates")
                if rendered_dates is None:
                    rendered_dates = list(touched_dates)
                if rendered_dates:
                    await db_conv.mark_dates_rendered(
                        rendered_dates,
                        card_locations=rendered_summary.get("card_locations"),
                        conn=conn,
                    )

        batch_summary = {
            "run_id": run_id,
            "mode": mode,
            "dry_run": dry_run,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "extractor_model": extractor_model,
            "embedding_model": embedding_model_used,
            "events_read": len(batch_events),
            "segments_processed": segments_flushed,
            "segments_discarded": segments_discarded,
            "cards_extracted": len(candidate_cards),
            "cards_accepted": len(accepted_cards),
            "cards_inserted": len(accepted_cards) if dry_run else cards_inserted,
            "cards_deduped": deduped_count,
            "cards_superseded": superseded_count,
            "quotes_rejected": quotes_rejected,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "rate_limit_retries": rate_limit_retries,
            "rendered": rendered_summary,
            "processed_chunk_keys": [
                (r["agent"], r["session_id"], r["event_id"], int(r["chunk_idx"]))
                for r in completed_processed_rows
            ],
        }

        if not dry_run:
            await db_conv.update_run_finish(
                run_id,
                status="completed",
                embedding_model=embedding_model_used,
                events_read=len(batch_events),
                segments_processed=segments_flushed,
                cards_extracted=len(candidate_cards),
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
                    "rendered": rendered_summary,
                },
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

        if not dry_run:
            is_interrupt = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
            run_status = "interrupted" if is_interrupt else "failed"
            try:
                await asyncio.shield(
                    db_conv.update_run_finish(
                        run_id,
                        status=run_status,
                        embedding_model=embedding_model_used,
                        events_read=len(batch_events),
                        segments_processed=segments_flushed,
                        cards_extracted=len(candidate_cards),
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
    hourly_token_budget: int | None = None,
    extract_concurrency: int | None = None,
    concurrency: int | None = None,
    skip_host_export: bool = False,
    extract_fn: Callable[..., Coroutine[Any, Any, SegmentExtractionOutcome]] = extract_segment_cards,
    embed_fn: Callable[..., Coroutine[Any, Any, tuple[list[list[float]], str]]] = embed_texts_via_bridge,
    sleep_fn: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
    backend_by_ticker: dict[str, Any] | None = None,
    home_backend: Any = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Execute the end-to-end conversation archive pipeline (`run` or `backfill`)."""
    cfg = get_dalton_settings().conversations
    today_et = datetime.now(ET_TZ).date()
    eff_since = since_date or (today_et - timedelta(days=int(cfg.default_backfill_days or 14)))
    eff_until = until_date or today_et
    eff_model = (extractor_model or cfg.extractor_model).strip()
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

        limiter = HourlyTokenBudgetLimiter(
            eff_budget,
            sleep_fn=sleep_fn,
        )

        batch_results: list[dict[str, Any]] = []
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

            if not dry_run and rule_discarded_rows:
                await db_conv.record_processed_event_chunks(rule_discarded_rows, conn=conn)
                for r in rule_discarded_rows:
                    processed_keys.add((r["agent"], r["session_id"], r["event_id"], int(r["chunk_idx"])))

            events_total += len(unprocessed_chunks)
            weekly_batches = group_events_by_weekly_window(
                unprocessed_chunks,
                since_date=eff_since,
                until_date=eff_until,
            )

            for w_start, w_end, b_events in weekly_batches:
                b_summary = await process_weekly_batch(
                    b_events,
                    mode=mode,
                    window_start=w_start,
                    window_end=w_end,
                    limiter=limiter,
                    extractor_model=eff_model,
                    prompt_version=cfg.prompt_version,
                    dry_run=dry_run,
                    initial_backoff_s=cfg.rate_limit_initial_backoff_seconds,
                    max_backoff_s=cfg.rate_limit_max_backoff_seconds,
                    extract_concurrency=eff_concurrency,
                    extract_fn=extract_fn,
                    embed_fn=embed_fn,
                    sleep_fn=sleep_fn,
                    backend_by_ticker=backend_by_ticker,
                    home_backend=home_backend,
                    conn=conn,
                )
                for k in b_summary.get("processed_chunk_keys") or []:
                    processed_keys.add(tuple(k))
                batch_results.append(b_summary)

            if not dry_run and set(all_file_chunk_keys).issubset(processed_keys):
                f_path.unlink(missing_ok=True)
                files_consumed.append(f_path.name)

        if not dry_run and not is_backfill and deferred_export_cursors is not None:
            save_cursors_file(deferred_export_cursors, eff_export_cursors)

        # Retry rendering for any previously stored cards whose digest files were not yet written
        rendered_retry_summary: dict[str, Any] | None = None
        if not dry_run:
            pending_dates = await db_conv.list_unrendered_dates(conn=conn)
            if pending_dates:
                date_cards = await db_conv.list_cards_for_dates(pending_dates, active_only=False, conn=conn)
                rendered_retry_summary = await render_and_write_conversation_digests(
                    date_cards,
                    target_dates=pending_dates,
                    user_id=archive_owner_user_id(),
                    backend_by_ticker=backend_by_ticker,
                    home_backend=home_backend,
                    conn=conn,
                )
                retry_rendered_dates = rendered_retry_summary.get("rendered_dates")
                if retry_rendered_dates is None:
                    retry_rendered_dates = list(pending_dates)
                if retry_rendered_dates:
                    await db_conv.mark_dates_rendered(
                        retry_rendered_dates,
                        card_locations=rendered_retry_summary.get("card_locations"),
                        conn=conn,
                    )

        return {
            "ok": True,
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
            "cards_inserted_total": sum(int(b.get("cards_inserted", 0)) for b in batch_results),
            "cards_deduped_total": sum(int(b.get("cards_deduped", 0)) for b in batch_results),
            "cards_superseded_total": sum(int(b.get("cards_superseded", 0)) for b in batch_results),
            "quotes_rejected_total": sum(int(b.get("quotes_rejected", 0)) for b in batch_results),
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
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="subcommand")
    subparsers.add_parser("run", parents=[common], help="Run incremental daily conversation extraction")
    subparsers.add_parser(
        "backfill",
        parents=[common],
        help="Backfill historical conversations in weekly batches with token budget pacing",
    )
    return parser


async def _async_main(args: argparse.Namespace) -> dict[str, Any]:
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
                await wm.shutdown()
        except Exception:
            pass
        if db_pool is not None:
            await db_pool.close()


def main(argv: list[str] | None = None) -> int:
    configure_archive_logging()
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        summary = asyncio.run(_async_main(args))
    except ArchiveAlreadyRunningError as exc:
        return int(exc.code if isinstance(exc.code, int) else 2)
    print(json.dumps(summary, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
