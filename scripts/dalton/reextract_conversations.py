#!/usr/bin/env python3
"""One-off re-extraction of every archived conversation card with a new model and prompt version.

The daily archive cannot do this: `dalton_conversation_processed` is keyed by event chunk, not
by prompt version, so `conversation_archive.py run|backfill` skips every event that was ever
extracted. This script re-exports the same OpenClaw history, extracts it again into a local
staging ledger, and only then swaps the cards in one guarded transaction.

Subcommands, in order (each later step reads the run directory the earlier one wrote):
- `plan`: read-only. Export + segment exactly like a backfill, print size and ETA, freeze the
  segments into `var/dalton/reextract/<run_id>/`.
- `extract`: LLM calls only; outcomes are appended to `staged.jsonl` (resumable). Never touches
  `dalton_conversation_cards` or `dalton_conversation_processed`.
- `apply`: the one writing step: retract the old cards whose events were re-extracted and insert
  the staged ones in a single transaction, with count assertions. Dry run by default (runs the
  transaction, then rolls back).
- `render`: embed the new cards and re-render every date whose files the run touched. Dry run
  by default.
- `rollback`: restore the pre-apply state from the saved snapshot. Dry run by default; `render`
  must run afterwards to put the old files back.

Exit codes: 0 ok, 1 fatal, 2 the daily archive is running (refused), 3 incomplete / refused.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import sys
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from psycopg.rows import dict_row  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

from scripts.dalton.conversation_archive import (  # noqa: E402
    EXIT_CODE_INCOMPLETE,
    EXIT_CODE_LOCK_HELD,
    EXIT_CODE_OK,
    HourlyTokenBudgetLimiter,
    PermanentQuotaError,
    archive_billing_user_id,
    configure_archive_logging,
    estimate_segment_tokens,
    extract_segment_with_infinite_backoff,
    group_events_by_weekly_window,
    read_inbox_jsonl,
)
from scripts.dalton.conversations.export_openclaw import (  # noqa: E402
    DEFAULT_AGENTS,
    DEFAULT_OCW_AGENTS_ROOT,
    is_operational_or_chitchat,
    run_export,
)
from src.dalton.conversations.embedder import embed_texts_via_bridge  # noqa: E402
from src.dalton.conversations.extractor import (  # noqa: E402
    SegmentExtractionOutcome,
    expand_event_chunks,
    extract_segment_cards,
    is_in_flight_error,
    is_transient_network_error,
    segment_events,
)
from src.dalton.conversations import render as render_mod  # noqa: E402
from src.dalton.conversations.render import (  # noqa: E402
    archive_owner_user_id,
    render_and_write_conversation_digests,
)
from src.dalton.database import conversations as db_conv  # noqa: E402
from src.dalton.settings import get_dalton_settings  # noqa: E402

logger = logging.getLogger("dalton.reextract_conversations")

RUNS_ROOT = REPO_ROOT / "var" / "dalton" / "reextract"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_FALLBACK_MODEL = "gemini-4-argon-medium-oauth"
DEFAULT_RETRACT_PROMPT_VERSION = "conv_card_v1"
DEFAULT_CONCURRENCY = 8
DEFAULT_P50_SECONDS = 116.0
DEFAULT_PROGRESS_EVERY = 50

PLAN_FILE = "plan.json"
SEGMENTS_FILE = "segments.jsonl"
STAGED_FILE = "staged.jsonl"
SNAPSHOT_FILE = "pre_apply_status.json"
REPLACED_ROWS_FILE = "pre_apply_replaced_rows.jsonl"
POST_APPLY_FILE = "post_apply_status.json"
APPLY_MANIFEST_FILE = "apply_manifest.json"
TOUCHED_LOCATIONS_FILE = "touched_locations.json"
RENDER_PROGRESS_FILE = "render_progress.json"
ROLLBACK_REPORT_FILE = "rollback_report.json"
GHOST_CARD_PREFIX = "__reextract_ghost__"

OUTCOME_FLAG_FIELDS = (
    "cards_raw_count",
    "quotes_rejected",
    "cards_flagged_unsupported_number",
    "cards_flagged_unsupported_names",
    "cards_flagged_unsupported_conclusion",
    "soft_unsupported_numbers",
    "figures_dropped",
    "periods_cleared",
    "currencies_stripped",
    "tickers_dropped",
    "tickers_backfilled",
    "types_corrected",
)

# Every column of dalton_conversation_cards with the cast a re-insert needs; rollback puts a
# replaced old row back byte-for-byte, embedding included, so nothing has to be recomputed.
FULL_ROW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("card_id", "text"),
    ("source_type", "text"),
    ("agent", "text"),
    ("session_id", "text"),
    ("session_key", "text"),
    ("generation", "text"),
    ("channel", "text"),
    ("chat_type", "text"),
    ("event_ids", "text[]"),
    ("seq_range", "jsonb"),
    ("ts", "timestamptz"),
    ("date_et", "date"),
    ("speaker", "jsonb"),
    ("speaker_role", "text"),
    ("tickers", "text[]"),
    ("entities", "text[]"),
    ("sectors", "text[]"),
    ("topic_label", "text"),
    ("type", "text"),
    ("claim", "text"),
    ("figures", "jsonb"),
    ("quote", "text"),
    ("quote_event_id", "text"),
    ("cited_sources", "jsonb"),
    ("confidence", "double precision"),
    ("status", "text"),
    ("supersedes", "text"),
    ("extractor_model", "text"),
    ("prompt_version", "text"),
    ("content_hash", "text"),
    ("search_text", "text"),
    ("embedding", "vector"),
    ("rendered_locations", "jsonb"),
    ("files_written_at", "timestamptz"),
    ("created_at", "timestamptz"),
)

PAUSE_HINT = """The daily conversation archive is running. Wait for it to finish, then pause its schedule
before retrying, so it cannot start in the middle of apply/render:
  - Dalton UI: System jobs -> Conversation archive -> off, or
  - POST /jobs/conversation-archive/disable on the researchdata bridge, or
  - write {"enabled": false} to ~/.config/dalton/conversation-archive-state.json
    (or $DALTON_ARCHIVE_STATE_FILE); launchd runs then exit with "已关闭".
Turn it back on after `render` has finished."""


class ArchiveBusyError(RuntimeError):
    """The daily archive holds its pid file or advisory lock, or our lock was lost."""


class RunStateError(RuntimeError):
    """The run directory is not in the state this subcommand needs."""


class RollbackConflictError(RunStateError):
    """Something changed the run's cards after apply; rolling back would undo it."""


class ApplyAssertionError(RuntimeError):
    """A count did not match; raising inside the transaction rolls everything back."""


class _DryRunRollback(Exception):
    """Raised at the end of a dry-run transaction so every statement runs but nothing commits."""


# --------------------------------------------------------------------------------------------
# Run directory I/O
# --------------------------------------------------------------------------------------------


def new_run_id() -> str:
    return f"rx_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"


def run_dir_for(run_id: str, root: Path | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id or ""):
        raise RunStateError(f"invalid run id {run_id!r}")
    return (root or RUNS_ROOT) / run_id


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_json_atomic(path: Path, payload: Any) -> None:
    """Snapshot and manifest files decide what rollback does, so a torn write must be impossible."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_plan(run_dir: Path) -> dict[str, Any]:
    path = run_dir / PLAN_FILE
    if not path.is_file():
        raise RunStateError(f"no plan in {run_dir}; run `plan` first")
    return read_json(path)


def load_segments(run_dir: Path) -> list[dict[str, Any]]:
    return read_inbox_jsonl(run_dir / SEGMENTS_FILE)


def _append_line_sync(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # After a crash mid-write the file may end without a newline; terminate the torn line first
    # or this record would be glued onto it and lost on the next load.
    torn = False
    if path.is_file() and path.stat().st_size > 0:
        with path.open("rb") as rfh:
            rfh.seek(-1, os.SEEK_END)
            torn = rfh.read(1) != b"\n"
    with path.open("a", encoding="utf-8") as fh:
        if torn:
            fh.write("\n")
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


class StagingLedger:
    """Append-only JSONL of per-segment outcomes; the latest line for a segment key wins.

    A JSONL file rather than a staging table keeps extract free of DDL and of any database
    write: it can run for hours against production with nothing to clean up, a crash loses at
    most the line being written (a torn last line is ignored), and apply reads it in one pass.
    Appends are serialized by one lock and the write+fsync runs off the event loop, so a high
    `--concurrency` neither interleaves lines nor stalls the in-flight calls.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _append_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    def load_latest(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        if not self.path.is_file():
            return latest
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if not s:
                    continue
                try:
                    rec = json.loads(s)
                except json.JSONDecodeError:
                    logger.warning("Ignoring a torn line in %s", self.path.name)
                    continue
                if isinstance(rec, dict) and rec.get("key"):
                    latest[str(rec["key"])] = rec
        return latest

    async def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        async with self._append_lock():
            await asyncio.to_thread(_append_line_sync, self.path, line)


def _loc_identity(loc: dict[str, Any]) -> str:
    return json.dumps(
        [loc.get("workspace_id"), loc.get("path"), loc.get("target"), loc.get("ticker")],
        ensure_ascii=False,
    )


class TouchedLocations:
    """Every digest file location the run's old or new cards ever claimed, by date.

    `render` revisits each of these dates even when no card is left on it, so a file that only
    a now-deleted or now-retracted card pointed at is still removed.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.by_date: dict[str, list[dict[str, Any]]] = {}
        if path.is_file():
            raw = read_json(path).get("by_date") or {}
            self.by_date = {str(d): list(v or []) for d, v in raw.items()}

    def add(self, date_et: Any, locations: Iterable[Any]) -> None:
        d = str(date_et or "")[:10]
        if not d:
            return
        slot = self.by_date.setdefault(d, [])
        seen = {_loc_identity(x) for x in slot}
        for loc in locations or []:
            if isinstance(loc, dict) and _loc_identity(loc) not in seen:
                seen.add(_loc_identity(loc))
                slot.append(dict(loc))

    def save(self) -> None:
        write_json_atomic(self.path, {"by_date": dict(sorted(self.by_date.items()))})


# --------------------------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------------------------


def segment_key(segment: list[dict[str, Any]]) -> str:
    """Identity of a segment by its chunk keys, so resume survives reordering and re-planning."""
    keys = [
        [
            str(ev.get("agent") or "main"),
            str(ev.get("session_id") or ""),
            str(ev.get("event_id") or f"seq:{int(ev.get('seq', 0))}"),
            int(ev.get("chunk_idx", 0)),
        ]
        for ev in segment
    ]
    return hashlib.sha256(json.dumps(keys, separators=(",", ":")).encode("utf-8")).hexdigest()[:32]


def segment_chars(segment: list[dict[str, Any]]) -> int:
    return sum(len(str(ev.get("text") or "")) for ev in segment)


def build_planned_segments(
    raw_events: list[dict[str, Any]],
    *,
    since_date: date,
    until_date: date,
    max_gap_minutes: int,
    max_segment_chars: int,
    max_segment_events: int,
) -> list[dict[str, Any]]:
    """Segment the export the way a backfill does: chunk, drop ops/chitchat, weekly windows, segment.

    The processed-chunk skip is deliberately absent: every chunk is re-extracted.
    """
    chunks = expand_event_chunks(raw_events, max_segment_chars=max_segment_chars)
    kept = [ch for ch in chunks if not is_operational_or_chitchat(str(ch.get("text") or ""))[0]]
    planned: list[dict[str, Any]] = []
    for w_start, w_end, w_events in group_events_by_weekly_window(
        kept, since_date=since_date, until_date=until_date
    ):
        for seg in segment_events(
            w_events,
            max_gap_minutes=max_gap_minutes,
            max_segment_chars=max_segment_chars,
            max_segment_events=max_segment_events,
        ):
            planned.append(
                {
                    "idx": len(planned),
                    "key": segment_key(seg),
                    "window": [w_start.isoformat(), w_end.isoformat()],
                    "chars": segment_chars(seg),
                    "events": seg,
                }
            )
    return planned


def estimate_eta_seconds(n_segments: int, concurrency: int, p50_seconds: float) -> float:
    """Waves of `concurrency` parallel calls, each wave lasting about the median call."""
    if n_segments <= 0:
        return 0.0
    return math.ceil(n_segments / max(1, int(concurrency))) * max(0.0, float(p50_seconds))


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def summarize_plan(
    planned: list[dict[str, Any]],
    *,
    concurrency: int,
    p50_seconds: float,
) -> dict[str, Any]:
    eta = estimate_eta_seconds(len(planned), concurrency, p50_seconds)
    return {
        "segments": len(planned),
        "total_chars": sum(int(s["chars"]) for s in planned),
        "events": sum(len(s["events"]) for s in planned),
        "concurrency": int(concurrency),
        "p50_seconds": float(p50_seconds),
        "eta_seconds": eta,
        "eta": format_duration(eta),
    }


def agent_sqlite_status(agents: list[str], ocw_agents_root: Path) -> tuple[list[str], list[str]]:
    """`run_export` silently skips an agent whose SQLite is missing; record it so apply can refuse."""
    present, missing = [], []
    for agent in agents:
        path = ocw_agents_root / agent / "agent" / "openclaw-agent.sqlite"
        (present if path.is_file() else missing).append(agent)
    return present, missing


def export_incomplete_reasons(plan: dict[str, Any]) -> list[str]:
    exp = plan.get("export") or {}
    reasons = []
    if exp.get("missing_agents"):
        reasons.append(f"agents without SQLite: {', '.join(exp['missing_agents'])}")
    if int(exp.get("events_parse_failed") or 0) > 0:
        reasons.append(f"{exp['events_parse_failed']} event(s) failed to parse")
    if "missing_agents" not in exp:
        reasons.append("plan predates export completeness tracking")
    return reasons


# --------------------------------------------------------------------------------------------
# Database access
# --------------------------------------------------------------------------------------------


def _state_hash(rows: Iterable[dict[str, Any]]) -> str:
    canon = sorted(
        [str(r["card_id"]), str(r.get("status")), r.get("supersedes")] for r in rows
    )
    return hashlib.sha256(json.dumps(canon, ensure_ascii=False).encode("utf-8")).hexdigest()


class CardStore:
    """All SQL this script runs, on the connection that holds the archive advisory lock."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["CardStore"]:
        async with self.conn.transaction():
            yield self

    async def _fetchall(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        async with self.conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return [dict(r) for r in (await cur.fetchall() or [])]

    async def _execute(self, sql: str, params: Any = None) -> int:
        async with self.conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return int(cur.rowcount or 0)

    async def set_read_only(self) -> None:
        await self._execute("SET TRANSACTION READ ONLY")

    async def lock_held(self, lock_key: str = db_conv.ARCHIVE_ADVISORY_LOCK_KEY) -> bool:
        # pg_advisory_lock(bigint) splits the key into classid (high 32 bits) and objid (low).
        rows = await self._fetchall(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_locks
                WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted
                  AND objsubid = 1
                  AND ((classid::bigint << 32) | objid::bigint) = hashtext(%s)::bigint
            ) AS held
            """,
            (str(lock_key),),
        )
        return bool(rows and rows[0].get("held"))

    async def db_clock(self) -> str:
        rows = await self._fetchall("SELECT clock_timestamp()::text AS now")
        return str(rows[0]["now"])

    async def lock_cards_table(self) -> None:
        # Readers (search) keep working; any other writer waits until we commit or roll back.
        await self._execute("LOCK TABLE dalton_conversation_cards IN SHARE ROW EXCLUSIVE MODE")

    async def date_range(self, prompt_version: str) -> tuple[date | None, date | None]:
        rows = await self._fetchall(
            "SELECT MIN(date_et) AS lo, MAX(date_et) AS hi FROM dalton_conversation_cards"
            " WHERE prompt_version = %s",
            (prompt_version,),
        )
        row = rows[0] if rows else {}
        return row.get("lo"), row.get("hi")

    async def counts_by_date(self) -> list[dict[str, Any]]:
        return await self._fetchall(
            """
            SELECT date_et::text AS date_et, prompt_version, status, COUNT(*)::int AS n
            FROM dalton_conversation_cards
            GROUP BY 1, 2, 3
            ORDER BY 1, 2, 3
            """
        )

    async def snapshot_statuses(self) -> list[dict[str, Any]]:
        return await self._fetchall(
            """
            SELECT card_id, status, supersedes, prompt_version, content_hash,
                   agent, session_id, event_ids, quote_event_id,
                   date_et::text AS date_et, rendered_locations,
                   files_written_at::text AS files_written_at
            FROM dalton_conversation_cards
            ORDER BY card_id
            """
        )

    async def count_live(self, prompt_version: str) -> int:
        rows = await self._fetchall(
            "SELECT COUNT(*)::int AS n FROM dalton_conversation_cards"
            " WHERE prompt_version = %s AND status <> 'retracted'",
            (prompt_version,),
        )
        return int(rows[0]["n"]) if rows else 0

    async def retract_ids(self, card_ids: list[str]) -> int:
        # rendered_locations stay: the renderer diffs them to delete digest files no card claims.
        if not card_ids:
            return 0
        return await self._execute(
            """
            UPDATE dalton_conversation_cards
            SET status = 'retracted', files_written_at = NULL
            WHERE card_id = ANY(%s::text[]) AND status <> 'retracted'
            """,
            (list(card_ids),),
        )

    async def fetch_full_rows(
        self, *, content_hashes: list[str], card_ids: list[str]
    ) -> list[dict[str, Any]]:
        if not content_hashes and not card_ids:
            return []
        select_cols = ", ".join(
            f"{name}::text AS {name}" if cast in ("vector", "timestamptz", "date") else name
            for name, cast in FULL_ROW_COLUMNS
        )
        return await self._fetchall(
            f"""
            SELECT {select_cols}
            FROM dalton_conversation_cards
            WHERE content_hash = ANY(%s::text[]) OR card_id = ANY(%s::text[])
            ORDER BY card_id
            """,
            (list(content_hashes), list(card_ids)),
        )

    async def delete_cards(self, card_ids: list[str]) -> int:
        if not card_ids:
            return 0
        return await self._execute(
            "DELETE FROM dalton_conversation_cards WHERE card_id = ANY(%s::text[])",
            (list(card_ids),),
        )

    async def insert_segment_cards(
        self, cards: list[dict[str, Any]]
    ) -> tuple[int, int, list[dict[str, Any]]]:
        # The archive's own insert path, so supersedes is computed exactly as a daily run would;
        # it inserts with supersedes NULL and links afterwards, so in-batch pointers are safe.
        return await db_conv.insert_cards_batch(cards, embeddings=None, conn=self.conn)

    async def set_rendered_locations(self, locations: dict[str, list[dict[str, Any]]]) -> int:
        total = 0
        for card_id, locs in locations.items():
            total += await self._execute(
                "UPDATE dalton_conversation_cards SET rendered_locations = %s WHERE card_id = %s",
                (Jsonb(list(locs or [])), str(card_id)),
            )
        return total

    async def count_present(self, card_ids: list[str]) -> int:
        if not card_ids:
            return 0
        rows = await self._fetchall(
            "SELECT COUNT(*)::int AS n FROM dalton_conversation_cards WHERE card_id = ANY(%s::text[])",
            (list(card_ids),),
        )
        return int(rows[0]["n"]) if rows else 0

    async def state_of(self, card_ids: list[str]) -> list[dict[str, Any]]:
        if not card_ids:
            return []
        return await self._fetchall(
            """
            SELECT card_id, status, supersedes
            FROM dalton_conversation_cards
            WHERE card_id = ANY(%s::text[])
            ORDER BY card_id
            """,
            (list(card_ids),),
        )

    async def later_references(
        self, card_ids: list[str], after: str | None = None
    ) -> list[dict[str, Any]]:
        """Cards outside `card_ids` (written after `after` when given) that point at `card_ids` via supersedes."""
        if not card_ids:
            return []
        return await self._fetchall(
            """
            SELECT card_id, supersedes, date_et::text AS date_et, created_at::text AS created_at
            FROM dalton_conversation_cards
            WHERE (%s::timestamptz IS NULL OR created_at > %s::timestamptz)
              AND supersedes = ANY(%s::text[])
              AND NOT (card_id = ANY(%s::text[]))
            ORDER BY card_id
            """,
            (after, after, list(card_ids), list(card_ids)),
        )

    async def count_created_after(self, after: str, exclude_ids: list[str]) -> int:
        rows = await self._fetchall(
            "SELECT COUNT(*)::int AS n FROM dalton_conversation_cards"
            " WHERE created_at > %s::timestamptz AND NOT (card_id = ANY(%s::text[]))",
            (after, list(exclude_ids)),
        )
        return int(rows[0]["n"]) if rows else 0

    async def fetch_locations(self, card_ids: list[str]) -> list[dict[str, Any]]:
        if not card_ids:
            return []
        return await self._fetchall(
            """
            SELECT card_id, date_et::text AS date_et, rendered_locations
            FROM dalton_conversation_cards
            WHERE card_id = ANY(%s::text[])
            ORDER BY card_id
            """,
            (list(card_ids),),
        )

    async def insert_full_rows(self, rows: list[dict[str, Any]]) -> int:
        """Two passes: rows go in with supersedes NULL, then pointers are restored, so a row may
        point at another row of the same batch regardless of insertion order."""
        if not rows:
            return 0
        cols = ", ".join(name for name, _ in FULL_ROW_COLUMNS)
        placeholders = ", ".join(f"%s::{cast}" for _, cast in FULL_ROW_COLUMNS)
        sql = f"INSERT INTO dalton_conversation_cards ({cols}) VALUES ({placeholders})"
        total = 0
        for row in rows:
            params = []
            for name, cast in FULL_ROW_COLUMNS:
                val = None if name == "supersedes" else row.get(name)
                if cast == "jsonb":
                    val = Jsonb(val if val is not None else [])
                params.append(val)
            total += await self._execute(sql, params)
        linked = 0
        expected = 0
        for row in rows:
            if row.get("supersedes"):
                expected += 1
                linked += await self._execute(
                    "UPDATE dalton_conversation_cards SET supersedes = %s WHERE card_id = %s",
                    (str(row["supersedes"]), str(row["card_id"])),
                )
        if linked != expected:
            raise ApplyAssertionError(f"re-linked supersedes on {linked} of {expected} rows")
        return total

    async def restore_statuses(self, rows: list[dict[str, Any]], *, clear_dates: Iterable[str]) -> int:
        """Put back status, supersedes, locations and (outside `clear_dates`) files_written_at.

        The cleared stamp is what makes render redo a date, and the archive's own pass renders
        every date with a NULL stamp, so clearing it on dates the run never touched would make
        the next archive run rewrite the whole table; writing back the snapshot stamp restores
        untouched active rows whose stamp `insert_cards_batch` cleared because they already had
        `supersedes`. Post-apply rows on `clear_dates` (not in `rows`) get their stamp cleared too.
        """
        dates = sorted({str(d)[:10] for d in clear_dates if d})
        restored = 0
        if rows:
            payload = [
                {
                    "card_id": r["card_id"],
                    "status": r["status"],
                    "supersedes": r.get("supersedes"),
                    "rendered_locations": list(r.get("rendered_locations") or []),
                    "files_written_at": r.get("files_written_at"),
                }
                for r in rows
            ]
            # One statement: the FK on supersedes is checked at its end, by which point every
            # target (snapshot rows, replaced rows already re-inserted) exists.
            restored = await self._execute(
                """
                UPDATE dalton_conversation_cards AS c
                SET status = r.status,
                    supersedes = r.supersedes,
                    rendered_locations = COALESCE(r.rendered_locations, '[]'::jsonb),
                    files_written_at = CASE WHEN c.date_et = ANY(%s::date[]) THEN NULL
                                            ELSE r.files_written_at END
                FROM jsonb_to_recordset(%s::jsonb)
                     AS r(
                         card_id text,
                         status text,
                         supersedes text,
                         rendered_locations jsonb,
                         files_written_at timestamptz
                     )
                WHERE c.card_id = r.card_id
                """,
                (dates, Jsonb(payload)),
            )
        if dates:
            await self._execute(
                """
                UPDATE dalton_conversation_cards
                SET files_written_at = NULL
                WHERE date_et = ANY(%s::date[]) AND files_written_at IS NOT NULL
                """,
                (dates,),
            )
        return restored

    async def cards_missing_embedding(self, card_ids: list[str], limit: int) -> list[dict[str, Any]]:
        if not card_ids:
            return []
        return await self._fetchall(
            """
            SELECT card_id, search_text
            FROM dalton_conversation_cards
            WHERE card_id = ANY(%s::text[]) AND embedding IS NULL
            ORDER BY card_id
            LIMIT %s
            """,
            (list(card_ids), int(limit)),
        )

    async def set_embedding(self, card_id: str, vector: list[float]) -> int:
        return await self._execute(
            "UPDATE dalton_conversation_cards SET embedding = %s::vector"
            " WHERE card_id = %s AND embedding IS NULL",
            (db_conv._format_vector_literal(vector), str(card_id)),
        )


DSN_ENV = "DALTON_REEXTRACT_DSN"


async def _open_autocommit_conn(dsn: str | None = None) -> Any:
    """A dedicated connection in autocommit, so `conn.transaction()` is a real BEGIN/COMMIT.

    `dsn` points the script at a scratch database (see `reextract_scratch_check.sh`); the
    connection is configured exactly like the app pool's.
    """
    from src.server.database import pool as pool_mod

    if dsn:
        from psycopg import AsyncConnection

        conn = await AsyncConnection.connect(dsn, **pool_mod._CONNECTION_KWARGS)
        await pool_mod._configure_postgres_connection(conn)
    else:
        conn = await pool_mod.open_session_connection()
    await conn.set_autocommit(True)
    return conn


def _dsn_dbname(dsn: str) -> str:
    from psycopg.conninfo import conninfo_to_dict

    return str(conninfo_to_dict(dsn).get("dbname") or "")


def resolve_dsn(args: argparse.Namespace) -> str | None:
    dsn = getattr(args, "dsn", None) or os.environ.get(DSN_ENV) or None
    if dsn:
        # Only the database name: the DSN may carry a password.
        logger.warning("Targeting database %r from --dsn/%s, not the app pool", _dsn_dbname(dsn), DSN_ENV)
    return dsn


async def ensure_lock_held(store: CardStore) -> None:
    if not await store.lock_held():
        raise ArchiveBusyError("lost the archive advisory lock; stopping before the next batch")


def check_run_consistency(
    plan: dict[str, Any],
    manifest: dict[str, Any] | None = None,
    *,
    retract_prompt_version: str | None = None,
) -> str:
    """Every step must act on the versions the plan was made for; returns the retract version."""
    planned = str(plan.get("retract_prompt_version") or "")
    if not planned:
        raise RunStateError("plan.json has no retract_prompt_version")
    if retract_prompt_version is not None and retract_prompt_version != planned:
        raise RunStateError(
            f"--retract-prompt-version {retract_prompt_version!r} does not match the plan's {planned!r}"
        )
    if manifest is not None:
        if manifest.get("run_id") not in (None, plan.get("run_id")):
            raise RunStateError("apply manifest belongs to another run")
        for key in ("retract_prompt_version", "prompt_version"):
            if manifest.get(key) is not None and manifest.get(key) != plan.get(key):
                raise RunStateError(f"apply manifest {key}={manifest.get(key)!r} does not match the plan")
    return planned


async def apply_commit_state(store: CardStore, manifest: dict[str, Any]) -> str:
    """For a `pending` manifest, tell from the table whether the apply transaction committed.

    Fresh (non-colliding) new cards exist only if it committed, and its retract targets are
    retracted only if it committed. Returns `committed`, `not_committed` or `unknown`.
    """
    replaced = set(manifest.get("replaced_card_ids") or [])
    fresh = [str(c) for c in manifest.get("new_card_ids") or [] if c not in replaced]
    retract = [str(c) for c in manifest.get("retracted_card_ids") or []]
    if not fresh and not retract:
        return "unknown"
    states = {str(r["card_id"]): r for r in await store.state_of(sorted(set(fresh) | set(retract)))}
    fresh_present = sum(1 for c in fresh if c in states)
    retracted_now = sum(1 for c in retract if (states.get(c) or {}).get("status") == "retracted")
    if fresh_present == 0 and retracted_now == 0:
        return "not_committed"
    if fresh_present == len(fresh) and retracted_now == len(retract):
        return "committed"
    return "unknown"


# --------------------------------------------------------------------------------------------
# Archive guard
# --------------------------------------------------------------------------------------------


def archive_pid_files() -> list[Path]:
    """The same pid files the bridge and `archive_daily.sh` claim before running the archive."""
    explicit = os.environ.get("DALTON_ARCHIVE_PID_FILE", "").strip()
    if explicit:
        return [Path(explicit).expanduser()]
    status = Path(
        os.environ.get(
            "DALTON_ARCHIVE_STATUS_FILE", "~/.cache/dalton/conversation-archive-status.json"
        )
    ).expanduser()
    state = Path(
        os.environ.get(
            "DALTON_ARCHIVE_STATE_FILE", "~/.config/dalton/conversation-archive-state.json"
        )
    ).expanduser()
    paths: list[Path] = []
    for parent in (status.parent, state.parent):
        cand = parent / "conversation-archive.pid"
        if cand not in paths:
            paths.append(cand)
    return paths


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def find_running_archive_pid(paths: list[Path] | None = None) -> int | None:
    for p in paths if paths is not None else archive_pid_files():
        try:
            pid = int(p.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if pid != os.getpid() and _pid_alive(pid):
            return pid
    return None


@asynccontextmanager
async def archive_guard(
    *,
    open_conn: Callable[[], Awaitable[Any]] | None = None,
    pid_files: list[Path] | None = None,
) -> AsyncIterator[Any]:
    """Refuse while the archive runs, then hold its advisory lock and yield that same connection.

    All of the subcommand's SQL runs on the yielded connection, so the lock cannot be lost
    without the work's own connection failing too. A scheduled archive run that fires
    meanwhile exits 2 ("已在运行，跳过").
    """
    pid = find_running_archive_pid(pid_files)
    if pid is not None:
        raise ArchiveBusyError(f"conversation archive pid {pid} is alive")
    lock_conn = await (open_conn or _open_autocommit_conn)()
    acquired = False
    try:
        acquired = await db_conv.try_acquire_archive_run_lock(conn=lock_conn)
        if not acquired:
            raise ArchiveBusyError("conversation archive advisory lock is held")
        yield lock_conn
    finally:
        if acquired:
            try:
                await db_conv.release_archive_run_lock(conn=lock_conn)
            except Exception:
                logger.warning("Releasing the archive advisory lock failed; closing the session frees it")
        await lock_conn.close()


def _archive_schedule_enabled() -> bool | None:
    path = Path(
        os.environ.get(
            "DALTON_ARCHIVE_STATE_FILE", "~/.config/dalton/conversation-archive-state.json"
        )
    ).expanduser()
    if not path.is_file():
        return True
    try:
        raw = path.read_text(encoding="utf-8").strip().lower()
        if raw in ("false", "0", "off", "disabled"):
            return False
        if raw in ("true", "1", "on", "enabled"):
            return True
        data = json.loads(raw)
        return bool(data.get("enabled", True)) if isinstance(data, dict) else None
    except Exception:
        return None


# --------------------------------------------------------------------------------------------
# extract
# --------------------------------------------------------------------------------------------


def _safe_error_kind(outcome: SegmentExtractionOutcome) -> str:
    """Only the error class / kind reaches the ledger: upstream messages can echo request data."""
    if outcome.error_kind:
        return str(outcome.error_kind)[:80]
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)", str(outcome.error or ""))
    return m.group(1) if m else "error"


def is_fallback_eligible(outcome: SegmentExtractionOutcome) -> bool:
    """Timeouts and transient upstream failures get one more try on the fallback model."""
    if is_in_flight_error(outcome) or is_transient_network_error(outcome) or outcome.transient_error:
        return True
    kind = f"{outcome.error_kind or ''} {outcome.error or ''}".lower()
    return "timeout" in kind or "timed out" in kind


def _outcome_record(
    seg: dict[str, Any],
    outcome: SegmentExtractionOutcome,
    *,
    attempt: str,
    model_requested: str,
    prompt_version: str,
    elapsed_s: float,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "key": seg["key"],
        "idx": int(seg["idx"]),
        "ok": bool(outcome.ok),
        "attempt": attempt,
        "model_requested": model_requested,
        "model_used": outcome.model_used or model_requested,
        "prompt_version": prompt_version,
        "elapsed_s": round(float(elapsed_s), 2),
        "at": datetime.now(timezone.utc).isoformat(),
        "tokens": {
            "in": int(outcome.input_tokens or 0),
            "out": int(outcome.output_tokens or 0),
            "total": int(outcome.total_tokens or 0),
        },
        "flags": {k: int(getattr(outcome, k, 0) or 0) for k in OUTCOME_FLAG_FIELDS},
    }
    if outcome.ok:
        rec["discarded"] = bool(outcome.discarded_segment)
        rec["category"] = outcome.category
        rec["cards"] = list(outcome.cards)
    else:
        rec["error_kind"] = _safe_error_kind(outcome)
        rec["fallback_eligible"] = is_fallback_eligible(outcome)
        rec["cards"] = []
    return rec


async def _build_shared_llm_service() -> Any:
    """Same shared service the archive builds: one client, provider fallback chain disabled."""
    try:
        from ptc_agent.config import load_from_files
        from src.server.services.llm.service import LLMService

        agent_cfg = await load_from_files()
        if getattr(agent_cfg, "llm", None) is not None and hasattr(agent_cfg.llm, "fallback"):
            agent_cfg.llm.fallback = []
        return LLMService(agent_config=agent_cfg, logger=logger)
    except Exception as exc:
        logger.debug("Shared LLMService init skipped: %s", type(exc).__name__)
        return None


@dataclass
class ExtractConfig:
    model: str
    fallback_model: str | None
    prompt_version: str
    concurrency: int = DEFAULT_CONCURRENCY
    limit: int | None = None
    respect_budget: bool = False
    hourly_budget: int = 120_000
    initial_backoff_s: float = 30.0
    max_backoff_s: float = 900.0
    progress_every: int = DEFAULT_PROGRESS_EVERY


@dataclass
class ExtractSummary:
    planned: int = 0
    already_staged: int = 0
    attempted: int = 0
    ok: int = 0
    failed: int = 0
    fallback_attempted: int = 0
    fallback_ok: int = 0
    cards: int = 0
    total_tokens: int = 0
    models_used: Counter = field(default_factory=Counter)
    failures: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "planned": self.planned,
            "already_staged": self.already_staged,
            "attempted": self.attempted,
            "ok": self.ok,
            "failed": self.failed,
            "fallback_attempted": self.fallback_attempted,
            "fallback_ok": self.fallback_ok,
            "cards": self.cards,
            "total_tokens": self.total_tokens,
            "models_used": dict(self.models_used),
            "failures": self.failures,
        }


class _Progress:
    def __init__(self, label: str, total: int, every: int, clock_fn: Callable[[], float]) -> None:
        self.label, self.total, self.every, self.clock = label, total, max(1, int(every)), clock_fn
        self.t0 = clock_fn()
        self.done = self.ok = self.failed = 0

    def tick(self, ok: bool) -> None:
        self.done += 1
        if ok:
            self.ok += 1
        else:
            self.failed += 1
        if self.done % self.every == 0 or self.done == self.total:
            elapsed = self.clock() - self.t0
            eta = elapsed / self.done * (self.total - self.done) if self.done else 0.0
            logger.info(
                "[%s] %d/%d done ok=%d failed=%d elapsed=%s eta=%s",
                self.label,
                self.done,
                self.total,
                self.ok,
                self.failed,
                format_duration(elapsed),
                format_duration(eta),
            )


async def run_extraction(
    segments: list[dict[str, Any]],
    ledger: StagingLedger,
    cfg: ExtractConfig,
    *,
    user_id: str,
    llm_service: Any = None,
    extract_fn: Callable[..., Awaitable[SegmentExtractionOutcome]] = extract_segment_cards,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
) -> ExtractSummary:
    """Extract every segment without an `ok` outcome; failures stay in the ledger for the next run.

    The primary model goes through the archive's backoff wrapper (429 waits, transient retries);
    the fallback model gets exactly one direct call, as decided for this run.

    Journal: `dry_run=True` on `extract_segment_with_infinite_backoff` means only
    `skip_journal=True` here (it writes nothing else). This run's journal is `staged.jsonl`;
    `dalton_llm_journal` rows are keyed by segment scope without the model, so a journalled
    primary attempt would collide with the fallback attempt for the same segment. The hourly
    budget gate is off unless `respect_budget` is set: the operator approved this spend.
    """
    summary = ExtractSummary(planned=len(segments))
    latest = ledger.load_latest()
    pending = [s for s in segments if not (latest.get(s["key"]) or {}).get("ok")]
    summary.already_staged = len(segments) - len(pending)
    if cfg.limit is not None:
        pending = pending[: max(0, int(cfg.limit))]

    limiter = (
        HourlyTokenBudgetLimiter(cfg.hourly_budget, sleep_fn=sleep_fn) if cfg.respect_budget else None
    )
    sem = asyncio.Semaphore(max(1, int(cfg.concurrency)))

    async def _primary_call(events: list[dict[str, Any]], est: int, reservation: Any) -> SegmentExtractionOutcome:
        outcome, _retries = await extract_segment_with_infinite_backoff(
            events,
            extractor_model=cfg.model,
            prompt_version=cfg.prompt_version,
            user_id=user_id,
            llm_service=llm_service,
            initial_backoff_s=cfg.initial_backoff_s,
            max_backoff_s=cfg.max_backoff_s,
            limiter=limiter,
            estimated_tokens=est,
            reservation=reservation,
            dry_run=True,  # = skip_journal=True only; see the docstring
            extract_fn=extract_fn,
            sleep_fn=sleep_fn,
        )
        return outcome

    async def _fallback_call(events: list[dict[str, Any]], est: int, reservation: Any) -> SegmentExtractionOutcome:
        outcome = await extract_fn(
            events,
            extractor_model=str(cfg.fallback_model),
            prompt_version=cfg.prompt_version,
            user_id=user_id,
            llm_service=llm_service,
            resend_indeterminate=False,
            persist_raw=False,
            skip_journal=True,
        )
        if limiter is not None and reservation is not None:
            limiter.record_tokens(int(outcome.total_tokens or 0) or est, reservation=reservation)
        return outcome

    async def _attempt(
        seg: dict[str, Any],
        model: str,
        attempt: str,
        call: Callable[[list[dict[str, Any]], int, Any], Awaitable[SegmentExtractionOutcome]],
        progress: _Progress,
    ) -> dict[str, Any]:
        events = seg["events"]
        async with sem:
            t0 = clock_fn()
            est = estimate_segment_tokens(
                events, hourly_budget=limiter.hourly_budget if limiter else None
            )
            reservation = await limiter.wait_for_budget(est) if limiter is not None else None
            try:
                outcome = await call(events, est, reservation)
            except PermanentQuotaError:
                if limiter is not None and reservation is not None:
                    limiter.release_reservation(reservation)
                raise
            except Exception as exc:
                if limiter is not None and reservation is not None:
                    limiter.release_reservation(reservation)
                outcome = SegmentExtractionOutcome(
                    ok=False,
                    model_used=model,
                    status="error",
                    error_kind=type(exc).__name__,
                    transient_error=is_transient_network_error(exc),
                )
            rec = _outcome_record(
                seg,
                outcome,
                attempt=attempt,
                model_requested=model,
                prompt_version=cfg.prompt_version,
                elapsed_s=clock_fn() - t0,
            )
        await ledger.append(rec)
        progress.tick(rec["ok"])
        return rec

    def _tally(rec: dict[str, Any]) -> None:
        summary.total_tokens += int(rec["tokens"]["total"])
        if rec["ok"]:
            summary.cards += len(rec["cards"])
            summary.models_used[rec["model_used"]] += 1

    summary.attempted = len(pending)
    p1 = _Progress(cfg.model, len(pending), cfg.progress_every, clock_fn)
    first = await asyncio.gather(*(_attempt(s, cfg.model, "primary", _primary_call, p1) for s in pending))
    retry: list[dict[str, Any]] = []
    by_key = {s["key"]: s for s in pending}
    final: dict[str, dict[str, Any]] = {}
    for rec in first:
        _tally(rec)
        final[rec["key"]] = rec
        if not rec["ok"] and rec.get("fallback_eligible") and cfg.fallback_model:
            retry.append(by_key[rec["key"]])

    if retry:
        logger.info("Retrying %d timed-out/transient segment(s) once on %s", len(retry), cfg.fallback_model)
        summary.fallback_attempted = len(retry)
        p2 = _Progress(str(cfg.fallback_model), len(retry), cfg.progress_every, clock_fn)
        second = await asyncio.gather(
            *(_attempt(s, str(cfg.fallback_model), "fallback", _fallback_call, p2) for s in retry)
        )
        for rec in second:
            _tally(rec)
            final[rec["key"]] = rec
            if rec["ok"]:
                summary.fallback_ok += 1

    for rec in final.values():
        if rec["ok"]:
            summary.ok += 1
        else:
            summary.failed += 1
            summary.failures.append(
                {"idx": rec["idx"], "key": rec["key"], "attempt": rec["attempt"], "error_kind": rec["error_kind"]}
            )
    summary.failures.sort(key=lambda f: f["idx"])
    return summary


# --------------------------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------------------------


EventKey = tuple[str, str, str]


@dataclass
class StagedCards:
    """Staged cards in segment order with in-staging duplicates removed, as the archive's seen-hash pass does."""

    by_segment: list[tuple[str, list[dict[str, Any]]]]
    total: int
    unique: int
    duplicates_in_staging: int
    missing_keys: list[str]
    failed: list[dict[str, Any]]
    wrong_prompt_version: int
    ok_keys: set[str]

    @property
    def cards(self) -> list[dict[str, Any]]:
        return [c for _, cs in self.by_segment for c in cs]


def collect_staged_cards(
    segments_meta: list[dict[str, Any]],
    latest: dict[str, dict[str, Any]],
    *,
    prompt_version: str,
) -> StagedCards:
    by_segment: list[tuple[str, list[dict[str, Any]]]] = []
    seen: set[str] = set()
    total = dupes = wrong_pv = 0
    missing: list[str] = []
    failed: list[dict[str, Any]] = []
    ok_keys: set[str] = set()
    for meta in sorted(segments_meta, key=lambda m: int(m["idx"])):
        rec = latest.get(meta["key"])
        if rec is None:
            missing.append(meta["key"])
            continue
        if not rec.get("ok"):
            failed.append(
                {"idx": rec.get("idx"), "key": meta["key"], "error_kind": rec.get("error_kind"), "attempt": rec.get("attempt")}
            )
            continue
        ok_keys.add(meta["key"])
        seg_cards: list[dict[str, Any]] = []
        for card in rec.get("cards") or []:
            total += 1
            if str(card.get("prompt_version") or "") != prompt_version:
                wrong_pv += 1
            h = str(card.get("content_hash") or "")
            if not h or h in seen:
                dupes += 1
                continue
            seen.add(h)
            seg_cards.append(dict(card))
        if seg_cards:
            by_segment.append((meta["key"], seg_cards))
    unique = sum(len(cs) for _, cs in by_segment)
    return StagedCards(by_segment, total, unique, dupes, missing, failed, wrong_pv, ok_keys)


def _segments_meta(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"idx": int(s["idx"]), "key": str(s["key"])} for s in segments]


def covered_event_keys(segments: list[dict[str, Any]], ok_keys: set[str]) -> set[EventKey]:
    """Events whose every planned chunk sits in a segment whose re-extraction succeeded.

    A long event is split into chunks that share its event_id and can land in different
    segments; one failed chunk segment leaves part of the event unread, so the whole event
    counts as uncovered. Chunks the ops/chitchat filter dropped have no segment in any run and
    are not required.
    """
    planned: dict[EventKey, set[int]] = {}
    ok: dict[EventKey, set[int]] = {}
    for seg in segments:
        seg_ok = seg["key"] in ok_keys
        for ev in seg["events"]:
            k = (str(ev.get("agent") or "main"), str(ev.get("session_id") or ""), str(ev.get("event_id") or ""))
            idx = int(ev.get("chunk_idx", 0) or 0)
            planned.setdefault(k, set()).add(idx)
            if seg_ok:
                ok.setdefault(k, set()).add(idx)
    return {k for k, idxs in planned.items() if ok.get(k) == idxs}


def card_coverage(card: dict[str, Any], covered: set[EventKey]) -> str:
    """`full` only when every event the old card was drawn from was re-extracted."""
    agent = str(card.get("agent") or "main")
    sid = str(card.get("session_id") or "")
    eids = [str(e) for e in (card.get("event_ids") or []) if str(e)]
    if not eids and card.get("quote_event_id"):
        eids = [str(card["quote_event_id"])]
    if not eids:
        return "none"
    hits = sum(1 for e in eids if (agent, sid, e) in covered)
    if hits == len(eids):
        return "full"
    return "partial" if hits else "none"


async def apply_run(
    run_dir: Path,
    store: CardStore,
    *,
    retract_prompt_version: str | None = None,
    allow_failures: int = 0,
    allow_incomplete_export: bool = False,
    on_uncovered_collision: str = "refuse",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Swap old cards for staged ones in one transaction; every count is asserted before commit.

    Only old cards whose every source event sits in a successfully re-extracted segment are
    retracted; the rest stay active and are reported, so a partial export or failed segments
    never remove knowledge without a replacement.

    A content_hash collision (same quote, same event: the hash is not version-scoped) is resolved
    by saving the old row's full pre-image, deleting it, and inserting the new card through the
    normal insert path with the old row's rendered_locations carried over, so nothing is dropped
    by `ON CONFLICT DO NOTHING` and the renderer still sees where the old card was written.
    Replacing is a retraction too, so a live colliding row must be fully covered by the same
    rule; otherwise apply refuses (`on_uncovered_collision="refuse"`) or keeps the old row and
    skips that new card (`"keep-old"`), and either way lists them.

    The insert path's (ticker, metric, period) supersede logic may supersede any active card,
    uncovered old cards included: that is normal archive semantics (a newer figure supersedes
    an older one). Every card whose status or supersedes changes that way is reported and its
    date is re-rendered.
    """
    if on_uncovered_collision not in ("refuse", "keep-old"):
        raise RunStateError(f"unknown on_uncovered_collision={on_uncovered_collision!r}")
    plan = load_plan(run_dir)
    retract_prompt_version = check_run_consistency(plan, retract_prompt_version=retract_prompt_version)
    manifest_path = run_dir / APPLY_MANIFEST_FILE
    recovered_pending = False
    if manifest_path.is_file():
        prev = read_json(manifest_path)
        check_run_consistency(plan, prev)
        if prev.get("status") == "pending":
            # A killed apply leaves `pending`; if its transaction never committed the table is
            # untouched (replaced rows included), so it is safe to mark it aborted and go again.
            state = await apply_commit_state(store, prev)
            if state != "not_committed":
                raise RunStateError(
                    f"apply manifest is 'pending' and the table says {state!r}; inspect it, "
                    "then `rollback --assume-applied` if the apply did commit"
                )
            if not dry_run:
                prev.update({"status": "aborted", "error": "recovered: transaction never committed"})
                write_json_atomic(manifest_path, prev)
            recovered_pending = True
        elif prev.get("status") in ("applied", "rolled_back"):
            raise RunStateError(
                f"run already has an apply manifest with status={prev.get('status')!r}"
            )
    prompt_version = str(plan["prompt_version"])
    if prompt_version == retract_prompt_version:
        raise RunStateError("new and retracted prompt versions are the same")
    if int((plan.get("summary") or {}).get("segments") or 0) == 0:
        raise RunStateError("the plan has no segments; nothing to apply")
    incomplete = export_incomplete_reasons(plan)
    if incomplete and not allow_incomplete_export:
        raise RunStateError(
            "the plan's export was incomplete (" + "; ".join(incomplete) + "); "
            "re-plan, or pass --allow-incomplete-export"
        )

    segments = load_segments(run_dir)
    if len(segments) != int(plan["summary"]["segments"]):
        raise RunStateError("segments.jsonl does not match plan.json")
    latest = StagingLedger(run_dir / STAGED_FILE).load_latest()
    allowed_models = {m for m in (plan.get("model"), plan.get("fallback_model")) if m}
    for rec in latest.values():
        if rec.get("ok") and str(rec.get("prompt_version") or "") != prompt_version:
            raise RunStateError(f"staged segment {rec.get('idx')} used prompt_version {rec.get('prompt_version')!r}")
        if rec.get("ok") and allowed_models and rec.get("model_requested") not in allowed_models:
            raise RunStateError(
                f"staged segment {rec.get('idx')} was extracted with {rec.get('model_requested')!r}, "
                f"not the plan's {sorted(allowed_models)}"
            )
    staged = collect_staged_cards(_segments_meta(segments), latest, prompt_version=prompt_version)
    report: dict[str, Any] = {
        "run_id": plan["run_id"],
        "dry_run": dry_run,
        "recovered_pending_manifest": recovered_pending,
        "retract_prompt_version": retract_prompt_version,
        "new_prompt_version": prompt_version,
        "export_incomplete": incomplete,
        "planned_segments": int(plan["summary"]["segments"]),
        "segments_missing_outcome": len(staged.missing_keys),
        "segments_failed": len(staged.failed),
        "failed_segments": staged.failed,
        "staged_cards_total": staged.total,
        "staged_duplicates_in_staging": staged.duplicates_in_staging,
        "staged_cards_unique": staged.unique,
    }
    if staged.missing_keys:
        raise RunStateError(
            f"{len(staged.missing_keys)} planned segment(s) have no outcome yet; run `extract` again"
        )
    if len(staged.failed) > int(allow_failures):
        listed = "\n".join(
            f"  segment {f['idx']} ({f['attempt']}): {f['error_kind']}" for f in staged.failed[:50]
        )
        more = f"\n  ... and {len(staged.failed) - 50} more" if len(staged.failed) > 50 else ""
        raise RunStateError(
            f"{len(staged.failed)} segment(s) failed (allowed {allow_failures}); "
            f"re-run `extract` or pass --allow-failures N\n{listed}{more}"
        )
    if staged.wrong_prompt_version:
        raise RunStateError(f"{staged.wrong_prompt_version} staged card(s) carry another prompt_version")
    covered = covered_event_keys(segments, staged.ok_keys)
    del segments
    if not covered:
        raise RunStateError("no event is covered by a successful segment; nothing to apply")

    all_staged = staged.cards
    if len({str(c["card_id"]) for c in all_staged}) != len(all_staged):
        raise ApplyAssertionError("two staged cards share a card_id with different content_hash")

    committed = False
    try:
        async with store.transaction():
            await ensure_lock_held(store)
            await store.lock_cards_table()
            snapshot = await store.snapshot_statuses()
            pre_live = await store.count_live(retract_prompt_version)
            old_live = [
                r
                for r in snapshot
                if r["prompt_version"] == retract_prompt_version and r["status"] != "retracted"
            ]
            if len(old_live) != pre_live:
                raise ApplyAssertionError(f"snapshot has {len(old_live)} live old cards, count says {pre_live}")

            colliding = await store.fetch_full_rows(
                content_hashes=[str(c["content_hash"]) for c in all_staged],
                card_ids=[str(c["card_id"]) for c in all_staged],
            )
            uncovered_collisions = [
                r for r in colliding if r["status"] != "retracted" and card_coverage(r, covered) != "full"
            ]
            listed_collisions = [
                {"card_id": r["card_id"], "date_et": str(r["date_et"])[:10], "prompt_version": r["prompt_version"]}
                for r in uncovered_collisions
            ]
            if uncovered_collisions and on_uncovered_collision == "refuse":
                raise RunStateError(
                    f"{len(uncovered_collisions)} live old card(s) collide with a new card but are not fully "
                    "covered by successful segments (re-extract them, or pass "
                    "--on-uncovered-collision keep-old):\n"
                    + json.dumps(listed_collisions[:100], ensure_ascii=False, indent=1)
                )
            kept_old_ids = {str(r["card_id"]) for r in uncovered_collisions}
            kept_old_hashes = {str(r["content_hash"]) for r in uncovered_collisions}
            skipped_new: list[str] = []
            by_segment: list[tuple[str, list[dict[str, Any]]]] = []
            for seg_key, seg_cards in staged.by_segment:
                keep = []
                for c in seg_cards:
                    if str(c["card_id"]) in kept_old_ids or str(c["content_hash"]) in kept_old_hashes:
                        skipped_new.append(str(c["card_id"]))
                    else:
                        keep.append(c)
                if keep:
                    by_segment.append((seg_key, keep))
            colliding = [r for r in colliding if str(r["card_id"]) not in kept_old_ids]
            new_cards = [c for _, cs in by_segment for c in cs]
            new_ids = [str(c["card_id"]) for c in new_cards]
            expected_inserts = staged.unique - len(skipped_new)
            colliding_ids = {str(r["card_id"]) for r in colliding}
            colliding_by_hash = {str(r["content_hash"]): r for r in colliding}
            colliding_by_id = {str(r["card_id"]): r for r in colliding}
            carry_over: dict[str, list[dict[str, Any]]] = {}
            for c in new_cards:
                old = colliding_by_hash.get(str(c["content_hash"])) or colliding_by_id.get(str(c["card_id"]))
                if old is not None:
                    carry_over[str(c["card_id"])] = list(old.get("rendered_locations") or [])

            coverage = {str(r["card_id"]): card_coverage(r, covered) for r in old_live}
            to_retract = [
                str(r["card_id"])
                for r in old_live
                if coverage[str(r["card_id"])] == "full" and str(r["card_id"]) not in colliding_ids
            ]
            uncovered = [
                r
                for r in old_live
                if coverage[str(r["card_id"])] != "full" and str(r["card_id"]) not in colliding_ids
            ]
            uncovered_by_date = Counter(str(r["date_et"])[:10] for r in uncovered)
            report.update(
                {
                    "cards_in_table": len(snapshot),
                    "old_live_cards": len(old_live),
                    "old_cards_to_retract": len(to_retract),
                    "uncovered_old_cards": {
                        "count": len(uncovered),
                        "partially_covered": sum(
                            1 for r in uncovered if coverage[str(r["card_id"])] == "partial"
                        ),
                        "by_date": dict(sorted(uncovered_by_date.items())),
                    },
                    "content_hash_collisions": len(colliding) + len(kept_old_ids),
                    "uncovered_collisions": listed_collisions,
                    "new_cards_skipped_keeping_old": len(skipped_new),
                    "collisions_by_prompt_version": dict(
                        Counter(str(r["prompt_version"]) for r in colliding)
                    ),
                    "other_version_live_cards": sum(
                        1
                        for r in snapshot
                        if r["prompt_version"] != retract_prompt_version and r["status"] != "retracted"
                    ),
                    "export_range": list(plan.get("export_since_until") or []),
                }
            )

            touched = TouchedLocations(run_dir / TOUCHED_LOCATIONS_FILE)
            retract_set = set(to_retract)
            for r in snapshot:
                if str(r["card_id"]) in retract_set:
                    touched.add(r["date_et"], r.get("rendered_locations") or [])
            for r in colliding:
                touched.add(r["date_et"], r.get("rendered_locations") or [])

            if not dry_run:
                write_json_atomic(
                    run_dir / SNAPSHOT_FILE,
                    {
                        "run_id": plan["run_id"],
                        "taken_at": datetime.now(timezone.utc).isoformat(),
                        "retract_prompt_version": retract_prompt_version,
                        "cards": snapshot,
                    },
                )
                write_jsonl_atomic(run_dir / REPLACED_ROWS_FILE, colliding)
                touched.save()
                write_json_atomic(
                    manifest_path,
                    {
                        "run_id": plan["run_id"],
                        "status": "pending",
                        "retract_prompt_version": retract_prompt_version,
                        "prompt_version": prompt_version,
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "new_card_ids": new_ids,
                        "replaced_card_ids": sorted(colliding_ids),
                        "retracted_card_ids": to_retract,
                    },
                )

            retracted = await store.retract_ids(to_retract)
            if retracted != len(to_retract):
                raise ApplyAssertionError(f"retracted {retracted} rows, expected {len(to_retract)}")

            deleted = await store.delete_cards(sorted(colliding_ids))
            if deleted != len(colliding):
                raise ApplyAssertionError(f"deleted {deleted} colliding rows, expected {len(colliding)}")

            inserted = superseded = 0
            inserted_ids: list[str] = []
            for seg_key, seg_cards in by_segment:
                ins_cnt, sup_cnt, ins_rows = await store.insert_segment_cards(seg_cards)
                if ins_cnt != len(seg_cards):
                    raise ApplyAssertionError(
                        f"segment {seg_key}: inserted {ins_cnt} of {len(seg_cards)} cards"
                    )
                inserted += ins_cnt
                superseded += sup_cnt
                inserted_ids.extend(str(r["card_id"]) for r in ins_rows)

            replaced = sum(1 for cid in inserted_ids if cid in carry_over)
            fresh = inserted - replaced
            if fresh + replaced + len(skipped_new) != staged.unique or sorted(inserted_ids) != sorted(new_ids):
                raise ApplyAssertionError(
                    f"inserted {fresh} new + {replaced} replaced + {len(skipped_new)} skipped "
                    f"!= {staged.unique} staged"
                )
            relocated = await store.set_rendered_locations(carry_over)
            if relocated != len(carry_over):
                raise ApplyAssertionError(f"carried locations to {relocated} of {len(carry_over)} cards")

            still_live = await store.count_live(retract_prompt_version)
            if still_live != len(uncovered):
                raise ApplyAssertionError(
                    f"{still_live} old cards live after retract, expected the {len(uncovered)} uncovered"
                )
            present = await store.count_present(new_ids)
            if present != expected_inserts:
                raise ApplyAssertionError(f"{present} of {expected_inserts} new cards present before commit")

            post_ids = sorted({str(r["card_id"]) for r in snapshot} | set(new_ids))
            post_state = await store.state_of(post_ids)
            applied_at = await store.db_clock()

            # Cards the insert path's supersede logic changed (or whose pointer the replace
            # delete cleared); their files must be re-rendered too.
            post_by_id = {str(r["card_id"]): r for r in post_state}
            indirect = [
                {
                    "card_id": str(r["card_id"]),
                    "date_et": str(r["date_et"])[:10],
                    "prompt_version": r["prompt_version"],
                    "before": {"status": r["status"], "supersedes": r.get("supersedes")},
                    "after": {
                        "status": post_by_id[str(r["card_id"])]["status"],
                        "supersedes": post_by_id[str(r["card_id"])].get("supersedes"),
                    },
                }
                for r in snapshot
                if str(r["card_id"]) not in retract_set
                and str(r["card_id"]) not in colliding_ids
                and str(r["card_id"]) in post_by_id
                and (
                    post_by_id[str(r["card_id"])]["status"] != r["status"]
                    or post_by_id[str(r["card_id"])].get("supersedes") != r.get("supersedes")
                )
            ]
            indirect_ids = {x["card_id"] for x in indirect}
            for r in snapshot:
                if str(r["card_id"]) in indirect_ids:
                    touched.add(r["date_et"], r.get("rendered_locations") or [])

            affected_dates = sorted(
                {str(r["date_et"])[:10] for r in snapshot if str(r["card_id"]) in retract_set}
                | {str(r["date_et"])[:10] for r in colliding}
                | {str(c.get("date_et") or "")[:10] for c in new_cards if c.get("date_et")}
                | {x["date_et"] for x in indirect}
            )
            report.update(
                {
                    "indirectly_changed_cards": {
                        "count": len(indirect),
                        "status_changed": sum(1 for x in indirect if x["before"]["status"] != x["after"]["status"]),
                        "by_date": dict(sorted(Counter(x["date_et"] for x in indirect).items())),
                        "cards": indirect[:200],
                    },
                    "retracted": retracted,
                    "colliding_rows_deleted": deleted,
                    "inserted_new": fresh,
                    "inserted_replacing_collision": replaced,
                    "superseded_by_insert_path": superseded,
                    "affected_dates": len(affected_dates),
                }
            )
            if dry_run:
                raise _DryRunRollback()
            touched.save()
            write_json_atomic(run_dir / POST_APPLY_FILE, {"applied_at": applied_at, "cards": post_state})
        committed = True
    except _DryRunRollback:
        report["committed"] = False
        return report
    except BaseException as exc:
        if not dry_run and manifest_path.is_file():
            prev = read_json(manifest_path)
            if prev.get("status") == "pending" and not committed:
                prev.update({"status": "aborted", "error": type(exc).__name__})
                write_json_atomic(manifest_path, prev)
        raise

    write_json_atomic(
        manifest_path,
        {
            "run_id": plan["run_id"],
            "status": "applied",
            "retract_prompt_version": retract_prompt_version,
            "prompt_version": prompt_version,
            "models": {"model": plan.get("model"), "fallback_model": plan.get("fallback_model")},
            "extract_runs": plan.get("extract_runs") or [],
            "applied_at": applied_at,
            "post_apply_state_hash": _state_hash(post_state),
            "new_card_ids": new_ids,
            "replaced_card_ids": sorted(colliding_ids),
            "retracted_card_ids": to_retract,
            "skipped_new_card_ids": skipped_new,
            "indirectly_changed_card_ids": sorted(indirect_ids),
            "affected_dates": affected_dates,
            "render_done": {},
            "counts": {k: v for k, v in report.items() if k != "failed_segments"},
        },
    )
    report["committed"] = True
    return report


# --------------------------------------------------------------------------------------------
# rollback
# --------------------------------------------------------------------------------------------


def rollback_render_dates(
    manifest: dict[str, Any], touched: TouchedLocations, snapshot_cards: Iterable[dict[str, Any]]
) -> set[str]:
    """Dates a rollback changes: render rewrites these, and only their stamps are cleared."""
    retracted = {str(x) for x in manifest.get("retracted_card_ids") or []}
    return (
        {str(d)[:10] for d in manifest.get("affected_dates") or []}
        | set(touched.by_date)
        | {str(r["date_et"])[:10] for r in snapshot_cards if str(r["card_id"]) in retracted}
    )


async def rollback_run(
    run_dir: Path,
    store: CardStore,
    *,
    dry_run: bool = True,
    assume_applied: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Undo apply: delete the run's cards, put back replaced rows, restore every snapshot status.

    What comes back from the snapshot: every business column and the embedding of replaced rows
    (re-inserted from their pre-image), and status, supersedes and rendered_locations of every
    row. Only `files_written_at` on the dates the rollback changes (`rollback_dates`: the run's
    affected and touched dates, any snapshot row whose status or supersedes is put back, and
    under `--force` any card that referenced the run's new cards via `supersedes`) is cleared
    to NULL so the next `render` rewrites those files; the rollback is not finished until that
    render has run. Rows on every other date get their snapshot `files_written_at` written back
    (restoring stamps that `insert_cards_batch` may have cleared during apply).

    Refuses when the run's cards changed after apply (a later archive run superseded one, or
    points at one), because rolling back would silently undo that later work; `--force` goes
    ahead, merges the referencing cards' dates into `rollback_dates`, and lists every pointer
    the delete clears.
    """
    manifest_path = run_dir / APPLY_MANIFEST_FILE
    if not manifest_path.is_file():
        raise RunStateError("no apply manifest; nothing to roll back")
    manifest = read_json(manifest_path)
    check_run_consistency(load_plan(run_dir), manifest)
    status = manifest.get("status")
    if status == "pending":
        state = await apply_commit_state(store, manifest)
        if state == "not_committed":
            # Nothing reached the table; restoring would re-insert rows that are still there.
            if not dry_run:
                manifest.update({"status": "aborted", "error": "recovered: transaction never committed"})
                write_json_atomic(manifest_path, manifest)
            raise RunStateError("the pending apply never committed; marked aborted, nothing to roll back")
        if not assume_applied:
            raise RunStateError(
                f"apply manifest is 'pending' and the table says {state!r} (crash between commit "
                "and manifest write?). Inspect it, then pass --assume-applied"
            )
    if status not in ("applied", "pending"):
        raise RunStateError(f"apply manifest status is {status!r}; nothing to roll back")
    snapshot = read_json(run_dir / SNAPSHOT_FILE)
    snap_rows: list[dict[str, Any]] = list(snapshot["cards"])
    replaced_rows = read_inbox_jsonl(run_dir / REPLACED_ROWS_FILE)
    new_ids = [str(x) for x in manifest.get("new_card_ids") or []]
    post_path = run_dir / POST_APPLY_FILE
    post = read_json(post_path) if post_path.is_file() else None
    if post is None and not force:
        raise RunStateError("no post-apply state recorded; pass --force to roll back without the check")

    report: dict[str, Any] = {
        "run_id": manifest.get("run_id"),
        "dry_run": dry_run,
        "snapshot_cards": len(snap_rows),
        "replaced_rows_to_restore": len(replaced_rows),
        "new_cards_in_manifest": len(new_ids),
    }
    try:
        async with store.transaction():
            await ensure_lock_held(store)
            await store.lock_cards_table()

            applied_at = str(post["applied_at"]) if post is not None and post.get("applied_at") else None
            later_refs = await store.later_references(new_ids, applied_at)
            if post is not None:
                expected = {str(r["card_id"]): r for r in post["cards"]}
                current = {str(r["card_id"]): r for r in await store.state_of(sorted(expected))}
                changed = sorted(
                    cid
                    for cid, exp in expected.items()
                    if cid not in current
                    or current[cid].get("status") != exp.get("status")
                    or current[cid].get("supersedes") != exp.get("supersedes")
                )
                later_count = await store.count_created_after(str(post["applied_at"]), new_ids)
                report.update(
                    {
                        "post_apply_state_unchanged": _state_hash(current.values())
                        == manifest.get("post_apply_state_hash"),
                        "changed_since_apply": changed,
                        "later_cards_referencing_run": later_refs,
                        "cards_created_after_apply": later_count,
                    }
                )
                if (changed or later_refs) and not force:
                    raise RollbackConflictError(
                        "cards changed after apply; rolling back would undo later work. "
                        "Pass --force to proceed anyway:\n"
                        + json.dumps(
                            {"changed_since_apply": changed[:100], "later_cards_referencing_run": later_refs[:100]},
                            ensure_ascii=False,
                            indent=1,
                        )
                    )
            else:
                report["later_cards_referencing_run"] = later_refs
            if later_refs:
                report["supersedes_cleared_by_delete"] = [
                    {"card_id": r["card_id"], "was": r["supersedes"]} for r in later_refs
                ]

            new_locs = await store.fetch_locations(new_ids)
            present = len(new_locs)
            deleted = await store.delete_cards(new_ids)
            if deleted != present:
                raise ApplyAssertionError(f"deleted {deleted} new cards, {present} were present")
            reinserted = await store.insert_full_rows(replaced_rows)
            if reinserted != len(replaced_rows):
                raise ApplyAssertionError(f"re-inserted {reinserted} of {len(replaced_rows)} replaced rows")
            # The deleted cards' files must be cleaned by render even if no card remains there.
            touched = TouchedLocations(run_dir / TOUCHED_LOCATIONS_FILE)
            for r in new_locs:
                touched.add(r["date_et"], r.get("rendered_locations") or [])
            # Rows whose state the restore changes, whatever the manifest recorded (a pending
            # manifest carries no affected_dates), plus any post-apply rows whose supersedes FK
            # the delete just nulled under --force.
            current = {str(r["card_id"]): r for r in await store.state_of([str(r["card_id"]) for r in snap_rows])}
            restored_dates = {
                str(r["date_et"])[:10]
                for r in snap_rows
                if (cur := current.get(str(r["card_id"]))) is not None
                and (cur.get("status") != r.get("status") or cur.get("supersedes") != r.get("supersedes"))
            }
            later_ref_dates = {str(r["date_et"])[:10] for r in later_refs if r.get("date_et")}
            rollback_dates = (
                rollback_render_dates(manifest, touched, snap_rows) | restored_dates | later_ref_dates
            )
            restored = await store.restore_statuses(snap_rows, clear_dates=rollback_dates)
            if restored != len(snap_rows):
                raise ApplyAssertionError(f"restored {restored} of {len(snap_rows)} snapshot rows")
            report.update(
                {
                    "new_cards_present": present,
                    "new_cards_missing": len(new_ids) - present,
                    "new_cards_deleted": deleted,
                    "replaced_rows_reinserted": reinserted,
                    "snapshot_rows_restored": restored,
                    "dates_to_rerender": len(rollback_dates),
                }
            )
            if dry_run:
                raise _DryRunRollback()
            touched.save()
    except _DryRunRollback:
        report["committed"] = False
        return report

    manifest.update(
        {
            "status": "rolled_back",
            "rolled_back_at": datetime.now(timezone.utc).isoformat(),
            "rollback_render_pending": True,
            "rollback_dates": sorted(rollback_dates),
        }
    )
    write_json_atomic(manifest_path, manifest)
    write_json_atomic(run_dir / ROLLBACK_REPORT_FILE, report)
    report["committed"] = True
    return report


# --------------------------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------------------------


def planned_render_locations(
    cards: list[dict[str, Any]],
    *,
    ticker_to_workspace_id: dict[str, str],
    home_workspace_id: str | None,
) -> dict[str, list[dict[str, Any]]]:
    """Every file the archive renderer may write for these cards, by date, before it writes.

    Mirrors the routing in `render_and_write_conversation_digests`: a mapped ticker goes to its
    company workspace, anything else to Home. It deliberately over-approximates (Home is listed
    for every date with an active card): a location listed here and then not written is only
    `rm -f`'d by a later render, while a location written but not listed would be orphaned if
    the batch fails and the run is rolled back.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for c in cards:
        if str(c.get("status") or "active") != "active":
            continue
        d = str(c.get("date_et") or "")[:10]
        if not d:
            continue
        locs = out.setdefault(d, [])
        for t in (str(x).strip().upper() for x in (c.get("tickers") or []) if str(x).strip()):
            ws = ticker_to_workspace_id.get(t)
            if ws:
                locs.append(
                    {"target": "company", "ticker": t, "workspace_id": ws, "path": render_mod.company_conversation_path(d)}
                )
        locs.append(
            {"target": "home", "ticker": None, "workspace_id": home_workspace_id, "path": render_mod.topic_conversation_path(d)}
        )
    return out


def ghost_cards(touched: TouchedLocations, dates: Iterable[str]) -> list[dict[str, Any]]:
    """Retracted placeholder cards carrying every touched location of a date.

    The archive renderer removes a location only when some card on that date lists it in
    `rendered_locations` and no active card targets it any more; for an empty date it writes
    nothing and removes every such location. A ghost makes it do that for files whose cards
    are gone, while still rewriting any location an active card claims.
    """
    out = []
    for d in dates:
        locs = touched.by_date.get(d) or []
        if locs:
            out.append(
                {
                    "card_id": f"{GHOST_CARD_PREFIX}{d}",
                    "status": "retracted",
                    "date_et": d,
                    "tickers": [],
                    "rendered_locations": [dict(x) for x in locs],
                }
            )
    return out


async def render_run(
    run_dir: Path,
    store: CardStore,
    *,
    dry_run: bool = True,
    dates_per_batch: int = 5,
    embed_batch: int = 100,
    embed_fn: Callable[..., Awaitable[tuple[list[list[float]], str]]] = embed_texts_via_bridge,
    render_fn: Callable[..., Awaitable[dict[str, Any]]] = render_and_write_conversation_digests,
) -> dict[str, Any]:
    """Embed the run's cards, then re-render every date the run touched with the archive's renderer.

    Every date is rewritten (not only unrendered ones) so files of deleted or retracted cards
    are removed; progress is kept in `render_progress.json` per phase, so an interrupted render
    resumes, and the advisory lock is re-checked before every batch.
    """
    manifest_path = run_dir / APPLY_MANIFEST_FILE
    if not manifest_path.is_file():
        raise RunStateError("no apply manifest; run `apply --yes` first")
    manifest = read_json(manifest_path)
    check_run_consistency(load_plan(run_dir), manifest)
    phase = manifest.get("status")
    if phase not in ("applied", "rolled_back"):
        raise RunStateError(f"apply manifest status is {phase!r}")
    new_ids = [str(x) for x in manifest.get("new_card_ids") or []] if phase == "applied" else []
    touched = TouchedLocations(run_dir / TOUCHED_LOCATIONS_FILE)
    affected = set(manifest.get("affected_dates") or []) | set(touched.by_date)
    if phase == "rolled_back":
        snapshot = read_json(run_dir / SNAPSHOT_FILE)
        affected |= rollback_render_dates(manifest, touched, snapshot["cards"])
        affected |= set(manifest.get("rollback_dates") or [])

    progress_path = run_dir / RENDER_PROGRESS_FILE
    progress = read_json(progress_path) if progress_path.is_file() else {}
    if progress.get("phase") != phase:
        progress = {"phase": phase, "done_dates": []}
    done = set(progress["done_dates"])
    dates = sorted(d for d in affected if d not in done)

    pending_embed = await store.cards_missing_embedding(new_ids, limit=len(new_ids) or 1) if new_ids else []
    report: dict[str, Any] = {
        "run_id": manifest.get("run_id"),
        "dry_run": dry_run,
        "phase": phase,
        "affected_dates": len(affected),
        "dates_already_rendered": len(done & affected),
        "dates_to_render": len(dates),
        "dates_with_touched_locations": sum(1 for d in dates if touched.by_date.get(d)),
        "render_batches": math.ceil(len(dates) / max(1, dates_per_batch)) if dates else 0,
        "cards_missing_embedding": len(pending_embed),
    }
    if dry_run:
        return report

    embedded = 0
    embedding_model = None
    while new_ids:
        await ensure_lock_held(store)
        batch = await store.cards_missing_embedding(new_ids, limit=embed_batch)
        if not batch:
            break
        vecs, embedding_model = await embed_fn(
            [str(r.get("search_text") or "") for r in batch], task_type="RETRIEVAL_DOCUMENT"
        )
        if len(vecs) != len(batch):
            raise ApplyAssertionError(f"embedder returned {len(vecs)} vectors for {len(batch)} cards")
        for row, vec in zip(batch, vecs):
            embedded += await store.set_embedding(str(row["card_id"]), vec)
        logger.info("Embedded %d/%d cards", embedded, len(pending_embed))

    rendered = 0
    removed_files = 0
    owner = archive_owner_user_id()
    # Resolved once and handed to the renderer, so the planned locations below and the files
    # it writes come from the same workspace mapping.
    ticker_map, home_ws = await render_mod._resolve_workspace_map(user_id=owner, conn=store.conn)
    step = max(1, dates_per_batch)
    for i in range(0, len(dates), step):
        await ensure_lock_held(store)
        batch_dates = dates[i : i + step]
        cards = await db_conv.list_cards_for_dates(batch_dates, active_only=False, conn=store.conn)
        # Record intent before any write: if this batch dies half-written and the run is then
        # rolled back, the next render still knows every file it may have created.
        planned = planned_render_locations(cards, ticker_to_workspace_id=ticker_map, home_workspace_id=home_ws)
        for d, locs in planned.items():
            touched.add(d, locs)
        touched.save()
        cards.extend(ghost_cards(touched, batch_dates))
        out = await render_fn(
            cards,
            target_dates=batch_dates,
            user_id=owner,
            ticker_to_workspace_id=ticker_map,
            home_workspace_id=home_ws,
            conn=store.conn,
        )
        card_locations = {
            cid: locs
            for cid, locs in (out.get("card_locations") or {}).items()
            if not str(cid).startswith(GHOST_CARD_PREFIX)
        }
        ok_dates = out.get("rendered_dates")
        ok_dates = list(batch_dates) if ok_dates is None else list(ok_dates)
        if ok_dates:
            await db_conv.mark_dates_rendered(ok_dates, card_locations=card_locations, conn=store.conn)
        by_id = {str(c.get("card_id")): c for c in cards}
        for cid, locs in card_locations.items():
            touched.add((by_id.get(str(cid)) or {}).get("date_et"), locs)
        touched.save()
        done.update(ok_dates)
        progress["done_dates"] = sorted(done)
        write_json_atomic(progress_path, progress)
        rendered += len(ok_dates)
        removed_files += len(out.get("removed_files") or [])
        logger.info("Rendered %d/%d dates", rendered, len(dates))

    complete = affected <= done
    manifest = read_json(manifest_path)
    if complete:
        manifest.setdefault("render_done", {})[phase] = datetime.now(timezone.utc).isoformat()
        if phase == "rolled_back":
            manifest["rollback_render_pending"] = False
        write_json_atomic(manifest_path, manifest)
    report.update(
        {
            "embedded": embedded,
            "embedding_model": embedding_model,
            "rendered_dates": rendered,
            "removed_stale_files": removed_files,
            "complete": complete,
        }
    )
    return report


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


async def _cmd_plan(args: argparse.Namespace) -> int:
    cfg = get_dalton_settings().conversations
    run_id = args.run_id or new_run_id()
    run_dir = run_dir_for(run_id)
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RunStateError(f"{run_dir} already exists; pick another --run-id")
    if cfg.prompt_version == args.retract_prompt_version:
        raise RunStateError("settings prompt_version equals the version being retracted")

    conn = await _open_autocommit_conn(resolve_dsn(args))
    try:
        store = CardStore(conn)
        async with store.transaction():
            await store.set_read_only()
            lo, hi = await store.date_range(args.retract_prompt_version)
            by_date_rows = await store.counts_by_date()
    finally:
        await conn.close()
    if lo is None or hi is None:
        raise RunStateError(f"no cards with prompt_version={args.retract_prompt_version!r}")
    since = date.fromisoformat(args.since) if args.since else lo - timedelta(days=1)
    until = date.fromisoformat(args.until) if args.until else hi + timedelta(days=1)

    agents = [a.strip() for a in str(args.agents).split(",") if a.strip()]
    ocw_root = Path(args.ocw_agents_root)
    present_agents, missing_agents = agent_sqlite_status(agents, ocw_root)
    with tempfile.TemporaryDirectory(prefix="dalton-reextract-") as tmp:
        tmp_dir = Path(tmp)
        exp = run_export(
            agents=agents,
            ocw_agents_root=ocw_root,
            inbox_dir=tmp_dir / "inbox",
            cursors_file=tmp_dir / "cursors.json",
            since_date=since,
            until_date=until,
            ignore_cursors=True,
            persist_cursors=False,
        )
        raw_events = read_inbox_jsonl(exp.inbox_path)
    planned = build_planned_segments(
        raw_events,
        since_date=since,
        until_date=until,
        max_gap_minutes=cfg.max_segment_gap_minutes,
        max_segment_chars=cfg.max_segment_chars,
        max_segment_events=cfg.max_segment_events,
    )
    summary = summarize_plan(planned, concurrency=args.concurrency, p50_seconds=args.p50_seconds)

    cards_by_date: dict[str, dict[str, int]] = {}
    for r in by_date_rows:
        slot = cards_by_date.setdefault(str(r["date_et"]), {})
        slot[f"{r['prompt_version']}:{r['status']}"] = int(r["n"])

    plan = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "retract_prompt_version": args.retract_prompt_version,
        "prompt_version": cfg.prompt_version,
        "model": args.model,
        "fallback_model": args.fallback_model or None,
        "extract_runs": [],
        "card_date_range": [lo.isoformat(), hi.isoformat()],
        "export_since_until": [since.isoformat(), until.isoformat()],
        "agents": agents,
        "export": {
            "agents_present": present_agents,
            "missing_agents": missing_agents,
            "events_scanned": exp.events_scanned,
            "events_exported": exp.events_exported,
            "events_skipped_ops": exp.events_skipped_ops,
            "events_skipped_chitchat": exp.events_skipped_chitchat,
            "events_skipped_transcript": exp.events_skipped_transcript,
            "events_parse_failed": exp.events_parse_failed,
        },
        "summary": summary,
        "segments_meta": _segments_meta(planned),
        "cards_by_date": cards_by_date,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(run_dir, 0o700)
    write_jsonl_atomic(run_dir / SEGMENTS_FILE, planned)
    write_json_atomic(run_dir / PLAN_FILE, plan)
    print(f"run_id: {run_id}")
    print(f"run dir: {run_dir}")
    print(f"old cards ({args.retract_prompt_version}) span {lo} .. {hi}; export {since} .. {until}")
    print(f"new prompt_version: {cfg.prompt_version}")
    for reason in export_incomplete_reasons(plan):
        print(f"WARNING: export incomplete: {reason} (apply will refuse without --allow-incomplete-export)")
    if not planned:
        print("WARNING: the plan has no segments; apply will refuse it")
    print(
        f"events exported: {exp.events_exported}; segments: {summary['segments']}; "
        f"total chars: {summary['total_chars']}"
    )
    print(
        f"ETA at concurrency {summary['concurrency']} x p50 {summary['p50_seconds']:.0f}s: "
        f"{summary['eta']} ({summary['eta_seconds']:.0f}s; fallback retries not included)"
    )
    print("existing cards per date (prompt_version:status=count):")
    for d in sorted(cards_by_date):
        parts = ", ".join(f"{k}={v}" for k, v in sorted(cards_by_date[d].items()))
        print(f"  {d}: {parts}")
    return EXIT_CODE_OK


async def _cmd_extract(args: argparse.Namespace) -> int:
    cfg_conv = get_dalton_settings().conversations
    run_dir = run_dir_for(args.run_id)
    plan = load_plan(run_dir)
    segments = load_segments(run_dir)
    if len(segments) != int(plan["summary"]["segments"]):
        raise RunStateError("segments.jsonl does not match plan.json")
    model = args.model or plan.get("model") or DEFAULT_MODEL
    fallback = plan.get("fallback_model") if args.fallback_model is None else (args.fallback_model or None)
    if plan.get("model") and model != plan["model"]:
        raise RunStateError(f"--model {model!r} does not match the plan's {plan['model']!r}")
    if "fallback_model" in plan and fallback != plan.get("fallback_model"):
        raise RunStateError(
            f"--fallback-model {fallback!r} does not match the plan's {plan.get('fallback_model')!r}"
        )
    from src.dalton.llm.complete import resolve_model_billing_and_provider

    billing, _provider = resolve_model_billing_and_provider(model)
    if args.respect_budget:
        banner = f"model {model} (billing={billing}): hourly budget gate on (--respect-budget)"
    else:
        banner = (
            f"{'cash' if billing != 'oauth' else 'subscription'} model {model} (billing={billing}), "
            "budget gate bypassed by operator approval"
        )
    print(banner, flush=True)
    logger.warning("%s", banner)
    plan.setdefault("extract_runs", []).append(
        {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "billing": billing,
            "fallback_model": fallback,
            "concurrency": int(args.concurrency),
            "respect_budget": bool(args.respect_budget),
            "budget_gate_bypassed_by_operator_approval": not args.respect_budget,
            "limit": args.limit,
        }
    )
    write_json_atomic(run_dir / PLAN_FILE, plan)
    cfg = ExtractConfig(
        model=model,
        fallback_model=fallback,
        prompt_version=str(plan["prompt_version"]),
        concurrency=args.concurrency,
        limit=args.limit,
        respect_budget=bool(args.respect_budget),
        hourly_budget=int(cfg_conv.hourly_token_budget),
        initial_backoff_s=float(cfg_conv.rate_limit_initial_backoff_seconds),
        max_backoff_s=float(cfg_conv.rate_limit_max_backoff_seconds),
        progress_every=args.progress_every,
    )
    from src.server.database.pool import get_or_create_pool

    pool = get_or_create_pool()
    await pool.open(wait=True)
    try:
        llm_service = await _build_shared_llm_service()
        summary = await run_extraction(
            segments,
            StagingLedger(run_dir / STAGED_FILE),
            cfg,
            user_id=archive_billing_user_id(),
            llm_service=llm_service,
        )
    finally:
        await pool.close()
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=1))
    remaining = summary.planned - summary.already_staged - summary.ok
    return EXIT_CODE_OK if remaining == 0 else EXIT_CODE_INCOMPLETE


async def _guarded(
    fn: Callable[[CardStore], Awaitable[dict[str, Any]]], *, dsn: str | None = None
) -> dict[str, Any]:
    enabled = _archive_schedule_enabled()
    if enabled is not False:
        logger.warning(
            "The daily archive schedule is %s; a run that fires while this holds the lock exits 2.",
            "on" if enabled else "unreadable",
        )
    async with archive_guard(open_conn=lambda: _open_autocommit_conn(dsn)) as lock_conn:
        return await fn(CardStore(lock_conn))


async def _cmd_apply(args: argparse.Namespace) -> int:
    run_dir = run_dir_for(args.run_id)
    report = await _guarded(
        lambda store: apply_run(
            run_dir,
            store,
            retract_prompt_version=args.retract_prompt_version,
            allow_failures=args.allow_failures,
            allow_incomplete_export=bool(args.allow_incomplete_export),
            on_uncovered_collision=args.on_uncovered_collision,
            dry_run=not args.yes,
        ),
        dsn=resolve_dsn(args),
    )
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    if not args.yes:
        print("dry run: the transaction ran and was rolled back; pass --yes to commit")
    return EXIT_CODE_OK


async def _cmd_render(args: argparse.Namespace) -> int:
    run_dir = run_dir_for(args.run_id)
    dsn = resolve_dsn(args)
    if dsn and args.yes:
        # The renderer resolves workspaces and writes files through the app pool; mixing that
        # with a scratch card table would write real files from scratch data.
        raise RunStateError("render --yes cannot target --dsn; use it only with a dry run")
    from src.server.database.pool import get_or_create_pool

    # The renderer resolves workspace backends through the pool; our own SQL stays on the lock conn.
    pool = get_or_create_pool()
    await pool.open(wait=True)
    try:
        report = await _guarded(
            lambda store: render_run(
                run_dir,
                store,
                dry_run=not args.yes,
                dates_per_batch=args.dates_per_batch,
                embed_batch=args.embed_batch,
            ),
            dsn=dsn,
        )
    finally:
        try:
            from src.server.services.workspace_manager import WorkspaceManager

            wm = WorkspaceManager.current()
            if wm is not None:
                await wm.shutdown()
        except Exception:
            pass
        await pool.close()
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    if not args.yes:
        print("dry run: nothing embedded or written; pass --yes to render")
    return EXIT_CODE_OK if (not args.yes or report.get("complete")) else EXIT_CODE_INCOMPLETE


async def _cmd_rollback(args: argparse.Namespace) -> int:
    run_dir = run_dir_for(args.run_id)
    report = await _guarded(
        lambda store: rollback_run(
            run_dir,
            store,
            dry_run=not args.yes,
            assume_applied=bool(args.assume_applied),
            force=bool(args.force),
        ),
        dsn=resolve_dsn(args),
    )
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    if not args.yes:
        print("dry run: the transaction ran and was rolled back; pass --yes to commit")
    else:
        print(f"not finished: run `render --run-id {args.run_id} --yes` to restore the old files")
    return EXIT_CODE_OK


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Re-extract all archived conversation cards with a new model/prompt version",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    def _confirm(p: argparse.ArgumentParser) -> None:
        g = p.add_mutually_exclusive_group()
        g.add_argument("--dry-run", action="store_true", default=True, help="Default: compute and print only")
        g.add_argument("--yes", action="store_true", help="Actually commit")

    def _dsn(p: argparse.ArgumentParser) -> None:
        p.add_argument("--dsn", default=None, help=f"Scratch database DSN (or ${DSN_ENV}); default: app DB")

    p_plan = sub.add_parser("plan", help="Read-only: export, segment, print size and ETA")
    p_plan.add_argument("--run-id", default=None)
    p_plan.add_argument("--retract-prompt-version", default=DEFAULT_RETRACT_PROMPT_VERSION)
    p_plan.add_argument("--since", default=None, help="Override export start (default: min date_et - 1)")
    p_plan.add_argument("--until", default=None, help="Override export end (default: max date_et + 1)")
    p_plan.add_argument("--agents", default=",".join(DEFAULT_AGENTS))
    p_plan.add_argument("--ocw-agents-root", default=str(DEFAULT_OCW_AGENTS_ROOT))
    p_plan.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p_plan.add_argument("--p50-seconds", type=float, default=DEFAULT_P50_SECONDS)
    p_plan.add_argument("--model", default=DEFAULT_MODEL, help="Extractor model recorded in the plan")
    p_plan.add_argument("--fallback-model", default=DEFAULT_FALLBACK_MODEL, help="'' disables the fallback pass")
    _dsn(p_plan)

    p_ext = sub.add_parser("extract", help="Extract into the run's staging ledger (resumable)")
    p_ext.add_argument("--run-id", required=True)
    p_ext.add_argument("--model", default=None, help="Default and required: the plan's model")
    p_ext.add_argument("--fallback-model", default=None, help="Default and required: the plan's fallback model")
    p_ext.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p_ext.add_argument("--limit", type=int, default=None, help="Only this many pending segments (smoke test)")
    p_ext.add_argument("--respect-budget", action="store_true", help="Pace with the hourly token budget")
    p_ext.add_argument("--progress-every", type=int, default=DEFAULT_PROGRESS_EVERY)

    p_apply = sub.add_parser("apply", help="Swap old cards for staged ones in one transaction")
    p_apply.add_argument("--run-id", required=True)
    p_apply.add_argument("--retract-prompt-version", default=None, help="Must match the plan if given")
    _dsn(p_apply)
    p_apply.add_argument("--allow-failures", type=int, default=0)
    p_apply.add_argument("--allow-incomplete-export", action="store_true")
    p_apply.add_argument(
        "--on-uncovered-collision",
        choices=("refuse", "keep-old"),
        default="refuse",
        help="A live old card colliding with a new one but not fully covered: refuse, or keep it and skip the new card",
    )
    _confirm(p_apply)

    p_render = sub.add_parser("render", help="Embed new cards and re-render every touched date")
    p_render.add_argument("--run-id", required=True)
    p_render.add_argument("--dates-per-batch", type=int, default=5)
    p_render.add_argument("--embed-batch", type=int, default=100)
    _dsn(p_render)
    _confirm(p_render)

    p_rb = sub.add_parser("rollback", help="Restore the pre-apply card state of a run")
    p_rb.add_argument("--run-id", required=True)
    p_rb.add_argument("--assume-applied", action="store_true")
    p_rb.add_argument("--force", action="store_true", help="Roll back even if cards changed after apply")
    _dsn(p_rb)
    _confirm(p_rb)
    return parser


COMMANDS: dict[str, Callable[[argparse.Namespace], Awaitable[int]]] = {
    "plan": _cmd_plan,
    "extract": _cmd_extract,
    "apply": _cmd_apply,
    "render": _cmd_render,
    "rollback": _cmd_rollback,
}


def main(argv: list[str] | None = None) -> int:
    configure_archive_logging()
    args = build_arg_parser().parse_args(argv)
    try:
        return asyncio.run(COMMANDS[args.subcommand](args))
    except ArchiveBusyError as exc:
        print(f"refused: {exc}\n\n{PAUSE_HINT}", file=sys.stderr)
        return EXIT_CODE_LOCK_HELD
    except (RunStateError, ApplyAssertionError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_CODE_INCOMPLETE


if __name__ == "__main__":
    sys.exit(main())
