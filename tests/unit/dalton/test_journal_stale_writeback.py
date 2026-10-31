"""R6: journal writeback may not clobber a newer attempt's row.

A stale worker (its pending claim was declared stale, another worker re-claimed
with `attempt + 1`) that returns after the newer attempt wrote the row must find
no matching row: `complete_journal_done` / `complete_journal_failed` match on
`invocation_id AND attempt AND state IN ('pending', 'indeterminate')`, log a warning, and return
None — never overwriting the newer attempt's state, response, or costs.

These tests run the REAL writeback functions against an in-memory single-row
store whose cursor applies the UPDATE's WHERE clause the way Postgres would;
they also pin the executed SQL text and parameter tuples. The SQL semantics
against a real Postgres still need a live check (lead, temp database).
"""

from __future__ import annotations

from decimal import Decimal

from datetime import datetime, timezone
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.database import llm_journal as journal_mod


class _FakeRow:
    """One `dalton_llm_journal` row plus every (sql, params) the cursor ran."""

    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.executed: list[tuple[str, tuple[Any, ...]]] = []


class _FakeCursor:
    def __init__(self, store: _FakeRow) -> None:
        self._store = store
        self._result: dict[str, Any] | None = None

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, sql: str, params: tuple = ()) -> None:
        store = self._store
        self._result = None
        store.executed.append((sql, tuple(params)))
        row = store.row
        text = " ".join(sql.split()).upper()
        if "SET STATE = 'DONE'" in text or ("SET STATE = %S" in text and "UPDATE" in text):
            # Both writebacks end their WHERE with (..., invocation_id, attempt);
            # evaluate it exactly as Postgres would before applying the SET.
            assert "AND ATTEMPT = %S AND STATE IN ('PENDING', 'INDETERMINATE')" in text, text
            matched = (
                row is not None
                and row.get("invocation_id") == params[-2]
                and int(row.get("attempt") or 1) == int(params[-1])
                and str(row.get("state") or "") in ("pending", "indeterminate")
            )
            if matched:
                if "SET STATE = 'DONE'" in text:
                    row.update(
                        state="done",
                        completed_at=datetime.now(timezone.utc),
                        response=params[0],
                        usage=params[1],
                        cash_usd=params[2],
                        notional_usd=params[3],
                    )
                else:
                    # complete_journal_failed params:
                    # (state, error_kind, error_text, cash, notional, inv, attempt)
                    row.update(
                        state=params[0],
                        completed_at=datetime.now(timezone.utc),
                        error_kind=params[1],
                        error_text=params[2],
                    )
                self._result = dict(row)

    async def fetchone(self) -> dict[str, Any] | None:
        return self._result


class _FakeConn:
    def __init__(self, store: _FakeRow) -> None:
        self._store = store

    def cursor(self, **kwargs: Any) -> _FakeCursor:
        return _FakeCursor(self._store)


def _pending_row(invocation_id: str, attempt: int) -> dict[str, Any]:
    return {
        "invocation_id": invocation_id,
        "user_id": "u1",
        "scope_ref": "turn:1",
        "purpose": "unit.test",
        "model_key": "m1",
        "provider": "p1",
        "billing_type": "oauth",
        "request_hash": "hash-1",
        "state": "pending",
        "attempt": attempt,
        "claimed_at": datetime.now(timezone.utc),
    }


# ------------------- stale writeback is refused, newer attempt untouched


@pytest.mark.asyncio
async def test_stale_failed_after_newer_attempt_done_is_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A (attempt=1) declared stale; B re-claims (attempt=2) and writes done;
    A's late `failed` must not overwrite B's done row."""
    row = _pending_row("inv-stale-1", 2)
    row["state"] = "done"
    row["response"] = {"kind": "json", "data": {"answer": 42}, "text": "{}"}
    store = _FakeRow(row)
    conn = _FakeConn(store)

    with caplog.at_level("WARNING", logger=journal_mod.logger.name):
        res = await journal_mod.complete_journal_failed(
            "inv-stale-1",
            attempt=1,
            error_kind="timeout",
            error_text="late failure from the stale worker",
            state="failed",
            conn=conn,
        )

    assert res is None
    assert row["state"] == "done"
    assert row["response"] == {"kind": "json", "data": {"answer": 42}, "text": "{}"}
    warnings = [r for r in caplog.records if "complete_journal_failed refused" in r.message]
    assert len(warnings) == 1
    assert "inv-stale-1" in warnings[0].message and "attempt=1" in warnings[0].message
    sql, params = store.executed[-1]
    assert "AND attempt = %s" in sql and "state IN ('pending', 'indeterminate')" in sql
    assert params[-2] == "inv-stale-1" and params[-1] == 1


@pytest.mark.asyncio
async def test_stale_done_while_newer_attempt_in_flight_is_refused() -> None:
    """B re-claimed (attempt=2, still pending, mid-send); A's late `done` must
    not swap the row's state or response under B's running attempt."""
    row = _pending_row("inv-stale-2", 2)
    store = _FakeRow(row)
    conn = _FakeConn(store)

    res = await journal_mod.complete_journal_done(
        "inv-stale-2",
        attempt=1,
        response={"kind": "json", "data": {"stale": True}, "text": "{}"},
        usage={"total_tokens": 1},
        conn=conn,
    )

    assert res is None
    assert row["state"] == "pending" and row["attempt"] == 2
    assert "response" not in row  # A's payload never landed
    sql, _params = store.executed[-1]
    assert "AND attempt = %s" in sql and "state IN ('pending', 'indeterminate')" in sql


@pytest.mark.asyncio
async def test_a_long_call_past_the_stale_window_still_settles_its_own_row() -> None:
    """A's row was stale-marked `indeterminate` (attempt unchanged) while A
    was still legitimately running (a review timeout past the 900 s stale
    window), and nobody re-claimed it. A's late `done` settles its own row:
    otherwise its result could never be replayed and a cash model's cost
    would be lost from the ledger the budget sums."""
    row = _pending_row("inv-stale-3", 1)
    row["state"] = "indeterminate"
    row["error_kind"] = "crash"
    store = _FakeRow(row)
    conn = _FakeConn(store)

    res = await journal_mod.complete_journal_done(
        "inv-stale-3",
        attempt=1,
        response={"kind": "json", "data": {"late": True}, "text": "{}"},
        usage={},
        cash_usd=Decimal("0.42"),
        conn=conn,
    )

    assert res is not None
    assert row["state"] == "done" and row["cash_usd"] == Decimal("0.42")


@pytest.mark.asyncio
async def test_a_stale_marked_row_taken_over_refuses_the_old_attempt() -> None:
    """Once another worker re-claimed the stale row (attempt bumped), the
    old attempt's late writeback is shut out even though the row is not done."""
    row = _pending_row("inv-stale-4", 2)
    row["state"] = "indeterminate"
    store = _FakeRow(row)
    conn = _FakeConn(store)

    res = await journal_mod.complete_journal_done(
        "inv-stale-4",
        attempt=1,
        response={"kind": "json", "data": {"late": True}, "text": "{}"},
        usage={},
        conn=conn,
    )

    assert res is None
    assert row["state"] == "indeterminate" and row["attempt"] == 2


# ------------------- the owning attempt's writeback still applies


@pytest.mark.asyncio
async def test_own_attempt_done_writeback_applies() -> None:
    row = _pending_row("inv-ok-1", 1)
    store = _FakeRow(row)
    conn = _FakeConn(store)

    res = await journal_mod.complete_journal_done(
        "inv-ok-1",
        attempt=1,
        response={"kind": "json", "data": {"answer": 7}, "text": "{}"},
        usage={"total_tokens": 9},
        conn=conn,
    )

    assert res is not None and res["state"] == "done"
    assert row["state"] == "done" and row["attempt"] == 1
    assert row["response"] == '{"kind": "json", "data": {"answer": 7}, "text": "{}"}'


@pytest.mark.asyncio
async def test_own_attempt_failed_writeback_applies_including_indeterminate() -> None:
    """`failed` and `indeterminate` are both pending→X transitions written by
    the attempt that owns the pending claim, so both must match the guard."""
    row = _pending_row("inv-ok-2", 3)
    store = _FakeRow(row)
    conn = _FakeConn(store)

    res = await journal_mod.complete_journal_failed(
        "inv-ok-2",
        attempt=3,
        error_kind="post_call_error",
        error_text="boom after the provider answered",
        state="indeterminate",
        conn=conn,
    )

    assert res is not None and res["state"] == "indeterminate"
    assert row["state"] == "indeterminate" and row["error_kind"] == "post_call_error"


# ------------------- dalton_complete threads the claimed attempt through


def _patch_complete_for_attempt(
    monkeypatch: pytest.MonkeyPatch,
    *,
    claim_answer: tuple[str, dict[str, Any]],
    done: Any,
    failed: Any,
) -> None:
    import src.dalton.llm.complete as complete_mod

    monkeypatch.setattr(
        complete_mod,
        "inspect_journal_state",
        create_autospec(complete_mod.inspect_journal_state, return_value=("proceed", None)),
    )
    monkeypatch.setattr(
        complete_mod,
        "_check_cash_budget_admission",
        create_autospec(
            complete_mod._check_cash_budget_admission, return_value=(True, None, None)
        ),
    )
    monkeypatch.setattr(
        complete_mod,
        "claim_journal_invocation",
        create_autospec(complete_mod.claim_journal_invocation, return_value=claim_answer),
    )
    monkeypatch.setattr(complete_mod, "complete_journal_done", done)
    monkeypatch.setattr(complete_mod, "complete_journal_failed", failed)
    monkeypatch.setattr(
        complete_mod,
        "record_provider_event",
        create_autospec(complete_mod.record_provider_event, return_value=None),
    )


@pytest.mark.asyncio
async def test_dalton_complete_passes_claimed_attempt_to_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.services.llm.service import LLMService

    import src.dalton.llm.complete as complete_mod

    service = create_autospec(LLMService, instance=True)
    service.complete.return_value = ("ok", {"total_tokens": 5})
    done = create_autospec(
        complete_mod.complete_journal_done, return_value={"state": "done"}
    )
    failed = create_autospec(complete_mod.complete_journal_failed, return_value=None)
    _patch_complete_for_attempt(
        monkeypatch, claim_answer=("claimed", {"attempt": 3}), done=done, failed=failed
    )

    from src.dalton.llm.complete import dalton_complete

    res = await dalton_complete(
        service, user_id="u1", user_prompt="p", request_model="gemini-3-flash-oauth"
    )
    assert res.ok
    done.assert_awaited_once()
    assert done.call_args.kwargs["attempt"] == 3
    failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_dalton_complete_passes_claimed_attempt_to_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.services.llm.service import LLMService

    import src.dalton.llm.complete as complete_mod

    service = create_autospec(LLMService, instance=True)
    service.complete.side_effect = TimeoutError("upstream timeout")
    done = create_autospec(complete_mod.complete_journal_done, return_value=None)
    failed = create_autospec(
        complete_mod.complete_journal_failed, return_value={"state": "failed"}
    )
    _patch_complete_for_attempt(
        monkeypatch, claim_answer=("claimed", {"attempt": 2}), done=done, failed=failed
    )

    from src.dalton.llm.complete import dalton_complete

    res = await dalton_complete(
        service, user_id="u1", user_prompt="p", request_model="gemini-3-flash-oauth"
    )
    assert not res.ok
    failed.assert_awaited_once()
    assert failed.call_args.kwargs["attempt"] == 2
    done.assert_not_awaited()
