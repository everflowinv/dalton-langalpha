"""Unit tests for the one-off conversation re-extraction script.

Every database interaction goes through `CardStore`, replaced here by a `create_autospec`
double backed by an in-memory table that honours the real constraints that matter: UNIQUE
content_hash with `ON CONFLICT DO NOTHING`, card_id primary key, `supersedes ON DELETE SET NULL`,
and transaction rollback on exception. The SQL itself is proven on Postgres by
`scripts/dalton/reextract_scratch_check.sh`.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from psycopg import AsyncConnection, AsyncCursor, AsyncTransaction

from scripts.dalton import reextract_conversations as rx
from src.dalton.conversations import render as render_mod
from src.dalton.conversations.extractor import (
    SegmentExtractionOutcome,
    compute_content_hash,
    extract_segment_cards,
)
from src.dalton.database import conversations as db_conv

OLD_PV = "conv_card_v1"
NEW_PV = "conv_card_v2_3"
ALL_OLD_EVENTS = ("ev_shared", "ev_b", "ev_c")
# Originals to spec from: a test may install doubles twice, and a double cannot be specced.
_LIST_CARDS = db_conv.list_cards_for_dates
_MARK_RENDERED = db_conv.mark_dates_rendered
_OWNER = rx.archive_owner_user_id
_RESOLVE_WS = render_mod._resolve_workspace_map


# ------------------------------------------------------------------------------------------
# In-memory table behind an autospec CardStore
# ------------------------------------------------------------------------------------------


def _row(seed: str, *, pv: str = OLD_PV, status: str = "active", **kw: Any) -> dict[str, Any]:
    quote = kw.pop("quote", f"quote {seed}")
    ev = kw.pop("quote_event_id", f"ev_{seed}")
    h = compute_content_hash(quote, ev)
    row = {
        "card_id": f"cc_{h[:24]}",
        "source_type": "conversation",
        "agent": "main",
        "session_id": "s1",
        "session_key": "s1",
        "generation": "",
        "channel": "",
        "chat_type": "",
        "event_ids": [ev],
        "seq_range": [1, 1],
        "ts": "2026-05-07T14:00:00+00:00",
        "date_et": "2026-05-07",
        "speaker": {"role": "owner", "name": "pm"},
        "speaker_role": "owner",
        "tickers": [],
        "entities": [],
        "sectors": [],
        "topic_label": "t",
        "type": "opinion",
        "claim": f"claim {seed} {pv}",
        "figures": [],
        "quote": quote,
        "quote_event_id": ev,
        "cited_sources": [],
        "confidence": 0.8,
        "status": status,
        "supersedes": None,
        "extractor_model": "gemini-4-argon-medium-oauth" if pv == OLD_PV else "deepseek-flash",
        "prompt_version": pv,
        "content_hash": h,
        "search_text": f"search {seed}",
        "embedding": "[0.1,0.2]",
        "rendered_locations": [
            {"path": "research/topics/conversations/2026-05-07.md", "target": "home", "ticker": None, "workspace_id": "ws_home"}
        ],
        "files_written_at": "2026-05-08 01:00:00+00",
        "created_at": "2026-05-08 01:00:00+00",
    }
    row.update(kw)
    return row


def _staged_card(seed: str, **kw: Any) -> dict[str, Any]:
    card = _row(seed, pv=NEW_PV, **kw)
    for k in ("embedding", "rendered_locations", "files_written_at"):
        card.pop(k)
    return card


class MemoryTable:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows: dict[str, dict[str, Any]] = {r["card_id"]: copy.deepcopy(r) for r in rows}
        self.commits = 0
        self.clock = "2026-10-23 12:00:00+00"

    def by_hash(self, h: str) -> dict[str, Any] | None:
        return next((r for r in self.rows.values() if r["content_hash"] == h), None)


def make_store(table: MemoryTable, *, lock_states: list[bool] | None = None) -> rx.CardStore:
    store = create_autospec(rx.CardStore, instance=True)
    store.conn = create_autospec(AsyncConnection, instance=True)

    @asynccontextmanager
    async def _tx():
        saved = copy.deepcopy(table.rows)
        try:
            yield store
        except BaseException:
            table.rows = saved
            raise
        table.commits += 1

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    async def _lock_held(*_a: Any, **_k: Any) -> bool:
        return lock_states.pop(0) if lock_states else True

    async def _clock() -> str:
        return table.clock

    async def _snapshot() -> list[dict[str, Any]]:
        keys = (
            "card_id", "status", "supersedes", "prompt_version", "content_hash", "agent", "session_id",
            "event_ids", "quote_event_id", "date_et", "rendered_locations", "files_written_at",
        )
        return [{k: copy.deepcopy(r[k]) for k in keys} for r in sorted(table.rows.values(), key=lambda r: r["card_id"])]

    async def _count_live(pv: str) -> int:
        return sum(1 for r in table.rows.values() if r["prompt_version"] == pv and r["status"] != "retracted")

    async def _retract_ids(ids: list[str]) -> int:
        n = 0
        for cid in ids:
            r = table.rows.get(cid)
            if r is not None and r["status"] != "retracted":
                r["status"] = "retracted"
                r["files_written_at"] = None
                n += 1
        return n

    async def _fetch_full(*, content_hashes: list[str], card_ids: list[str]) -> list[dict[str, Any]]:
        hs, ids = set(content_hashes), set(card_ids)
        return [copy.deepcopy(r) for r in sorted(table.rows.values(), key=lambda r: r["card_id"]) if r["content_hash"] in hs or r["card_id"] in ids]

    async def _delete(ids: list[str]) -> int:
        n = 0
        for cid in ids:
            if table.rows.pop(cid, None) is not None:
                n += 1
                for r in table.rows.values():
                    if r["supersedes"] == cid:
                        r["supersedes"] = None
        return n

    async def _insert_segment(cards: list[dict[str, Any]]) -> tuple[int, int, list[dict[str, Any]]]:
        tickers = {t for c in cards for t in (c.get("tickers") or [])}
        existing_active = [
            r for r in table.rows.values()
            if r["status"] == "active" and tickers.intersection(r.get("tickers") or [])
        ]
        inserted = []
        for c in cards:
            if table.by_hash(c["content_hash"]) is not None:
                continue  # ON CONFLICT (content_hash) DO NOTHING
            if c["card_id"] in table.rows:
                raise RuntimeError("UniqueViolation card_id")
            row = {**copy.deepcopy(c), "status": "active", "supersedes": None, "embedding": None, "rendered_locations": [], "files_written_at": None}
            table.rows[c["card_id"]] = row
            inserted.append(copy.deepcopy(row))
        if inserted:
            # insert_cards_batch (:427-438) runs SET supersedes = COALESCE(supersedes, ...),
            # files_written_at = NULL on every existing_active row that already has supersedes.
            for r in existing_active:
                if r.get("supersedes"):
                    r["files_written_at"] = None
        return len(inserted), 0, inserted

    async def _set_locs(locs: dict[str, list[dict[str, Any]]]) -> int:
        n = 0
        for cid, ls in locs.items():
            if cid in table.rows:
                table.rows[cid]["rendered_locations"] = copy.deepcopy(ls)
                n += 1
        return n

    async def _count_present(ids: list[str]) -> int:
        return sum(1 for cid in ids if cid in table.rows)

    async def _state_of(ids: list[str]) -> list[dict[str, Any]]:
        return [
            {"card_id": cid, "status": table.rows[cid]["status"], "supersedes": table.rows[cid]["supersedes"]}
            for cid in sorted(ids)
            if cid in table.rows
        ]

    async def _later_refs(ids: list[str], after: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "card_id": r["card_id"],
                "supersedes": r["supersedes"],
                "date_et": r["date_et"],
                "created_at": r["created_at"],
            }
            for r in sorted(table.rows.values(), key=lambda r: r["card_id"])
            if (after is None or r["created_at"] > after)
            and r["supersedes"] in ids
            and r["card_id"] not in ids
        ]

    async def _created_after(after: str, exclude: list[str]) -> int:
        return sum(1 for r in table.rows.values() if r["created_at"] > after and r["card_id"] not in exclude)

    async def _fetch_locs(ids: list[str]) -> list[dict[str, Any]]:
        return [
            {"card_id": cid, "date_et": table.rows[cid]["date_et"], "rendered_locations": copy.deepcopy(table.rows[cid]["rendered_locations"])}
            for cid in sorted(ids)
            if cid in table.rows
        ]

    async def _insert_full(rows: list[dict[str, Any]]) -> int:
        for r in rows:
            if r["card_id"] in table.rows or table.by_hash(r["content_hash"]) is not None:
                raise RuntimeError("UniqueViolation")
            table.rows[r["card_id"]] = copy.deepcopy(r)
        return len(rows)

    async def _restore(rows: list[dict[str, Any]], *, clear_dates: Any) -> int:
        clear = {str(d)[:10] for d in clear_dates if d}
        n = 0
        if rows:
            for r in rows:
                cur = table.rows.get(r["card_id"])
                if cur is None:
                    continue
                cur.update(
                    status=r["status"],
                    supersedes=r.get("supersedes"),
                    rendered_locations=copy.deepcopy(r.get("rendered_locations") or []),
                    files_written_at=None if str(cur["date_et"])[:10] in clear else r.get("files_written_at"),
                )
                n += 1
        if clear:
            for cur in table.rows.values():
                if str(cur["date_et"])[:10] in clear and cur["files_written_at"] is not None:
                    cur["files_written_at"] = None
        return n

    async def _missing_embedding(ids: list[str], limit: int) -> list[dict[str, Any]]:
        out = [
            {"card_id": cid, "search_text": table.rows[cid]["search_text"]}
            for cid in sorted(ids)
            if cid in table.rows and table.rows[cid]["embedding"] is None
        ]
        return out[:limit]

    async def _set_embedding(cid: str, vec: list[float]) -> int:
        if cid in table.rows and table.rows[cid]["embedding"] is None:
            table.rows[cid]["embedding"] = str(vec)
            return 1
        return 0

    store.transaction.side_effect = _tx
    store.lock_held.side_effect = _lock_held
    store.db_clock.side_effect = _clock
    store.lock_cards_table.side_effect = _noop
    store.snapshot_statuses.side_effect = _snapshot
    store.count_live.side_effect = _count_live
    store.retract_ids.side_effect = _retract_ids
    store.fetch_full_rows.side_effect = _fetch_full
    store.delete_cards.side_effect = _delete
    store.insert_segment_cards.side_effect = _insert_segment
    store.set_rendered_locations.side_effect = _set_locs
    store.count_present.side_effect = _count_present
    store.state_of.side_effect = _state_of
    store.later_references.side_effect = _later_refs
    store.count_created_after.side_effect = _created_after
    store.fetch_locations.side_effect = _fetch_locs
    store.insert_full_rows.side_effect = _insert_full
    store.restore_statuses.side_effect = _restore
    store.cards_missing_embedding.side_effect = _missing_embedding
    store.set_embedding.side_effect = _set_embedding
    return store


def _ev(eid: str) -> dict[str, Any]:
    return {"agent": "main", "session_id": "s1", "event_id": eid, "seq": 1, "text": "x", "date_et": "2026-05-07"}


def write_run(
    run_dir: Path,
    staged: list[tuple[str, dict[str, Any] | None]],
    *,
    covered: tuple[str, ...] = ALL_OLD_EVENTS,
    export: dict[str, Any] | None = None,
    events_by_segment: list[list[dict[str, Any]]] | None = None,
) -> None:
    """One planned segment per entry; the first segment carries the `covered` old events.

    A `None` record leaves that segment without an outcome.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    segments = []
    for i, (key, _rec) in enumerate(staged):
        events = [_ev(f"ev_seg{i}")] + ([_ev(e) for e in covered] if i == 0 else [])
        if events_by_segment is not None:
            events = events_by_segment[i]
        segments.append({"idx": i, "key": key, "window": ["2026-05-04", "2026-05-10"], "chars": 1, "events": events})
    rx.write_jsonl_atomic(run_dir / rx.SEGMENTS_FILE, segments)
    rx.write_json_atomic(
        run_dir / rx.PLAN_FILE,
        {
            "run_id": run_dir.name,
            "retract_prompt_version": OLD_PV,
            "prompt_version": NEW_PV,
            "model": "deepseek-flash",
            "fallback_model": "gemini-4-argon-medium-oauth",
            "export_since_until": ["2026-05-06", "2026-05-08"],
            "export": export if export is not None else {"missing_agents": [], "events_parse_failed": 0},
            "summary": {"segments": len(segments)},
            "segments_meta": rx._segments_meta(segments),
        },
    )
    lines = [
        json.dumps({"key": k, "idx": i, **rec}, ensure_ascii=False)
        for i, (k, rec) in enumerate(staged)
        if rec is not None
    ]
    (run_dir / rx.STAGED_FILE).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _ok(cards: list[dict[str, Any]], model: str = "deepseek-flash") -> dict[str, Any]:
    return {"ok": True, "attempt": "primary", "model_requested": model, "model_used": model, "prompt_version": NEW_PV, "cards": cards}


def _seed_table() -> tuple[MemoryTable, dict[str, dict[str, Any]]]:
    a = _row("a", quote="shared quote", quote_event_id="ev_shared")
    b = _row("b", status="superseded")
    c = _row("c")
    b["supersedes"] = a["card_id"]
    old = {"a": a, "b": b, "c": c}
    return MemoryTable([a, b, c]), old


# ------------------------------------------------------------------------------------------
# plan
# ------------------------------------------------------------------------------------------


def test_eta_is_waves_of_concurrency_times_p50() -> None:
    assert rx.estimate_eta_seconds(0, 8, 116) == 0.0
    assert rx.estimate_eta_seconds(8, 8, 116) == 116.0
    assert rx.estimate_eta_seconds(9, 8, 116) == 232.0
    assert rx.estimate_eta_seconds(1000, 8, 116) == 125 * 116.0
    assert rx.estimate_eta_seconds(5, 0, 10) == 50.0  # concurrency floors at 1
    assert rx.format_duration(125 * 116.0) == "4h01m"


def _event(seq: int, day: date, text: str, *, minute: int = 0) -> dict[str, Any]:
    ts = datetime(day.year, day.month, day.day, 15, minute, tzinfo=timezone.utc)
    return {
        "agent": "main",
        "session_id": "s1",
        "event_id": f"e{seq}",
        "seq": seq,
        "ts": ts.isoformat(),
        "created_at_ms": int(ts.timestamp() * 1000),
        "date_et": day.isoformat(),
        "speaker": {"role": "owner"},
        "text": text,
    }


def test_plan_segments_like_backfill_and_sums_chars() -> None:
    d1 = date(2026, 5, 4)
    d2 = d1 + timedelta(days=9)  # next weekly window
    long_text = "NVDA 数据中心收入同比增长，毛利率维持在七成以上，我们认为估值仍有空间。" * 3
    events = [
        _event(1, d1, long_text),
        _event(2, d1, "好的", minute=1),  # chitchat, dropped before segmentation
        _event(3, d1, long_text, minute=2),
        _event(4, d2, long_text),
    ]
    planned = rx.build_planned_segments(
        events,
        since_date=d1,
        until_date=d2,
        max_gap_minutes=30,
        max_segment_chars=6000,
        max_segment_events=25,
    )
    assert [len(s["events"]) for s in planned] == [2, 1]
    assert [s["window"][0] for s in planned] == [d1.isoformat(), (d1 + timedelta(days=7)).isoformat()]
    assert len({s["key"] for s in planned}) == 2
    assert planned[0]["key"] == rx.segment_key(planned[0]["events"])

    summary = rx.summarize_plan(planned, concurrency=8, p50_seconds=116)
    assert (summary["segments"], summary["events"]) == (2, 3)
    assert summary["total_chars"] == 3 * len(long_text)
    assert summary["eta_seconds"] == 116.0


def test_plan_records_missing_agent_sqlite(tmp_path: Path) -> None:
    db = tmp_path / "main" / "agent" / "openclaw-agent.sqlite"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"")
    present, missing = rx.agent_sqlite_status(["main", "chem"], tmp_path)
    assert (present, missing) == (["main"], ["chem"])
    assert rx.export_incomplete_reasons({"export": {"missing_agents": missing, "events_parse_failed": 0}}) == [
        "agents without SQLite: chem"
    ]
    assert rx.export_incomplete_reasons({"export": {"missing_agents": [], "events_parse_failed": 0}}) == []


# ------------------------------------------------------------------------------------------
# extract
# ------------------------------------------------------------------------------------------


def _segments(n: int) -> list[dict[str, Any]]:
    out = []
    for i in range(n):
        evs = [_event(i + 1, date(2026, 5, 4), f"segment text {i}")]
        out.append({"idx": i, "key": rx.segment_key(evs), "window": ["2026-05-04", "2026-05-10"], "chars": 10, "events": evs})
    return out


def test_extract_stages_outcomes_retries_transient_once_on_fallback_and_resumes(tmp_path: Path) -> None:
    segments = _segments(4)
    calls: list[tuple[str, str]] = []
    fail_seg2 = {"on": True}

    async def fake_extract(segment, *, extractor_model=None, **_kw):
        text = segment[0]["text"]
        calls.append((text, extractor_model))
        transient = SegmentExtractionOutcome(
            ok=False, model_used=extractor_model, status="indeterminate", error_kind="timeout", transient_error=True, error="ReadTimeout: x"
        )
        if text.endswith("1") and extractor_model == "deepseek-flash":
            return transient
        if text.endswith("3"):
            return transient  # fails on both models
        if text.endswith("2") and fail_seg2["on"]:
            return SegmentExtractionOutcome(ok=False, model_used=extractor_model, status="error", error_kind="ValidationError", error="ValidationError: bad json")
        card = _staged_card(text, extractor_model=extractor_model)
        return SegmentExtractionOutcome(ok=True, model_used=extractor_model, cards=[card], total_tokens=10)

    extract_fn = create_autospec(extract_segment_cards, side_effect=fake_extract)
    sleep_fn = create_autospec(asyncio.sleep, return_value=None)
    ledger = rx.StagingLedger(tmp_path / rx.STAGED_FILE)
    cfg = rx.ExtractConfig(model="deepseek-flash", fallback_model="gemini-4-argon-medium-oauth", prompt_version=NEW_PV, concurrency=2, progress_every=1)

    s1 = asyncio.run(rx.run_extraction(segments, ledger, cfg, user_id="u", extract_fn=extract_fn, sleep_fn=sleep_fn))
    assert (s1.attempted, s1.ok, s1.failed, s1.fallback_attempted, s1.fallback_ok) == (4, 2, 2, 2, 1)
    assert s1.models_used == {"deepseek-flash": 1, "gemini-4-argon-medium-oauth": 1}
    # The primary goes through the backoff wrapper (1 + 5 transient retries); the fallback is one call.
    assert calls.count(("segment text 3", "deepseek-flash")) == 6
    assert calls.count(("segment text 3", "gemini-4-argon-medium-oauth")) == 1
    assert calls.count(("segment text 1", "gemini-4-argon-medium-oauth")) == 1
    assert ("segment text 2", "gemini-4-argon-medium-oauth") not in calls  # non-transient
    fb_kwargs = [c.kwargs for c in extract_fn.call_args_list if c.kwargs.get("extractor_model") == "gemini-4-argon-medium-oauth"]
    assert all(k["skip_journal"] is True and k["persist_raw"] is False for k in fb_kwargs)
    latest = ledger.load_latest()
    assert latest[segments[1]["key"]]["attempt"] == "fallback"
    assert latest[segments[1]["key"]]["cards"][0]["extractor_model"] == "gemini-4-argon-medium-oauth"
    assert latest[segments[2]["key"]]["fallback_eligible"] is False
    assert "bad json" not in (tmp_path / rx.STAGED_FILE).read_text(encoding="utf-8")

    # A torn last line (crash mid-write) is ignored on resume and does not swallow the next record.
    with (tmp_path / rx.STAGED_FILE).open("a", encoding="utf-8") as fh:
        fh.write('{"key": "torn')

    calls.clear()
    fail_seg2["on"] = False
    s2 = asyncio.run(rx.run_extraction(segments, ledger, cfg, user_id="u", extract_fn=extract_fn, sleep_fn=sleep_fn))
    assert (s2.already_staged, s2.attempted, s2.ok) == (2, 2, 1)
    assert ("segment text 2", "deepseek-flash") in calls
    assert latest[segments[0]["key"]]["ok"] is True
    assert ledger.load_latest()[segments[2]["key"]]["ok"] is True


def test_ledger_appends_stay_whole_under_high_concurrency(tmp_path: Path) -> None:
    ledger = rx.StagingLedger(tmp_path / rx.STAGED_FILE)

    async def run() -> None:
        await asyncio.gather(*(ledger.append({"key": f"k{i}", "pad": "x" * 5000, "ok": True}) for i in range(200)))

    asyncio.run(run())
    lines = (tmp_path / rx.STAGED_FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 200
    assert {json.loads(line)["key"] for line in lines} == {f"k{i}" for i in range(200)}


# ------------------------------------------------------------------------------------------
# apply
# ------------------------------------------------------------------------------------------


def test_apply_dry_run_reports_counts_and_commits_nothing(tmp_path: Path) -> None:
    table, _old = _seed_table()
    before = copy.deepcopy(table.rows)
    run_dir = tmp_path / "rx_dry"
    write_run(run_dir, [("k0", _ok([_staged_card("n0")])), ("k1", _ok([_staged_card("n1")]))])

    report = asyncio.run(rx.apply_run(run_dir, make_store(table), dry_run=True))
    assert report["committed"] is False
    assert (report["old_live_cards"], report["old_cards_to_retract"], report["retracted"]) == (3, 3, 3)
    assert report["uncovered_old_cards"]["count"] == 0
    assert report["inserted_new"] == 2
    assert table.rows == before
    assert table.commits == 0
    for name in (rx.SNAPSHOT_FILE, rx.APPLY_MANIFEST_FILE, rx.TOUCHED_LOCATIONS_FILE, rx.POST_APPLY_FILE):
        assert not (run_dir / name).exists()


def test_apply_keeps_uncovered_and_partially_covered_old_cards_active(tmp_path: Path) -> None:
    table, old = _seed_table()
    d = _row("d", event_ids=["ev_b", "ev_not_exported"], date_et="2026-05-06")
    table.rows[d["card_id"]] = d
    run_dir = tmp_path / "rx_partial"
    write_run(
        run_dir,
        [("k0", _ok([_staged_card("n0")])), ("k1", {"ok": False, "attempt": "fallback", "error_kind": "timeout", "cards": []})],
        covered=("ev_shared", "ev_b"),
    )
    report = asyncio.run(rx.apply_run(run_dir, make_store(table), allow_failures=1, dry_run=False))
    assert report["committed"] is True
    assert report["retracted"] == 2  # a and b
    assert report["uncovered_old_cards"] == {
        "count": 2,
        "partially_covered": 1,
        "by_date": {"2026-05-06": 1, "2026-05-07": 1},
    }
    assert table.rows[old["c"]["card_id"]]["status"] == "active"
    assert table.rows[d["card_id"]]["status"] == "active"
    assert table.rows[old["a"]["card_id"]]["status"] == "retracted"
    manifest = rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE)
    assert sorted(manifest["retracted_card_ids"]) == sorted([old["a"]["card_id"], old["b"]["card_id"]])
    assert manifest["retract_prompt_version"] == OLD_PV


def test_apply_refuses_empty_incomplete_or_mismatched_runs(tmp_path: Path) -> None:
    table, _ = _seed_table()
    store = make_store(table)

    run_dir = tmp_path / "rx_empty"
    write_run(run_dir, [])
    with pytest.raises(rx.RunStateError, match="no segments"):
        asyncio.run(rx.apply_run(run_dir, store, dry_run=False))

    run_dir = tmp_path / "rx_missing"
    write_run(run_dir, [("k0", _ok([_staged_card("n0")])), ("k1", None)])
    with pytest.raises(rx.RunStateError, match="no outcome"):
        asyncio.run(rx.apply_run(run_dir, store, dry_run=False))

    run_dir = tmp_path / "rx_failed"
    write_run(run_dir, [("k0", _ok([_staged_card("n0")])), ("k1", {"ok": False, "attempt": "fallback", "error_kind": "timeout", "cards": []})])
    with pytest.raises(rx.RunStateError, match="segment 1 \\(fallback\\): timeout"):
        asyncio.run(rx.apply_run(run_dir, store, dry_run=False))

    run_dir = tmp_path / "rx_export"
    write_run(run_dir, [("k0", _ok([_staged_card("n0")]))], export={"missing_agents": ["chem"], "events_parse_failed": 0})
    with pytest.raises(rx.RunStateError, match="agents without SQLite: chem"):
        asyncio.run(rx.apply_run(run_dir, store, dry_run=False))
    report = asyncio.run(rx.apply_run(run_dir, store, allow_incomplete_export=True, dry_run=True))
    assert report["export_incomplete"] == ["agents without SQLite: chem"]

    run_dir = tmp_path / "rx_version"
    write_run(run_dir, [("k0", _ok([_staged_card("n0")]))])
    with pytest.raises(rx.RunStateError, match="does not match the plan"):
        asyncio.run(rx.apply_run(run_dir, store, retract_prompt_version="conv_card_v0", dry_run=True))

    run_dir = tmp_path / "rx_model"
    write_run(run_dir, [("k0", _ok([_staged_card("n0")], model="some-other-model"))])
    with pytest.raises(rx.RunStateError, match="some-other-model"):
        asyncio.run(rx.apply_run(run_dir, store, dry_run=True))
    assert table.commits == 0


def test_apply_count_mismatch_rolls_back_everything(tmp_path: Path) -> None:
    table, _ = _seed_table()
    before = copy.deepcopy(table.rows)
    run_dir = tmp_path / "rx_mismatch"
    write_run(run_dir, [("k0", _ok([_staged_card("n0")]))])
    store = make_store(table)
    real_retract = store.retract_ids.side_effect

    async def retract_reports_extra(ids: list[str]) -> int:
        return await real_retract(ids) + 1  # e.g. a writer slipped a row in

    store.retract_ids.side_effect = retract_reports_extra
    with pytest.raises(rx.ApplyAssertionError, match="retracted 4 rows, expected 3"):
        asyncio.run(rx.apply_run(run_dir, store, dry_run=False))
    assert table.rows == before
    assert table.commits == 0
    assert rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE)["status"] == "aborted"
    report = asyncio.run(rx.apply_run(run_dir, make_store(table), dry_run=False))  # retry allowed
    assert report["committed"] is True


def test_killed_apply_with_pending_manifest_is_recovered_when_nothing_committed(tmp_path: Path) -> None:
    table, old = _seed_table()
    run_dir = tmp_path / "rx_killed"
    collide = _staged_card("x", quote="shared quote", quote_event_id="ev_shared")
    fresh = _staged_card("fresh")
    write_run(run_dir, [("k0", _ok([collide, fresh]))])
    rx.write_json_atomic(
        run_dir / rx.APPLY_MANIFEST_FILE,
        {
            "run_id": run_dir.name,
            "status": "pending",
            "retract_prompt_version": OLD_PV,
            "prompt_version": NEW_PV,
            "new_card_ids": [collide["card_id"], fresh["card_id"]],
            "replaced_card_ids": [old["a"]["card_id"]],
            "retracted_card_ids": [old["b"]["card_id"], old["c"]["card_id"]],
        },
    )
    # Rollback must not touch it: the replaced row is still the old one.
    with pytest.raises(rx.RunStateError, match="never committed"):
        asyncio.run(rx.rollback_run(run_dir, make_store(table), dry_run=False))
    assert rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE)["status"] == "aborted"
    assert table.rows[old["a"]["card_id"]]["prompt_version"] == OLD_PV

    rx.write_json_atomic(run_dir / rx.APPLY_MANIFEST_FILE, {**rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE), "status": "pending"})
    report = asyncio.run(rx.apply_run(run_dir, make_store(table), dry_run=False))
    assert report["recovered_pending_manifest"] is True
    assert report["committed"] is True

    # A pending manifest whose transaction did commit is not silently re-applied.
    rx.write_json_atomic(run_dir / rx.APPLY_MANIFEST_FILE, {**rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE), "status": "pending"})
    with pytest.raises(rx.RunStateError, match="'committed'"):
        asyncio.run(rx.apply_run(run_dir, make_store(table), dry_run=False))


def test_apply_never_silently_drops_a_card_that_on_conflict_would_skip(tmp_path: Path) -> None:
    table, _old = _seed_table()
    before = copy.deepcopy(table.rows)
    run_dir = tmp_path / "rx_drop"
    write_run(run_dir, [("k0", _ok([_staged_card("x", quote="shared quote", quote_event_id="ev_shared")]))])
    store = make_store(table)

    async def blind_fetch(*, content_hashes: list[str], card_ids: list[str]) -> list[dict[str, Any]]:
        return []  # collision lookup misses -> insert path would DO NOTHING

    store.fetch_full_rows.side_effect = blind_fetch
    with pytest.raises(rx.ApplyAssertionError, match="inserted 0 of 1"):
        asyncio.run(rx.apply_run(run_dir, store, dry_run=False))
    assert table.rows == before


def test_apply_replaces_content_hash_collision_with_old_row(tmp_path: Path) -> None:
    table, old = _seed_table()
    run_dir = tmp_path / "rx_collide"
    colliding = _staged_card("x", quote="shared quote", quote_event_id="ev_shared")
    assert colliding["content_hash"] == old["a"]["content_hash"]  # hash ignores prompt version
    dup = copy.deepcopy(colliding)
    fresh = _staged_card("fresh")
    write_run(run_dir, [("k0", _ok([colliding])), ("k1", _ok([dup, fresh]))])

    report = asyncio.run(rx.apply_run(run_dir, make_store(table), dry_run=False))
    assert report["committed"] is True
    assert (report["staged_cards_total"], report["staged_duplicates_in_staging"], report["staged_cards_unique"]) == (3, 1, 2)
    assert (report["content_hash_collisions"], report["inserted_new"], report["inserted_replacing_collision"]) == (1, 1, 1)

    replaced = table.rows[old["a"]["card_id"]]
    assert (replaced["prompt_version"], replaced["status"], replaced["claim"]) == (NEW_PV, "active", colliding["claim"])
    assert replaced["rendered_locations"] == old["a"]["rendered_locations"]
    assert table.rows[old["b"]["card_id"]]["status"] == "retracted"
    assert table.rows[old["b"]["card_id"]]["supersedes"] is None  # ON DELETE SET NULL; snapshot keeps it

    pre = rx.read_json(run_dir / rx.SNAPSHOT_FILE)
    assert {r["card_id"]: r["status"] for r in pre["cards"]} == {o["card_id"]: o["status"] for o in old.values()}
    pre_image = [json.loads(line) for line in (run_dir / rx.REPLACED_ROWS_FILE).read_text().splitlines()]
    assert [r["claim"] for r in pre_image] == [old["a"]["claim"]]
    touched = rx.TouchedLocations(run_dir / rx.TOUCHED_LOCATIONS_FILE)
    assert touched.by_date == {"2026-05-07": [old["a"]["rendered_locations"][0]]}
    manifest = rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE)
    assert manifest["status"] == "applied"
    assert manifest["applied_at"] == table.clock
    assert manifest["post_apply_state_hash"] == rx._state_hash(rx.read_json(run_dir / rx.POST_APPLY_FILE)["cards"])

    with pytest.raises(rx.RunStateError, match="already has an apply manifest"):
        asyncio.run(rx.apply_run(run_dir, make_store(table), dry_run=False))


# ------------------------------------------------------------------------------------------
# rollback
# ------------------------------------------------------------------------------------------


def _applied_run(tmp_path: Path) -> tuple[MemoryTable, dict[str, Any], dict[str, Any], dict[str, Any], Path]:
    table, old = _seed_table()
    run_dir = tmp_path / "rx_rb"
    colliding = _staged_card("x", quote="shared quote", quote_event_id="ev_shared")
    fresh = _staged_card("fresh", date_et="2026-05-09")
    write_run(run_dir, [("k0", _ok([colliding, fresh]))])
    asyncio.run(rx.apply_run(run_dir, make_store(table), dry_run=False))
    return table, old, colliding, fresh, run_dir


def test_rollback_restores_snapshot_round_trip(tmp_path: Path) -> None:
    table, old, _colliding, fresh, run_dir = _applied_run(tmp_path)
    original = {o["card_id"]: o for o in old.values()}
    fresh_loc = {"path": "conversations/2026-05-09.md", "target": "company", "ticker": "NVDA", "workspace_id": "ws_nvda"}
    table.rows[fresh["card_id"]]["rendered_locations"] = [fresh_loc]  # as render would record

    dry = asyncio.run(rx.rollback_run(run_dir, make_store(table), dry_run=True))
    assert dry["committed"] is False and dry["new_cards_deleted"] == 2
    assert table.rows[fresh["card_id"]]["prompt_version"] == NEW_PV

    report = asyncio.run(rx.rollback_run(run_dir, make_store(table), dry_run=False))
    assert report["committed"] is True
    assert (report["replaced_rows_reinserted"], report["snapshot_rows_restored"]) == (1, 3)
    assert report["changed_since_apply"] == [] and report["later_cards_referencing_run"] == []
    assert fresh["card_id"] not in table.rows

    def strip(rows: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {cid: {k: v for k, v in r.items() if k != "files_written_at"} for cid, r in rows.items()}

    assert strip(table.rows) == strip(original)
    assert all(r["files_written_at"] is None for r in table.rows.values())  # render redoes them
    # The deleted card's file location is kept for render, though no card remains on its date.
    touched = rx.TouchedLocations(run_dir / rx.TOUCHED_LOCATIONS_FILE)
    assert touched.by_date["2026-05-09"] == [fresh_loc]
    manifest = rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE)
    assert (manifest["status"], manifest["rollback_render_pending"]) == ("rolled_back", True)
    with pytest.raises(rx.RunStateError):
        asyncio.run(rx.rollback_run(run_dir, make_store(table), dry_run=False))


def test_rollback_clears_files_written_at_only_on_the_runs_dates(tmp_path: Path) -> None:
    table, old = _seed_table()
    # A card on a date the run never touches, already rendered.
    other = _row("other", pv=NEW_PV, date_et="2026-04-01", files_written_at="2026-04-02 01:00:00+00")
    table.rows[other["card_id"]] = copy.deepcopy(other)
    run_dir = tmp_path / "rx_scope"
    colliding = _staged_card("x", quote="shared quote", quote_event_id="ev_shared")
    fresh = _staged_card("fresh", date_et="2026-05-09")
    write_run(run_dir, [("k0", _ok([colliding, fresh]))])
    asyncio.run(rx.apply_run(run_dir, make_store(table), dry_run=False))
    # Render already re-stamped the run's dates.
    for r in table.rows.values():
        r["files_written_at"] = r["files_written_at"] or "2026-10-23 13:00:00+00"

    dry = asyncio.run(rx.rollback_run(run_dir, make_store(table), dry_run=True))
    assert dry["dates_to_rerender"] == 2
    assert all(r["files_written_at"] is not None for r in table.rows.values())

    report = asyncio.run(rx.rollback_run(run_dir, make_store(table), dry_run=False))
    assert report["committed"] is True
    assert table.rows[other["card_id"]]["files_written_at"] == other["files_written_at"]
    assert all(table.rows[o["card_id"]]["files_written_at"] is None for o in old.values())
    manifest = rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE)
    assert manifest["rollback_dates"] == ["2026-05-07", "2026-05-09"]
    dry_render = asyncio.run(rx.render_run(run_dir, make_store(table), dry_run=True))
    assert dry_render["affected_dates"] == 2


def test_rollback_refuses_after_later_archive_activity_unless_forced(tmp_path: Path) -> None:
    table, _old, _colliding, fresh, run_dir = _applied_run(tmp_path)
    later = _row("later", pv=NEW_PV, created_at="2026-10-24 09:00:00+00", supersedes=fresh["card_id"])
    table.rows[later["card_id"]] = later
    table.rows[fresh["card_id"]]["status"] = "superseded"  # a daily run superseded the run's card
    before = copy.deepcopy(table.rows)

    with pytest.raises(rx.RollbackConflictError, match="changed after apply") as exc:
        asyncio.run(rx.rollback_run(run_dir, make_store(table), dry_run=False))
    assert fresh["card_id"] in str(exc.value) and later["card_id"] in str(exc.value)
    assert table.rows == before

    report = asyncio.run(rx.rollback_run(run_dir, make_store(table), dry_run=False, force=True))
    assert report["committed"] is True
    assert report["changed_since_apply"] == [fresh["card_id"]]
    assert report["supersedes_cleared_by_delete"] == [{"card_id": later["card_id"], "was": fresh["card_id"]}]
    assert report["cards_created_after_apply"] == 1
    assert table.rows[later["card_id"]]["supersedes"] is None


def test_rollback_restores_files_written_at_cleared_on_outside_date_by_insert_batch(tmp_path: Path) -> None:
    table, _old = _seed_table()
    prior = _row("prior", pv=NEW_PV, status="superseded", date_et="2026-03-25", files_written_at="2026-03-26 01:00:00+00")
    outside = _row(
        "outside",
        pv=NEW_PV,
        date_et="2026-04-01",
        tickers=["NVDA"],
        supersedes=prior["card_id"],
        files_written_at="2026-04-02 01:00:00+00",
    )
    table.rows[prior["card_id"]] = copy.deepcopy(prior)
    table.rows[outside["card_id"]] = copy.deepcopy(outside)
    run_dir = tmp_path / "rx_outside_stamp"
    fresh = _staged_card("fresh", date_et="2026-05-09", tickers=["NVDA"])
    write_run(run_dir, [("k0", _ok([fresh]))])

    applied = asyncio.run(rx.apply_run(run_dir, make_store(table), dry_run=False))
    assert applied["indirectly_changed_cards"]["count"] == 0
    assert table.rows[outside["card_id"]]["files_written_at"] is None  # cleared by insert_cards_batch

    rolled = asyncio.run(rx.rollback_run(run_dir, make_store(table), dry_run=False))
    assert rolled["committed"] is True
    assert "2026-04-01" not in rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE)["rollback_dates"]
    assert table.rows[outside["card_id"]]["files_written_at"] == outside["files_written_at"]


def test_force_rollback_includes_later_referencing_card_date_and_clears_its_stamp(tmp_path: Path) -> None:
    table, _old, _colliding, fresh, run_dir = _applied_run(tmp_path)
    later = _row(
        "later",
        pv=NEW_PV,
        date_et="2026-05-12",
        created_at="2026-10-24 09:00:00+00",
        files_written_at="2026-10-24 09:05:00+00",
        supersedes=fresh["card_id"],
    )
    table.rows[later["card_id"]] = later
    table.rows[fresh["card_id"]]["status"] = "superseded"

    report = asyncio.run(rx.rollback_run(run_dir, make_store(table), dry_run=False, force=True))
    assert report["committed"] is True
    manifest = rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE)
    assert manifest["rollback_dates"] == ["2026-05-07", "2026-05-09", "2026-05-12"]
    assert table.rows[later["card_id"]]["supersedes"] is None
    assert table.rows[later["card_id"]]["files_written_at"] is None


def test_force_rollback_without_post_apply_file_includes_referencing_card_date_and_clears_stamp(tmp_path: Path) -> None:
    table, _old, _colliding, fresh, run_dir = _applied_run(tmp_path)
    (run_dir / rx.POST_APPLY_FILE).unlink()
    later = _row(
        "later_no_post",
        pv=NEW_PV,
        date_et="2026-05-14",
        created_at="2026-10-24 09:00:00+00",
        files_written_at="2026-10-24 09:05:00+00",
        supersedes=fresh["card_id"],
    )
    table.rows[later["card_id"]] = later

    with pytest.raises(rx.RunStateError, match="no post-apply state"):
        asyncio.run(rx.rollback_run(run_dir, make_store(table), dry_run=False))

    report = asyncio.run(rx.rollback_run(run_dir, make_store(table), dry_run=False, force=True))
    assert report["committed"] is True
    assert report["supersedes_cleared_by_delete"] == [{"card_id": later["card_id"], "was": fresh["card_id"]}]
    manifest = rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE)
    assert manifest["rollback_dates"] == ["2026-05-07", "2026-05-09", "2026-05-14"]
    assert table.rows[later["card_id"]]["supersedes"] is None
    assert table.rows[later["card_id"]]["files_written_at"] is None


def test_force_rollback_with_empty_snapshot_clears_later_referencing_card_stamp(tmp_path: Path) -> None:
    table, _old, _colliding, fresh, run_dir = _applied_run(tmp_path)
    for cid in list(table.rows):
        if cid != fresh["card_id"]:
            table.rows.pop(cid)
    rx.write_json_atomic(run_dir / rx.SNAPSHOT_FILE, {**rx.read_json(run_dir / rx.SNAPSHOT_FILE), "cards": []})
    rx.write_jsonl_atomic(run_dir / rx.REPLACED_ROWS_FILE, [])
    manifest = rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE)
    manifest.update({"new_card_ids": [fresh["card_id"]], "replaced_card_ids": [], "retracted_card_ids": [], "affected_dates": ["2026-05-09"]})
    rx.write_json_atomic(run_dir / rx.APPLY_MANIFEST_FILE, manifest)

    later = _row(
        "later_empty_snap",
        pv=NEW_PV,
        date_et="2026-05-15",
        created_at="2026-10-24 09:00:00+00",
        files_written_at="2026-10-24 09:05:00+00",
        supersedes=fresh["card_id"],
    )
    table.rows[later["card_id"]] = later

    report = asyncio.run(rx.rollback_run(run_dir, make_store(table), dry_run=False, force=True))
    assert report["committed"] is True
    assert report["snapshot_rows_restored"] == 0
    assert rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE)["rollback_dates"] == ["2026-05-07", "2026-05-09", "2026-05-15"]
    assert table.rows[later["card_id"]]["files_written_at"] is None

    # Direct SQL check on CardStore.restore_statuses when rows is empty.
    cur = create_autospec(AsyncCursor, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = False
    cur.rowcount = 1
    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.return_value = cur
    assert asyncio.run(rx.CardStore(conn).restore_statuses([], clear_dates=["2026-05-15"])) == 0
    assert len(cur.execute.call_args_list) == 1
    assert "SET files_written_at = NULL" in cur.execute.call_args_list[0].args[0]
    assert cur.execute.call_args_list[0].args[1] == (["2026-05-15"],)


def test_insert_full_rows_links_supersedes_in_a_second_pass() -> None:
    cur = create_autospec(AsyncCursor, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = False
    cur.rowcount = 1
    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.return_value = cur
    a = _row("a")
    b = _row("b", supersedes=a["card_id"])
    n = asyncio.run(rx.CardStore(conn).insert_full_rows([b, a]))  # b points at a, inserted later
    assert n == 2
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert [s.split()[0] for s in sqls] == ["INSERT", "INSERT", "UPDATE"]
    sup_idx = [name for name, _ in rx.FULL_ROW_COLUMNS].index("supersedes")
    assert all(c.args[1][sup_idx] is None for c in cur.execute.call_args_list[:2])
    assert cur.execute.call_args_list[2].args[1] == (a["card_id"], b["card_id"])


# ------------------------------------------------------------------------------------------
# render
# ------------------------------------------------------------------------------------------


def _render_doubles(monkeypatch: pytest.MonkeyPatch, cards_by_date: dict[str, list[dict[str, Any]]]):
    async def _list(dates, *, active_only=True, conn=None):
        return [copy.deepcopy(c) for d in dates for c in cards_by_date.get(d, [])]

    list_cards = create_autospec(_LIST_CARDS, side_effect=_list)
    mark = create_autospec(_MARK_RENDERED, return_value=0)
    owner = create_autospec(_OWNER, return_value="owner")
    monkeypatch.setattr(rx.db_conv, "list_cards_for_dates", list_cards)
    monkeypatch.setattr(rx.db_conv, "mark_dates_rendered", mark)
    monkeypatch.setattr(rx, "archive_owner_user_id", owner)
    ws_map = create_autospec(_RESOLVE_WS, return_value=({"NVDA": "ws_nvda"}, "ws_home"))
    monkeypatch.setattr(render_mod, "_resolve_workspace_map", ws_map)

    async def _render(cards, *, target_dates=None, user_id=None, conn=None, **_kw):
        locs = {str(c["card_id"]): ([{"path": f"p/{c['date_et']}", "target": "home"}] if c.get("status") == "active" else []) for c in cards}
        removed = [loc for c in cards if c.get("status") != "active" for loc in c.get("rendered_locations") or []]
        return {"rendered_dates": sorted(target_dates), "card_locations": locs, "removed_files": removed}

    render_fn = create_autospec(render_mod.render_and_write_conversation_digests, side_effect=_render)
    embed_fn = create_autospec(rx.embed_texts_via_bridge)

    async def _embed(texts, *, task_type="RETRIEVAL_DOCUMENT", **_kw):
        return [[0.5] for _ in texts], "emb-model"

    embed_fn.side_effect = _embed
    return list_cards, mark, render_fn, embed_fn


def test_render_cleans_touched_dates_with_no_cards_and_resumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    table, _old, _colliding, fresh, run_dir = _applied_run(tmp_path)
    orphan_loc = {"path": "conversations/2026-05-01.md", "target": "company", "ticker": "AMD", "workspace_id": "ws_amd"}
    touched = rx.TouchedLocations(run_dir / rx.TOUCHED_LOCATIONS_FILE)
    touched.add("2026-05-01", [orphan_loc])
    touched.save()
    cards_by_date = {"2026-05-07": [table.rows[cid] for cid in table.rows if table.rows[cid]["date_et"] == "2026-05-07"],
                     "2026-05-09": [table.rows[fresh["card_id"]]]}
    _list, mark, render_fn, embed_fn = _render_doubles(monkeypatch, cards_by_date)

    dry = asyncio.run(rx.render_run(run_dir, make_store(table), dry_run=True, render_fn=render_fn, embed_fn=embed_fn))
    assert (dry["dates_to_render"], dry["cards_missing_embedding"]) == (3, 2)
    render_fn.assert_not_called()

    report = asyncio.run(rx.render_run(run_dir, make_store(table), dry_run=False, dates_per_batch=1, render_fn=render_fn, embed_fn=embed_fn))
    assert report["complete"] is True and report["embedded"] == 2
    first_call_cards = render_fn.call_args_list[0].args[0]
    assert render_fn.call_args_list[0].kwargs["target_dates"] == ["2026-05-01"]
    assert [c["card_id"] for c in first_call_cards] == [f"{rx.GHOST_CARD_PREFIX}2026-05-01"]
    assert first_call_cards[0]["rendered_locations"] == [orphan_loc]
    assert report["removed_stale_files"] >= 1
    for call in mark.call_args_list:
        assert not any(k.startswith(rx.GHOST_CARD_PREFIX) for k in call.kwargs["card_locations"])
    assert rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE)["render_done"]["applied"]

    render_fn.reset_mock()
    again = asyncio.run(rx.render_run(run_dir, make_store(table), dry_run=False, render_fn=render_fn, embed_fn=embed_fn))
    assert again["dates_to_render"] == 0
    render_fn.assert_not_called()


def test_render_stops_when_the_advisory_lock_is_lost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    table, _old, _colliding, fresh, run_dir = _applied_run(tmp_path)
    for cid in table.rows:
        table.rows[cid]["embedding"] = table.rows[cid]["embedding"] or "[0.1]"
    _list, _mark, render_fn, embed_fn = _render_doubles(monkeypatch, {})
    # Checks: embedding loop, render batch 1, then the lock is gone before batch 2.
    store = make_store(table, lock_states=[True, True, False])
    with pytest.raises(rx.ArchiveBusyError, match="lost the archive advisory lock"):
        asyncio.run(rx.render_run(run_dir, store, dry_run=False, dates_per_batch=1, render_fn=render_fn, embed_fn=embed_fn))
    assert render_fn.call_count == 1
    progress = rx.read_json(run_dir / rx.RENDER_PROGRESS_FILE)
    assert progress == {"phase": "applied", "done_dates": ["2026-05-07"]}
    assert "render_done" not in rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE) or not rx.read_json(
        run_dir / rx.APPLY_MANIFEST_FILE
    )["render_done"]


# ------------------------------------------------------------------------------------------
# archive guard
# ------------------------------------------------------------------------------------------


def _lock_conn() -> AsyncConnection:
    conn = create_autospec(AsyncConnection, instance=True)
    tx = create_autospec(AsyncTransaction, instance=True)
    tx.__aenter__.return_value = tx
    tx.__aexit__.return_value = False
    conn.transaction.return_value = tx
    return conn


def test_guard_refuses_while_archive_pid_is_alive(tmp_path: Path) -> None:
    pid_file = tmp_path / "conversation-archive.pid"
    pid_file.write_text(f"{os.getppid()}\n", encoding="utf-8")
    open_conn = create_autospec(rx._open_autocommit_conn, return_value=_lock_conn())

    async def run() -> None:
        async with rx.archive_guard(open_conn=open_conn, pid_files=[pid_file]):
            raise AssertionError("must not enter")

    with pytest.raises(rx.ArchiveBusyError, match="pid"):
        asyncio.run(run())
    open_conn.assert_not_called()


def test_guard_holds_the_lock_on_the_connection_it_yields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _lock_conn()
    open_conn = create_autospec(rx._open_autocommit_conn, return_value=conn)
    acquire = create_autospec(db_conv.try_acquire_archive_run_lock, return_value=False)
    release = create_autospec(db_conv.release_archive_run_lock, return_value=True)
    monkeypatch.setattr(rx.db_conv, "try_acquire_archive_run_lock", acquire)
    monkeypatch.setattr(rx.db_conv, "release_archive_run_lock", release)
    yielded: list[Any] = []

    async def run() -> None:
        async with rx.archive_guard(open_conn=open_conn, pid_files=[tmp_path / "none.pid"]) as c:
            yielded.append(c)

    with pytest.raises(rx.ArchiveBusyError, match="advisory lock"):
        asyncio.run(run())
    assert yielded == []
    release.assert_not_called()
    conn.close.assert_awaited_once()

    acquire.return_value = True
    asyncio.run(run())
    assert yielded == [conn]  # all work runs on the lock-holding connection
    acquire.assert_awaited_with(conn=conn)
    release.assert_awaited_once_with(conn=conn)


def test_cli_apply_refusal_prints_pause_hint_and_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pid_file = tmp_path / "conversation-archive.pid"
    pid_file.write_text(f"{os.getppid()}\n", encoding="utf-8")
    monkeypatch.setenv("DALTON_ARCHIVE_PID_FILE", str(pid_file))
    monkeypatch.setenv("DALTON_ARCHIVE_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.delenv(rx.DSN_ENV, raising=False)
    apply_spy = create_autospec(rx.apply_run)
    monkeypatch.setattr(rx, "apply_run", apply_spy)
    monkeypatch.setattr(rx, "RUNS_ROOT", tmp_path)

    code = rx.main(["apply", "--run-id", "rx_x", "--yes"])
    assert code == rx.EXIT_CODE_LOCK_HELD
    err = capsys.readouterr().err
    assert "refused" in err and "conversation-archive/disable" in err
    apply_spy.assert_not_called()


# ------------------------------------------------------------------------------------------
# review round 2
# ------------------------------------------------------------------------------------------


def _chunk(eid: str, idx: int, count: int) -> dict[str, Any]:
    return {**_ev(eid), "chunk_idx": idx, "chunk_count": count}


def test_event_is_covered_only_when_every_chunk_segment_is_ok() -> None:
    segs = [
        {"key": "k0", "events": [_chunk("ev_long", 0, 2), _ev("ev_a")]},
        {"key": "k1", "events": [_chunk("ev_long", 1, 2)]},
    ]
    assert rx.covered_event_keys(segs, {"k0"}) == {("main", "s1", "ev_a")}
    assert ("main", "s1", "ev_long") in rx.covered_event_keys(segs, {"k0", "k1"})


def test_old_card_stays_active_when_one_chunk_of_its_event_failed(tmp_path: Path) -> None:
    table, old = _seed_table()
    run_dir = tmp_path / "rx_chunks"
    write_run(
        run_dir,
        [("k0", _ok([_staged_card("n0")])), ("k1", {"ok": False, "attempt": "fallback", "error_kind": "timeout", "cards": []})],
        events_by_segment=[[_chunk("ev_b", 0, 2), _ev("ev_c")], [_chunk("ev_b", 1, 2)]],
    )
    report = asyncio.run(rx.apply_run(run_dir, make_store(table), allow_failures=1, dry_run=False))
    assert report["committed"] is True
    assert table.rows[old["b"]["card_id"]]["status"] == "superseded"  # untouched: chunk 1 failed
    assert table.rows[old["c"]["card_id"]]["status"] == "retracted"
    assert old["b"]["card_id"] not in rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE)["retracted_card_ids"]


def _uncovered_collision_run(tmp_path: Path, name: str) -> tuple[MemoryTable, dict[str, Any], dict[str, Any], Path]:
    table, old = _seed_table()
    a = table.rows[old["a"]["card_id"]]
    a["event_ids"] = ["ev_shared", "ev_not_reextracted"]  # only partly covered
    run_dir = tmp_path / name
    colliding = _staged_card("x", quote="shared quote", quote_event_id="ev_shared")
    write_run(run_dir, [("k0", _ok([colliding, _staged_card("fresh")]))])
    return table, old, colliding, run_dir


def test_uncovered_live_collision_refuses_by_default(tmp_path: Path) -> None:
    table, old, _colliding, run_dir = _uncovered_collision_run(tmp_path, "rx_coll_refuse")
    before = copy.deepcopy(table.rows)
    with pytest.raises(rx.RunStateError, match="not fully covered") as exc:
        asyncio.run(rx.apply_run(run_dir, make_store(table), dry_run=False))
    assert old["a"]["card_id"] in str(exc.value)
    assert table.rows == before
    assert table.commits == 0


def test_uncovered_live_collision_keep_old_skips_the_new_card(tmp_path: Path) -> None:
    table, old, colliding, run_dir = _uncovered_collision_run(tmp_path, "rx_coll_keep")
    report = asyncio.run(rx.apply_run(run_dir, make_store(table), on_uncovered_collision="keep-old", dry_run=False))
    assert report["committed"] is True
    assert report["new_cards_skipped_keeping_old"] == 1
    assert report["uncovered_collisions"][0]["card_id"] == old["a"]["card_id"]
    assert (report["inserted_new"], report["inserted_replacing_collision"]) == (1, 0)
    kept = table.rows[old["a"]["card_id"]]
    assert (kept["prompt_version"], kept["status"], kept["claim"]) == (OLD_PV, "active", old["a"]["claim"])
    manifest = rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE)
    assert manifest["skipped_new_card_ids"] == [colliding["card_id"]]
    assert colliding["card_id"] not in manifest["new_card_ids"]
    with pytest.raises(rx.RunStateError, match="unknown on_uncovered_collision"):
        asyncio.run(rx.apply_run(run_dir, make_store(table), on_uncovered_collision="replace", dry_run=True))


def test_cards_superseded_by_the_insert_path_are_reported_and_re_rendered(tmp_path: Path) -> None:
    table, old = _seed_table()
    other = _row("other", pv=NEW_PV, date_et="2026-05-03", tickers=["NVDA"])
    table.rows[other["card_id"]] = other
    run_dir = tmp_path / "rx_indirect"
    fresh = _staged_card("fresh", tickers=["NVDA"])
    write_run(run_dir, [("k0", _ok([fresh]))])
    store = make_store(table)
    real_insert = store.insert_segment_cards.side_effect

    async def insert_and_supersede(cards: list[dict[str, Any]]):
        n, _sup, rows = await real_insert(cards)
        table.rows[other["card_id"]]["status"] = "superseded"  # (NVDA, metric, period) is newer
        return n, 1, rows

    store.insert_segment_cards.side_effect = insert_and_supersede
    report = asyncio.run(rx.apply_run(run_dir, store, dry_run=False))
    ind = report["indirectly_changed_cards"]
    assert (ind["count"], ind["status_changed"], ind["by_date"]) == (1, 1, {"2026-05-03": 1})
    assert ind["cards"][0]["before"]["status"] == "active" and ind["cards"][0]["after"]["status"] == "superseded"
    manifest = rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE)
    assert "2026-05-03" in manifest["affected_dates"]
    assert manifest["indirectly_changed_card_ids"] == [other["card_id"]]
    assert "2026-05-03" in rx.TouchedLocations(run_dir / rx.TOUCHED_LOCATIONS_FILE).by_date


def test_partial_render_then_rollback_still_cleans_every_planned_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    table, old = _seed_table()
    run_dir = tmp_path / "rx_partial_render"
    fresh = _staged_card("fresh", date_et="2026-05-09", tickers=["NVDA"])
    write_run(run_dir, [("k0", _ok([fresh]))])
    asyncio.run(rx.apply_run(run_dir, make_store(table), dry_run=False))
    cards_by_date = {"2026-05-09": [table.rows[fresh["card_id"]]]}
    _list, _mark, render_fn, embed_fn = _render_doubles(monkeypatch, cards_by_date)

    async def dies_mid_write(cards, *, target_dates=None, **_kw):
        if "2026-05-09" in target_dates:
            raise RuntimeError("backend write failed after writing the company file")
        return {"rendered_dates": sorted(target_dates), "card_locations": {}, "removed_files": []}

    render_fn.side_effect = dies_mid_write
    with pytest.raises(RuntimeError, match="backend write failed"):
        asyncio.run(rx.render_run(run_dir, make_store(table), dry_run=False, dates_per_batch=1, render_fn=render_fn, embed_fn=embed_fn))
    nvda_loc = {"target": "company", "ticker": "NVDA", "workspace_id": "ws_nvda", "path": "conversations/2026-05-09.md"}
    assert nvda_loc in rx.TouchedLocations(run_dir / rx.TOUCHED_LOCATIONS_FILE).by_date["2026-05-09"]
    assert table.rows[fresh["card_id"]]["rendered_locations"] == []  # never marked

    asyncio.run(rx.rollback_run(run_dir, make_store(table), dry_run=False))
    assert fresh["card_id"] not in table.rows
    _list, _mark, render_fn, embed_fn = _render_doubles(monkeypatch, {})
    report = asyncio.run(rx.render_run(run_dir, make_store(table), dry_run=False, dates_per_batch=1, render_fn=render_fn, embed_fn=embed_fn))
    assert report["complete"] is True and report["phase"] == "rolled_back"
    ghost_09 = [
        c
        for call in render_fn.call_args_list
        for c in call.args[0]
        if c["card_id"] == f"{rx.GHOST_CARD_PREFIX}2026-05-09"
    ]
    assert ghost_09 and nvda_loc in ghost_09[0]["rendered_locations"]
    assert rx.read_json(run_dir / rx.APPLY_MANIFEST_FILE)["rollback_render_pending"] is False


def test_planned_locations_follow_the_renderer_routing() -> None:
    cards = [
        {"card_id": "1", "status": "active", "date_et": "2026-05-07", "tickers": ["NVDA"]},
        {"card_id": "2", "status": "active", "date_et": "2026-05-07", "tickers": ["ZZZZ"]},
        {"card_id": "3", "status": "retracted", "date_et": "2026-05-08", "tickers": ["NVDA"]},
    ]
    out = rx.planned_render_locations(cards, ticker_to_workspace_id={"NVDA": "ws_nvda"}, home_workspace_id="ws_home")
    assert list(out) == ["2026-05-07"]
    assert {"target": "company", "ticker": "NVDA", "workspace_id": "ws_nvda", "path": "conversations/2026-05-07.md"} in out["2026-05-07"]
    assert {"target": "home", "ticker": None, "workspace_id": "ws_home", "path": "research/topics/conversations/2026-05-07.md"} in out["2026-05-07"]
