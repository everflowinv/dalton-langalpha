"""Tests for `dalton_complete` billing on structured-output parse failures, retries, timeouts, and OAuth."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

from langchain_core.messages import AIMessage
from pydantic import BaseModel
import pytest

from ptc_agent.config.agent import CredentialSource
from src.dalton.budget import service as budget_svc
from src.dalton.database import budget as budget_db
from src.dalton.database import llm_journal as journal_db
from src.dalton.llm import complete as complete_mod
from src.dalton.llm.complete import compute_call_costs, dalton_complete
from src.llms import api_call as api_call_mod
from src.server.services.llm import service as llm_svc_mod

_REAL_RESOLVE_LLM_CONFIG = llm_svc_mod.resolve_llm_config
_REAL_MAKE_API_CALL = api_call_mod.make_api_call
_REAL_ASYNCIO_SLEEP = asyncio.sleep


class _VerdictSchema(BaseModel):
    verdict: str
    reason: str


def _ai_msg(text: str, *, input_tokens: int, output_tokens: int) -> AIMessage:
    return AIMessage(
        content=text,
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    )


class _SequencedChatModel:
    """Minimal chat-model stand-in supporting `with_structured_output` and `ainvoke`."""

    def __init__(
        self,
        responses: list[Any],
        *,
        structured_responses: list[Any] | None = None,
    ) -> None:
        self._responses = list(responses)
        self._structured_responses = list(structured_responses) if structured_responses is not None else None
        self.ainvoke_calls = 0
        self.structured_calls = 0

    def with_structured_output(self, *_args: Any, **_kwargs: Any) -> Any:
        if self._structured_responses is None:
            raise NotImplementedError("native structured output disabled")
        parent = self

        class _Wrapper:
            async def ainvoke(self, *_a: Any, **_kw: Any) -> Any:
                idx = parent.structured_calls
                parent.structured_calls += 1
                item = parent._structured_responses[idx]
                if isinstance(item, BaseException):
                    raise item
                return item

        return _Wrapper()

    async def ainvoke(self, *_args: Any, **_kwargs: Any) -> Any:
        idx = self.ainvoke_calls
        self.ainvoke_calls += 1
        item = self._responses[idx]
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return await item()
        return item


class _InMemoryJournalStore:
    """In-memory `dalton_llm_journal` table emulating Postgres UPDATE/SUM semantics."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def conn(self) -> "_FakeConn":
        return _FakeConn(self)


class _FakeCursor:
    def __init__(self, store: _InMemoryJournalStore) -> None:
        self._store = store
        self._one: dict[str, Any] | None = None
        self._many: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        text = " ".join(sql.split()).upper()
        self._one = None
        self._many = []
        rows = self._store.rows

        if text.startswith("SELECT * FROM DALTON_LLM_JOURNAL WHERE INVOCATION_ID"):
            row = rows.get(str(params[0]))
            self._one = dict(row) if row is not None else None
            return

        if text.startswith("INSERT INTO DALTON_LLM_JOURNAL"):
            inv_id = str(params[0])
            if inv_id not in rows:
                has_usage = len(params) == 9
                rows[inv_id] = {
                    "invocation_id": inv_id,
                    "user_id": params[1],
                    "scope_ref": params[2],
                    "purpose": params[3],
                    "model_key": params[4],
                    "provider": params[5],
                    "billing_type": params[6],
                    "request_hash": params[7],
                    "usage": json.loads(params[8]) if has_usage else None,
                    "response": None,
                    "state": "pending",
                    "attempt": 1,
                    "cash_usd": Decimal("0"),
                    "notional_usd": Decimal("0"),
                    "error_kind": None,
                    "error_text": None,
                    "claimed_at": datetime.now(timezone.utc),
                    "completed_at": None,
                    "created_at": datetime.now(timezone.utc),
                }
                self._one = dict(rows[inv_id])
            return

        if "ATTEMPT = ATTEMPT + 1" in text:
            inv_id = str(params[-1])
            row = rows.get(inv_id)
            if row is not None:
                row.update(
                    state="pending",
                    attempt=int(row.get("attempt") or 1) + 1,
                    claimed_at=datetime.now(timezone.utc),
                    completed_at=None,
                    error_kind=None,
                    error_text=None,
                    model_key=params[0],
                    provider=params[1] or row.get("provider"),
                    billing_type=params[2] or row.get("billing_type"),
                )
                if "USAGE = NULL" in text:
                    row["usage"] = None
                self._one = dict(row)
            return

        if "SET STATE = 'DONE'" in text:
            inv_id = str(params[-2])
            attempt = int(params[-1])
            row = rows.get(inv_id)
            if (
                row is not None
                and int(row.get("attempt") or 1) == attempt
                and row.get("state") in ("pending", "indeterminate")
            ):
                resp_val = json.loads(params[0]) if params[0] is not None else None
                usage_val = json.loads(params[1]) if params[1] is not None else {}
                if "COALESCE(USAGE" in text and isinstance(row.get("usage"), dict):
                    merged_u = dict(row["usage"])
                    merged_u.update(usage_val)
                    usage_val = merged_u
                row.update(
                    state="done",
                    completed_at=datetime.now(timezone.utc),
                    response=resp_val,
                    usage=usage_val,
                    cash_usd=Decimal(str(row.get("cash_usd") or "0")) + Decimal(str(params[2] or "0")),
                    notional_usd=Decimal(str(row.get("notional_usd") or "0")) + Decimal(str(params[3] or "0")),
                    provider=params[4] or row.get("provider"),
                    billing_type=params[5] or row.get("billing_type"),
                    error_kind=None,
                    error_text=None,
                )
                self._one = dict(row)
            return

        if text.startswith("UPDATE DALTON_LLM_JOURNAL") and "SET STATE = %S" in text:
            inv_id = str(params[-2])
            attempt = int(params[-1])
            row = rows.get(inv_id)
            if (
                row is not None
                and int(row.get("attempt") or 1) == attempt
                and row.get("state") in ("pending", "indeterminate")
            ):
                add_cash = Decimal(str(params[3])) if params[3] is not None else Decimal("0")
                add_notional = Decimal(str(params[4])) if params[4] is not None else Decimal("0")
                row.update(
                    state=params[0],
                    completed_at=datetime.now(timezone.utc),
                    error_kind=params[1],
                    error_text=params[2],
                    cash_usd=Decimal(str(row.get("cash_usd") or "0")) + add_cash,
                    notional_usd=Decimal(str(row.get("notional_usd") or "0")) + add_notional,
                )
                if len(params) == 8:
                    usage_val = json.loads(params[5])
                    if "COALESCE(USAGE" in text and isinstance(row.get("usage"), dict):
                        merged_u = dict(row["usage"])
                        merged_u.update(usage_val)
                        usage_val = merged_u
                    row["usage"] = usage_val
                self._one = dict(row)
            return

        if "SET USAGE = COALESCE(USAGE" in text and "REPLAY_COUNT" in text:
            inv_id = str(params[0])
            row = rows.get(inv_id)
            if row is not None and row.get("state") == "done":
                u = dict(row.get("usage") or {})
                u["replay_count"] = int(u.get("replay_count") or 0) + 1
                row["usage"] = u
            return

        if "SELECT COALESCE(SUM(CASH_USD), 0) AS JOURNAL_CASH_USD" in text:
            uid = params[2]
            total = Decimal("0")
            for r in rows.values():
                if uid is not None and r.get("user_id") not in (uid, None):
                    continue
                cash_val = Decimal(str(r.get("cash_usd") or "0"))
                if r.get("state") in ("done", "indeterminate") or cash_val > Decimal("0"):
                    total += cash_val
            self._one = {"journal_cash_usd": total}
            return

        if "FROM DALTON_LLM_JOURNAL WHERE CREATED_AT >=" in text:
            uid = params[2]
            matched = [
                dict(r)
                for r in rows.values()
                if uid is None or r.get("user_id") in (uid, None)
            ]
            self._many = matched
            return

        raise AssertionError(f"Unexpected SQL in test harness: {sql}")

    async def fetchone(self) -> dict[str, Any] | None:
        return self._one

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._many)


class _FakeConn:
    def __init__(self, store: _InMemoryJournalStore) -> None:
        self._store = store

    def cursor(self, **_kwargs: Any) -> _FakeCursor:
        return _FakeCursor(self._store)


def _wire_journal_and_budget(
    monkeypatch: pytest.MonkeyPatch,
    store: _InMemoryJournalStore,
) -> None:
    conn = store.conn()

    async def _inspect(invocation_id: str, **kwargs: Any) -> tuple[str, dict[str, Any] | None]:
        return await journal_db.inspect_journal_state(invocation_id, conn=conn, **kwargs)

    async def _claim(**kwargs: Any) -> tuple[str, dict[str, Any]]:
        return await journal_db.claim_journal_invocation(conn=conn, **kwargs)

    async def _done(invocation_id: str, **kwargs: Any) -> dict[str, Any] | None:
        return await journal_db.complete_journal_done(invocation_id, conn=conn, **kwargs)

    async def _failed(invocation_id: str, **kwargs: Any) -> dict[str, Any] | None:
        return await journal_db.complete_journal_failed(invocation_id, conn=conn, **kwargs)

    async def _replay(invocation_id: str, **kwargs: Any) -> None:
        await journal_db.record_journal_replay(invocation_id, conn=conn)

    monkeypatch.setattr(
        complete_mod,
        "inspect_journal_state",
        create_autospec(journal_db.inspect_journal_state, side_effect=_inspect),
    )
    monkeypatch.setattr(
        complete_mod,
        "claim_journal_invocation",
        create_autospec(journal_db.claim_journal_invocation, side_effect=_claim),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_done",
        create_autospec(journal_db.complete_journal_done, side_effect=_done),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_failed",
        create_autospec(journal_db.complete_journal_failed, side_effect=_failed),
    )
    monkeypatch.setattr(
        complete_mod,
        "record_journal_replay",
        create_autospec(journal_db.record_journal_replay, side_effect=_replay),
    )
    monkeypatch.setattr(
        complete_mod,
        "_check_cash_budget_admission",
        create_autospec(complete_mod._check_cash_budget_admission, return_value=(True, None, None)),
    )
    monkeypatch.setattr(
        complete_mod,
        "record_provider_event",
        create_autospec(complete_mod.record_provider_event, return_value=1),
    )
    monkeypatch.setattr(
        api_call_mod.asyncio,
        "sleep",
        create_autospec(_REAL_ASYNCIO_SLEEP, return_value=None),
    )

    # Wire budget summary helpers onto the same in-memory journal store
    monkeypatch.setattr(
        budget_svc,
        "resolve_user_budget_config",
        create_autospec(
            budget_svc.resolve_user_budget_config,
            return_value={
                "user_id": "u1",
                "daily_cap_usd": Decimal("50.00"),
                "default_task_cap_usd": None,
                "timezone": "Asia/Shanghai",
                "configured": True,
            },
        ),
    )
    monkeypatch.setattr(
        budget_svc,
        "acquire_budget_advisory_lock",
        create_autospec(budget_svc.acquire_budget_advisory_lock, return_value=None),
    )
    monkeypatch.setattr(
        budget_svc,
        "reconcile_reservations_sql",
        create_autospec(budget_svc.reconcile_reservations_sql, return_value=None),
    )
    monkeypatch.setattr(
        budget_svc,
        "fetch_day_reservations",
        create_autospec(budget_svc.fetch_day_reservations, return_value=[]),
    )
    monkeypatch.setattr(
        budget_svc,
        "fetch_unreserved_usage_today",
        create_autospec(budget_svc.fetch_unreserved_usage_today, return_value=Decimal("0")),
    )
    monkeypatch.setattr(
        budget_svc,
        "fetch_today_token_usages",
        create_autospec(budget_svc.fetch_today_token_usages, return_value=[]),
    )

    async def _fetch_cash(
        c: Any,
        day_start: datetime,
        day_end: datetime,
        user_id: str | None = None,
    ) -> Decimal:
        return await budget_db.fetch_journal_cash_today(c, day_start, day_end, user_id=user_id)

    async def _fetch_rows(
        day_start: datetime,
        day_end: datetime,
        *,
        user_id: str | None = None,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        return await journal_db.fetch_journal_rows_in_window(
            day_start, day_end, user_id=user_id, conn=conn
        )

    monkeypatch.setattr(
        budget_svc,
        "fetch_journal_cash_today",
        create_autospec(budget_db.fetch_journal_cash_today, side_effect=_fetch_cash),
    )
    monkeypatch.setattr(
        budget_svc,
        "fetch_journal_rows_in_window",
        create_autospec(journal_db.fetch_journal_rows_in_window, side_effect=_fetch_rows),
    )


def _make_real_llm_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chat_model: Any,
    model_key: str,
    credential_source: CredentialSource = CredentialSource.PLATFORM,
    max_parsing_retries: int | None = None,
) -> llm_svc_mod.LLMService:
    fake_resolved = SimpleNamespace(
        llm_client=chat_model,
        llm=SimpleNamespace(background=model_key, name=model_key),
        credential_source=credential_source,
    )
    monkeypatch.setattr(
        llm_svc_mod,
        "resolve_llm_config",
        create_autospec(_REAL_RESOLVE_LLM_CONFIG, return_value=fake_resolved),
    )
    if max_parsing_retries is not None:

        async def _bounded_make(*args: Any, **kwargs: Any) -> Any:
            kwargs["max_parsing_retries"] = max_parsing_retries
            return await _REAL_MAKE_API_CALL(*args, **kwargs)

        monkeypatch.setattr(
            llm_svc_mod,
            "make_api_call",
            create_autospec(_REAL_MAKE_API_CALL, side_effect=_bounded_make),
        )
    return llm_svc_mod.LLMService(agent_config=SimpleNamespace(llm=fake_resolved.llm))


@pytest.mark.asyncio
async def test_parse_failure_once_then_retry_success_bills_both_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paid model fails structured-output parsing once, retries and succeeds -> both calls are billed."""
    store = _InMemoryJournalStore()
    _wire_journal_and_budget(monkeypatch, store)

    u1 = {"input_tokens": 10_000, "output_tokens": 500}
    u2 = {"input_tokens": 12_000, "output_tokens": 800}
    chat_model = _SequencedChatModel(
        [
            _ai_msg("not json at all", **u1),
            _ai_msg('{"verdict": "pass", "reason": "verified"}', **u2),
        ]
    )
    svc = _make_real_llm_service(monkeypatch, chat_model=chat_model, model_key="grok-4.7")

    res = await dalton_complete(
        svc,
        user_id="u1",
        user_prompt="check report",
        scope_ref="review:retry-ok",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
    )

    c1, _ = compute_call_costs("grok-4.7", "platform", u1)
    c2, _ = compute_call_costs("grok-4.7", "platform", u2)
    expected_total = c1 + c2
    assert c1 > Decimal("0") and c2 > Decimal("0")

    assert res.ok is True
    assert res.status == "done"
    assert isinstance(res.data, _VerdictSchema)
    assert res.cash_usd == pytest.approx(float(expected_total))

    row = store.rows[res.invocation_id]
    assert row["state"] == "done"
    assert row["cash_usd"] == expected_total
    assert row["usage"]["input_tokens"] == 22_000
    assert row["usage"]["output_tokens"] == 1_300
    assert len(row["usage"]["per_call_usages"]) == 2

    summary = await budget_svc.reconcile_and_summarize_day(store.conn(), "u1")
    assert summary["journal_cash_usd"] == pytest.approx(float(expected_total))
    assert summary["spent_usd"] == pytest.approx(float(expected_total))
    assert summary["journal_unknown_usage_count"] == 0
    assert summary["journal_unknown_usage_cash_count"] == 0


@pytest.mark.asyncio
async def test_parse_failure_then_same_invocation_resend_accumulates_without_double_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attempt 1 exhausts parsing retries (billed), attempt 2 resends same invocation and succeeds ->
    journal accumulates attempt 1 + attempt 2, and a 3rd replay call adds $0."""
    store = _InMemoryJournalStore()
    _wire_journal_and_budget(monkeypatch, store)

    u1 = {"input_tokens": 10_000, "output_tokens": 500}
    u2 = {"input_tokens": 15_000, "output_tokens": 600}
    c1, _ = compute_call_costs("grok-4.7", "platform", u1)
    c2, _ = compute_call_costs("grok-4.7", "platform", u2)
    assert c1 > Decimal("0") and c2 > Decimal("0")

    # Attempt 1: 1 fallback attempt fails parsing -> exhausted
    chat_fail = _SequencedChatModel([_ai_msg("broken output", **u1)])
    svc_fail = _make_real_llm_service(
        monkeypatch, chat_model=chat_fail, model_key="grok-4.7", max_parsing_retries=1
    )
    res1 = await dalton_complete(
        svc_fail,
        user_id="u1",
        user_prompt="check report",
        scope_ref="review:same-inv",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
    )
    assert res1.ok is False
    assert res1.status == "indeterminate"
    assert res1.error_kind == "parse_error"
    assert res1.cash_usd == pytest.approx(float(c1))

    row_after_1 = store.rows[res1.invocation_id]
    assert row_after_1["state"] == "indeterminate"
    assert row_after_1["error_kind"] == "parse_error"
    assert row_after_1["cash_usd"] == c1
    assert row_after_1["usage"]["usage_known"] is True

    summary1 = await budget_svc.reconcile_and_summarize_day(store.conn(), "u1")
    assert summary1["journal_cash_usd"] == pytest.approx(float(c1))
    assert summary1["journal_unknown_usage_count"] == 0

    # Attempt 2: explicit resend on same invocation succeeds -> total becomes c1 + c2
    chat_ok = _SequencedChatModel([_ai_msg('{"verdict": "pass", "reason": "ok"}', **u2)])
    svc_ok = _make_real_llm_service(
        monkeypatch, chat_model=chat_ok, model_key="grok-4.7", max_parsing_retries=1
    )
    res2 = await dalton_complete(
        svc_ok,
        user_id="u1",
        user_prompt="check report",
        scope_ref="review:same-inv",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
        resend_indeterminate=True,
    )
    assert res2.ok is True
    assert res2.status == "done"
    assert res2.invocation_id == res1.invocation_id
    assert res2.attempt == 2
    assert res2.cash_usd == pytest.approx(float(c2))

    row_after_2 = store.rows[res2.invocation_id]
    assert row_after_2["state"] == "done"
    assert row_after_2["cash_usd"] == c1 + c2
    assert row_after_2["usage"]["input_tokens"] == 25_000
    assert row_after_2["usage"]["output_tokens"] == 1_100
    assert len(row_after_2["usage"]["per_call_usages"]) == 2

    # Call 3: cached replay must not bill again
    res3 = await dalton_complete(
        svc_ok,
        user_id="u1",
        user_prompt="check report",
        scope_ref="review:same-inv",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
    )
    assert res3.ok is True
    assert res3.replayed is True
    assert res3.cash_usd == 0.0
    assert store.rows[res3.invocation_id]["cash_usd"] == c1 + c2

    summary2 = await budget_svc.reconcile_and_summarize_day(store.conn(), "u1")
    assert summary2["journal_cash_usd"] == pytest.approx(float(c1 + c2))
    assert summary2["spent_usd"] == pytest.approx(float(c1 + c2))


@pytest.mark.asyncio
async def test_parse_failure_exhausted_bills_all_completed_calls_and_enters_daily_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native raw parse failure + exhausted fallback parse retries -> all 3 paid calls enter cash_usd and daily budget."""
    store = _InMemoryJournalStore()
    _wire_journal_and_budget(monkeypatch, store)

    u_native = {"input_tokens": 8_000, "output_tokens": 300}
    u_fb1 = {"input_tokens": 9_000, "output_tokens": 400}
    u_fb2 = {"input_tokens": 10_000, "output_tokens": 500}
    chat_model = _SequencedChatModel(
        [
            _ai_msg("bad json 1", **u_fb1),
            _ai_msg('{"verdict": 123}', **u_fb2),  # fails pydantic validation (missing reason, wrong type)
        ],
        structured_responses=[{"parsed": None, "raw": _ai_msg("native invalid", **u_native)}],
    )
    svc = _make_real_llm_service(
        monkeypatch, chat_model=chat_model, model_key="grok-4.7", max_parsing_retries=2
    )

    res = await dalton_complete(
        svc,
        user_id="u1",
        user_prompt="check report",
        scope_ref="review:exhausted",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
    )

    c_native, _ = compute_call_costs("grok-4.7", "platform", u_native)
    c_fb1, _ = compute_call_costs("grok-4.7", "platform", u_fb1)
    c_fb2, _ = compute_call_costs("grok-4.7", "platform", u_fb2)
    expected_total = c_native + c_fb1 + c_fb2
    assert expected_total > Decimal("0")

    assert res.ok is False
    assert res.status == "indeterminate"
    assert res.error_kind == "parse_error"
    assert res.cash_usd == pytest.approx(float(expected_total))

    row = store.rows[res.invocation_id]
    assert row["state"] == "indeterminate"
    assert row["error_kind"] == "parse_error"
    assert row["cash_usd"] == expected_total
    assert row["usage"]["usage_known"] is True
    assert row["usage"]["parsing_failed"] is True
    assert len(row["usage"]["per_call_usages"]) == 3

    summary = await budget_svc.reconcile_and_summarize_day(store.conn(), "u1")
    assert summary["journal_cash_usd"] == pytest.approx(float(expected_total))
    assert summary["spent_usd"] == pytest.approx(float(expected_total))
    assert summary["journal_unknown_usage_count"] == 0
    assert summary["by_provider"]["xai"]["cash_usd"] == pytest.approx(float(expected_total))


@pytest.mark.asyncio
async def test_parse_failure_then_retry_timeout_bills_completed_call_and_marks_unknown_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attempt 1 returns bad JSON with usage, attempt 2 times out via wait_for ->
    attempt 1 is billed, attempt 2 is NOT guessed, and summary flags the unknown final attempt."""
    store = _InMemoryJournalStore()
    _wire_journal_and_budget(monkeypatch, store)

    u1 = {"input_tokens": 10_000, "output_tokens": 500}
    c1, _ = compute_call_costs("grok-4.7", "platform", u1)

    never_set = asyncio.Event()

    async def _hang() -> Any:
        await never_set.wait()
        return _ai_msg("never", input_tokens=1, output_tokens=1)

    chat_model = _SequencedChatModel([_ai_msg("invalid json", **u1), _hang])
    svc = _make_real_llm_service(
        monkeypatch, chat_model=chat_model, model_key="grok-4.7", max_parsing_retries=2
    )

    res = await dalton_complete(
        svc,
        user_id="u1",
        user_prompt="check report",
        scope_ref="review:partial-timeout",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
        timeout_s=0.05,
    )

    assert res.ok is False
    assert res.status == "indeterminate"
    assert res.error_kind == "timeout"
    assert res.cash_usd == pytest.approx(float(c1))

    row = store.rows[res.invocation_id]
    assert row["state"] == "indeterminate"
    assert row["error_kind"] == "timeout"
    assert row["cash_usd"] == c1
    assert row["usage"]["usage_known"] is False
    assert len(row["usage"]["per_call_usages"]) == 1

    summary = await budget_svc.reconcile_and_summarize_day(store.conn(), "u1")
    assert summary["journal_cash_usd"] == pytest.approx(float(c1))
    assert summary["journal_unknown_usage_count"] == 1
    assert summary["journal_unknown_usage_cash_count"] == 1
    assert summary["by_provider"]["xai"]["unknown_usage_calls"] == 1


@pytest.mark.asyncio
async def test_timeout_and_stream_cut_without_usage_not_billed_but_distinguished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout and stream cut with no usage record $0 (no guessed cost) and are distinguished in summary,
    while pre-send failures stay `failed` and are not flagged as unknown-usage."""
    store = _InMemoryJournalStore()
    _wire_journal_and_budget(monkeypatch, store)

    # 1. Timeout with no usage
    svc_timeout = create_autospec(llm_svc_mod.LLMService, instance=True)
    svc_timeout.complete.side_effect = TimeoutError("upstream timed out")
    res_to = await dalton_complete(
        svc_timeout,
        user_id="u1",
        user_prompt="p1",
        scope_ref="review:timeout-no-usage",
        purpose="publish.review",
        request_model="grok-4.7",
    )
    assert res_to.ok is False
    assert res_to.status == "indeterminate"
    assert res_to.error_kind == "timeout"
    assert res_to.cash_usd == 0.0
    row_to = store.rows[res_to.invocation_id]
    assert row_to["state"] == "indeterminate"
    assert row_to["cash_usd"] == Decimal("0")
    assert row_to["usage"]["usage_known"] is False

    # 2. Stream cut with no usage
    svc_cut = create_autospec(llm_svc_mod.LLMService, instance=True)
    svc_cut.complete.side_effect = RuntimeError("connection reset by peer")
    res_cut = await dalton_complete(
        svc_cut,
        user_id="u1",
        user_prompt="p2",
        scope_ref="review:stream-cut-no-usage",
        purpose="publish.review",
        request_model="grok-4.7",
    )
    assert res_cut.ok is False
    assert res_cut.status == "indeterminate"
    assert res_cut.error_kind == "stream_cut"
    assert res_cut.cash_usd == 0.0
    row_cut = store.rows[res_cut.invocation_id]
    assert row_cut["state"] == "indeterminate"
    assert row_cut["cash_usd"] == Decimal("0")
    assert row_cut["usage"]["usage_known"] is False

    # 3. Pre-send failure (never billed, must NOT be counted as unknown-usage)
    svc_pre = create_autospec(llm_svc_mod.LLMService, instance=True)
    svc_pre.complete.side_effect = ValueError("MissingProviderKeyError: no xai key")
    res_pre = await dalton_complete(
        svc_pre,
        user_id="u1",
        user_prompt="p3",
        scope_ref="review:pre-send",
        purpose="publish.review",
        request_model="grok-4.7",
    )
    assert res_pre.ok is False
    assert res_pre.status == "failed"
    assert res_pre.error_kind == "pre_send"
    assert store.rows[res_pre.invocation_id]["state"] == "failed"
    assert store.rows[res_pre.invocation_id]["cash_usd"] == Decimal("0")

    summary = await budget_svc.reconcile_and_summarize_day(store.conn(), "u1")
    assert summary["journal_cash_usd"] == 0.0
    assert summary["spent_usd"] == 0.0
    assert summary["journal_unknown_usage_count"] == 2
    assert summary["journal_unknown_usage_cash_count"] == 2
    kinds = {item["error_kind"] for item in summary["journal_unknown_usage_calls"]}
    assert kinds == {"timeout", "stream_cut"}
    assert all(item["cash_usd"] == 0.0 for item in summary["journal_unknown_usage_calls"])
    assert summary["by_provider"]["xai"]["cash_usd"] == 0.0
    assert summary["by_provider"]["xai"]["unknown_usage_calls"] == 2


@pytest.mark.asyncio
async def test_oauth_parse_failure_and_timeout_keep_cash_usd_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OAuth subscription calls keep cash_usd == 0 on parse failure, retry, and timeout,
    while tracking notional_usd and excluding OAuth timeouts from cash unknown-usage count."""
    store = _InMemoryJournalStore()
    _wire_journal_and_budget(monkeypatch, store)

    u1 = {"input_tokens": 150_000, "output_tokens": 1_000}
    u2 = {"input_tokens": 150_000, "output_tokens": 1_000}
    _, n1 = compute_call_costs("muse-ember-1.0-oauth", "oauth", u1)
    _, n2 = compute_call_costs("muse-ember-1.0-oauth", "oauth", u2)
    assert n1 > Decimal("0") and n2 > Decimal("0")

    # 1. OAuth parse failure exhausted -> cash=0, notional=n1+n2
    chat_fail = _SequencedChatModel([_ai_msg("bad 1", **u1), _ai_msg("bad 2", **u2)])
    svc_fail = _make_real_llm_service(
        monkeypatch,
        chat_model=chat_fail,
        model_key="muse-ember-1.0-oauth",
        credential_source=CredentialSource.OAUTH,
        max_parsing_retries=2,
    )
    res_fail = await dalton_complete(
        svc_fail,
        user_id="u1",
        user_prompt="check oauth fail",
        scope_ref="review:oauth-fail",
        purpose="publish.review",
        request_model="muse-ember-1.0-oauth",
        response_schema=_VerdictSchema,
    )
    assert res_fail.ok is False
    assert res_fail.status == "indeterminate"
    assert res_fail.error_kind == "parse_error"
    assert res_fail.cash_usd == 0.0
    assert res_fail.notional_usd == pytest.approx(float(n1 + n2))
    row_fail = store.rows[res_fail.invocation_id]
    assert row_fail["cash_usd"] == Decimal("0")
    assert row_fail["notional_usd"] == n1 + n2
    assert row_fail["usage"]["usage_known"] is True

    # 2. OAuth parse failure once then retry success -> cash=0, notional=n1+n2
    chat_retry = _SequencedChatModel(
        [_ai_msg("bad 1", **u1), _ai_msg('{"verdict": "pass", "reason": "ok"}', **u2)]
    )
    svc_retry = _make_real_llm_service(
        monkeypatch,
        chat_model=chat_retry,
        model_key="muse-ember-1.0-oauth",
        credential_source=CredentialSource.OAUTH,
        max_parsing_retries=2,
    )
    res_retry = await dalton_complete(
        svc_retry,
        user_id="u1",
        user_prompt="check oauth retry",
        scope_ref="review:oauth-retry",
        purpose="publish.review",
        request_model="muse-ember-1.0-oauth",
        response_schema=_VerdictSchema,
    )
    assert res_retry.ok is True
    assert res_retry.cash_usd == 0.0
    assert res_retry.notional_usd == pytest.approx(float(n1 + n2))
    assert store.rows[res_retry.invocation_id]["cash_usd"] == Decimal("0")

    # 3. OAuth timeout without usage -> cash=0, unknown_usage_count=1, unknown_usage_cash_count=0
    svc_to = create_autospec(llm_svc_mod.LLMService, instance=True)
    svc_to.complete.side_effect = TimeoutError("oauth timeout")
    res_to = await dalton_complete(
        svc_to,
        user_id="u1",
        user_prompt="check oauth timeout",
        scope_ref="review:oauth-timeout",
        purpose="publish.review",
        request_model="muse-ember-1.0-oauth",
    )
    assert res_to.ok is False
    assert res_to.status == "indeterminate"
    assert res_to.cash_usd == 0.0

    summary = await budget_svc.reconcile_and_summarize_day(store.conn(), "u1")
    assert summary["journal_cash_usd"] == 0.0
    assert summary["spent_usd"] == 0.0
    assert summary["subscription_notional_usd"] == pytest.approx(float((n1 + n2) * 2))
    assert summary["journal_unknown_usage_count"] == 1
    assert summary["journal_unknown_usage_cash_count"] == 0
