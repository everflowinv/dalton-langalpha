"""Tests for `dalton_complete` billing on structured-output parse failures, retries, timeouts, and OAuth."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import httpx
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
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
_REAL_CREATE_LLM = llm_svc_mod.create_llm
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
    """In-memory `dalton_llm_journal` + `dalton_llm_journal_attempts` tables emulating
    Postgres CTE, ON CONFLICT, stale CAS guard, window filtering, and SUM semantics."""

    def __init__(self, *, now_fn: Any = None) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.attempts: dict[tuple[str, int, int], dict[str, Any]] = {}
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.before_stale_update_hook: Any = None

    def now(self) -> datetime:
        return self.now_fn()

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

    def _is_crash_placeholder(self, rec: dict[str, Any]) -> bool:
        return (
            rec.get("error_kind") == "crash"
            and rec.get("usage_known") is False
            and Decimal(str(rec.get("cash_usd") or "0")) == Decimal("0")
            and Decimal(str(rec.get("notional_usd") or "0")) == Decimal("0")
        )

    def _attempt_already_settled(self, inv_id: str, attempt: int) -> bool:
        for (a_inv, a_att, _seq), rec in self._store.attempts.items():
            if a_inv == inv_id and a_att == attempt and not self._is_crash_placeholder(rec):
                return True
        return False

    def _upsert_attempt(
        self,
        *,
        parent: dict[str, Any],
        attempt: int,
        call_seq: int = 1,
        outcome: str,
        error_kind: str | None,
        usage: dict[str, Any] | None,
        cash_usd: Decimal,
        notional_usd: Decimal,
        usage_known: bool,
        unknown_usage_calls: int,
        occurred_at: datetime,
        provider: str | None = None,
        billing_type: str | None = None,
        insert_only: bool = False,
    ) -> None:
        key = (str(parent["invocation_id"]), int(attempt), int(call_seq))
        existing = self._store.attempts.get(key)
        candidate = {
            "invocation_id": str(parent["invocation_id"]),
            "attempt": int(attempt),
            "call_seq": int(call_seq),
            "user_id": parent.get("user_id"),
            "scope_ref": parent.get("scope_ref"),
            "purpose": parent.get("purpose"),
            "model_key": parent.get("model_key"),
            "provider": provider or parent.get("provider"),
            "billing_type": billing_type or parent.get("billing_type"),
            "outcome": outcome,
            "error_kind": error_kind,
            "usage": dict(usage) if isinstance(usage, dict) else usage,
            "cash_usd": cash_usd,
            "notional_usd": notional_usd,
            "usage_known": bool(usage_known),
            "unknown_usage_calls": int(unknown_usage_calls),
            "occurred_at": occurred_at,
            "created_at": existing["created_at"] if existing else occurred_at,
        }
        if existing is None:
            self._store.attempts[key] = candidate
            return
        if insert_only:
            return
        if self._is_crash_placeholder(existing):
            self._store.attempts[key] = candidate

    def _record_call_records(
        self,
        *,
        parent: dict[str, Any],
        attempt: int,
        records_json: str,
        now: datetime,
        provider: str | None = None,
        billing_type: str | None = None,
    ) -> None:
        inv_id = str(parent["invocation_id"])
        if self._attempt_already_settled(inv_id, attempt):
            return
        records = json.loads(records_json)
        for item in records:
            raw_occ = item.get("occurred_at")
            if isinstance(raw_occ, str) and raw_occ:
                occ = datetime.fromisoformat(raw_occ)
            else:
                occ = parent.get("claimed_at") or now
            self._upsert_attempt(
                parent=parent,
                attempt=attempt,
                call_seq=int(item.get("call_seq") or 1),
                outcome=str(item.get("outcome") or "indeterminate"),
                error_kind=item.get("error_kind"),
                usage=item.get("usage"),
                cash_usd=Decimal(str(item.get("cash_usd") or "0")),
                notional_usd=Decimal(str(item.get("notional_usd") or "0")),
                usage_known=bool(item.get("usage_known", True)),
                unknown_usage_calls=int(item.get("unknown_usage_calls") or 0),
                occurred_at=occ,
                provider=provider,
                billing_type=billing_type,
            )

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        text = " ".join(sql.split()).upper()
        self._one = None
        self._many = []
        rows = self._store.rows
        now = self._store.now()

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
                    "claimed_at": now,
                    "completed_at": None,
                    "created_at": now,
                }
                self._one = dict(rows[inv_id])
            return

        if "SET STATE = 'INDETERMINATE', ERROR_KIND = COALESCE" in text:
            if self._store.before_stale_update_hook is not None:
                hook = self._store.before_stale_update_hook
                self._store.before_stale_update_hook = None
                res = hook()
                if asyncio.iscoroutine(res):
                    await res
            inv_id = str(params[0])
            row = rows.get(inv_id)
            matched = row is not None
            if matched and "AND STATE = 'PENDING'" in text:
                matched = row.get("state") == "pending"
            if matched and "AND ATTEMPT = %S" in text:
                matched = int(row.get("attempt") or 1) == int(params[1])
            if matched and "CLAIMED_AT IS NOT DISTINCT FROM %S" in text:
                matched = row.get("claimed_at") == params[2]
            if matched:
                row["state"] = "indeterminate"
                row["error_kind"] = row.get("error_kind") or "crash"
                row["completed_at"] = row.get("completed_at") or now
                if "DALTON_LLM_JOURNAL_ATTEMPTS" in text:
                    self._upsert_attempt(
                        parent=row,
                        attempt=int(row.get("attempt") or 1),
                        call_seq=1,
                        outcome="indeterminate",
                        error_kind=row["error_kind"],
                        usage={"usage_known": False, "unknown_usage_calls": 1},
                        cash_usd=Decimal("0"),
                        notional_usd=Decimal("0"),
                        usage_known=False,
                        unknown_usage_calls=1,
                        occurred_at=row.get("claimed_at") or now,
                        insert_only=True,
                    )
                self._one = dict(row)
            return

        if "ATTEMPT = ATTEMPT + 1" in text:
            inv_id = str(params[-1])
            row = rows.get(inv_id)
            if row is not None:
                prev_snapshot = dict(row)
                if "RECORDED_STALE" in text and prev_snapshot.get("state") == "pending":
                    self._upsert_attempt(
                        parent=prev_snapshot,
                        attempt=int(prev_snapshot.get("attempt") or 1),
                        call_seq=1,
                        outcome="indeterminate",
                        error_kind="crash",
                        usage={"usage_known": False, "unknown_usage_calls": 1},
                        cash_usd=Decimal("0"),
                        notional_usd=Decimal("0"),
                        usage_known=False,
                        unknown_usage_calls=1,
                        occurred_at=prev_snapshot.get("claimed_at") or now,
                        insert_only=True,
                    )
                # params: (invocation_id, model_key, provider, billing_type, invocation_id)
                model_key = params[1] if len(params) == 5 else params[0]
                provider = params[2] if len(params) == 5 else params[1]
                billing_type = params[3] if len(params) == 5 else params[2]
                row.update(
                    state="pending",
                    attempt=int(row.get("attempt") or 1) + 1,
                    claimed_at=now,
                    completed_at=None,
                    error_kind=None,
                    error_text=None,
                    model_key=model_key,
                    provider=provider or row.get("provider"),
                    billing_type=billing_type or row.get("billing_type"),
                )
                if "USAGE - 'CREDENTIAL_USER_ID'" in text:
                    if isinstance(row.get("usage"), dict):
                        u_copy = dict(row["usage"])
                        u_copy.pop("credential_user_id", None)
                        row["usage"] = u_copy
                elif "USAGE = NULL" in text:
                    row["usage"] = None
                self._one = dict(row)
            return

        if "SET STATE = 'DONE'" in text:
            inv_id = str(params[6])
            attempt = int(params[7])
            row = rows.get(inv_id)
            if row is not None:
                parent_snapshot = dict(row)
                if (
                    int(row.get("attempt") or 1) == attempt
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
                        completed_at=now,
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
                if "DALTON_LLM_JOURNAL_ATTEMPTS" in text:
                    # params: (..., inv_id[8], att[9], provider[10], billing_type[11], records_json[12], inv_id[13], att[14])
                    self._record_call_records(
                        parent=parent_snapshot,
                        attempt=int(params[-1]),
                        records_json=str(params[12]),
                        now=now,
                        provider=params[10],
                        billing_type=params[11],
                    )
            return

        if "UPDATE DALTON_LLM_JOURNAL" in text and "SET STATE = %S" in text:
            has_usage_param = "USAGE = " in text.split("ALREADY_SETTLED")[0]
            upd_inv_idx = 6 if has_usage_param else 5
            inv_id = str(params[upd_inv_idx])
            attempt = int(params[upd_inv_idx + 1])
            row = rows.get(inv_id)
            if row is not None:
                parent_snapshot = dict(row)
                if (
                    int(row.get("attempt") or 1) == attempt
                    and row.get("state") in ("pending", "indeterminate")
                ):
                    add_cash = Decimal(str(params[3])) if params[3] is not None else Decimal("0")
                    add_notional = Decimal(str(params[4])) if params[4] is not None else Decimal("0")
                    row.update(
                        state=params[0],
                        completed_at=now,
                        error_kind=params[1],
                        error_text=params[2],
                        cash_usd=Decimal(str(row.get("cash_usd") or "0")) + add_cash,
                        notional_usd=Decimal(str(row.get("notional_usd") or "0")) + add_notional,
                    )
                    if has_usage_param:
                        usage_val = json.loads(params[5])
                        if "COALESCE(USAGE" in text.split("ALREADY_SETTLED")[0] and isinstance(row.get("usage"), dict):
                            merged_u = dict(row["usage"])
                            merged_u.update(usage_val)
                            usage_val = merged_u
                        row["usage"] = usage_val
                    self._one = dict(row)
                if "DALTON_LLM_JOURNAL_ATTEMPTS" in text:
                    # params end with (..., inv_id, att, records_json, inv_id, att)
                    self._record_call_records(
                        parent=parent_snapshot,
                        attempt=int(params[-1]),
                        records_json=str(params[-3]),
                        now=now,
                    )
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
            day_start, day_end, uid = params[0], params[1], params[2]
            total = Decimal("0")
            attempt_invs = {inv for inv, _att, _seq in self._store.attempts}
            for a in self._store.attempts.values():
                if uid is not None and a.get("user_id") not in (uid, None):
                    continue
                occ = a.get("occurred_at")
                if not (day_start <= occ < day_end):
                    continue
                cash_val = Decimal(str(a.get("cash_usd") or "0"))
                if a.get("outcome") in ("done", "indeterminate") or cash_val > Decimal("0"):
                    total += cash_val
            for inv_id, r in rows.items():
                if inv_id in attempt_invs:
                    continue
                if uid is not None and r.get("user_id") not in (uid, None):
                    continue
                cr = r.get("created_at")
                if not (day_start <= cr < day_end):
                    continue
                cash_val = Decimal(str(r.get("cash_usd") or "0"))
                if r.get("state") in ("done", "indeterminate") or cash_val > Decimal("0"):
                    total += cash_val
            self._one = {"journal_cash_usd": total}
            return

        if "AS JOURNAL_SCOPED_USD" in text:
            uid, scope_like = params[0], str(params[2])
            needle = scope_like.strip("%")
            total = Decimal("0")
            attempt_invs = {inv for inv, _att, _seq in self._store.attempts}
            for a in self._store.attempts.values():
                if uid is not None and a.get("user_id") not in (uid, None):
                    continue
                if needle not in str(a.get("scope_ref") or ""):
                    continue
                cash_val = Decimal(str(a.get("cash_usd") or "0"))
                if a.get("outcome") in ("done", "indeterminate") or cash_val > Decimal("0"):
                    total += cash_val
            for inv_id, r in rows.items():
                if inv_id in attempt_invs:
                    continue
                if uid is not None and r.get("user_id") not in (uid, None):
                    continue
                if needle not in str(r.get("scope_ref") or ""):
                    continue
                cash_val = Decimal(str(r.get("cash_usd") or "0"))
                if r.get("state") in ("done", "indeterminate") or cash_val > Decimal("0"):
                    total += cash_val
            self._one = {"journal_scoped_usd": total}
            return

        if "FROM DALTON_LLM_JOURNAL_ATTEMPTS A" in text and "AS ROWS" in text:
            day_start, day_end, uid = params[0], params[1], params[2]
            matched: list[dict[str, Any]] = []
            attempt_invs = {inv for inv, _att, _seq in self._store.attempts}
            for a in self._store.attempts.values():
                if uid is not None and a.get("user_id") not in (uid, None):
                    continue
                occ = a.get("occurred_at")
                if not (day_start <= occ < day_end):
                    continue
                matched.append(
                    {
                        "invocation_id": a["invocation_id"],
                        "user_id": a["user_id"],
                        "scope_ref": a["scope_ref"],
                        "purpose": a["purpose"],
                        "model_key": a["model_key"],
                        "provider": a["provider"],
                        "billing_type": a["billing_type"],
                        "state": a["outcome"],
                        "attempt": a["attempt"],
                        "usage": a["usage"],
                        "cash_usd": a["cash_usd"],
                        "notional_usd": a["notional_usd"],
                        "error_kind": a["error_kind"],
                        "usage_known": a["usage_known"],
                        "unknown_usage_calls": a["unknown_usage_calls"],
                        "created_at": a["occurred_at"],
                        "completed_at": a["occurred_at"],
                    }
                )
            for inv_id, r in rows.items():
                if inv_id in attempt_invs:
                    continue
                if uid is not None and r.get("user_id") not in (uid, None):
                    continue
                cr = r.get("created_at")
                if not (day_start <= cr < day_end):
                    continue
                entry = dict(r)
                entry.setdefault("usage_known", None)
                entry.setdefault("unknown_usage_calls", None)
                matched.append(entry)
            matched.sort(key=lambda x: x["created_at"], reverse=True)
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
        api_call_mod,
        "_utc_now",
        store.now,
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
    monkeypatch.setattr(
        llm_svc_mod,
        "create_llm",
        create_autospec(_REAL_CREATE_LLM, return_value=chat_model),
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


# ---------------------------------------------------------------------------
# Round 2 review fixes (all High & Medium items from .agy/review_billing_codex.md)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_responses_api_sdk_parse_error_with_usage_bills_native_and_fallback_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """High #1 (api_call.py:224): real ChatOpenAI(use_responses_api=True) + mock HTTP transport.
    Response 1 returns truncated structured JSON WITH usage, causing openai SDK's raw_response.parse()
    to raise pydantic.ValidationError before returning `raw`. Fallback call 2 succeeds with usage.
    Both calls' tokens and USD cost must be billed in full."""
    store = _InMemoryJournalStore()
    _wire_journal_and_budget(monkeypatch, store)

    requests_seen: list[dict[str, Any]] = []

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests_seen.append(body)
        if len(requests_seen) == 1:
            # Native Responses API structured output call -> truncated output WITH usage
            return httpx.Response(
                200,
                json={
                    "id": "resp_trunc_1",
                    "object": "response",
                    "created_at": 1700000000,
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "model": "gpt-5-mini",
                    "output": [
                        {
                            "id": "msg_1",
                            "type": "message",
                            "status": "incomplete",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": '{"verdict": "pass", "rea',
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 12_000,
                        "input_tokens_details": {"cached_tokens": 2_000},
                        "output_tokens": 450,
                        "output_tokens_details": {"reasoning_tokens": 100},
                        "total_tokens": 12_450,
                    },
                },
            )
        # Fallback call -> valid JSON WITH usage
        return httpx.Response(
            200,
            json={
                "id": "resp_ok_2",
                "object": "response",
                "created_at": 1700000005,
                "status": "completed",
                "model": "gpt-5-mini",
                "output": [
                    {
                        "id": "msg_2",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"verdict": "pass", "reason": "verified"}',
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 13_000,
                    "output_tokens": 200,
                    "total_tokens": 13_200,
                },
            },
        )

    sync_client = httpx.Client(transport=httpx.MockTransport(_mock_handler))
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler))
    try:
        real_llm = ChatOpenAI(
            model="gpt-6.1-terra",
            api_key="sk-test",
            use_responses_api=True,
            streaming=False,
            http_client=sync_client,
            http_async_client=http_client,
        )
        svc = _make_real_llm_service(
            monkeypatch, chat_model=real_llm, model_key="gpt-6.1-terra", max_parsing_retries=2
        )

        res = await dalton_complete(
            svc,
            user_id="u1",
            user_prompt="check report",
            scope_ref="review:responses-sdk-trunc",
            purpose="publish.review",
            request_model="gpt-6.1-terra",
            response_schema=_VerdictSchema,
        )
    finally:
        await http_client.aclose()
        sync_client.close()

    assert len(requests_seen) == 2
    assert "text" in requests_seen[0] and "format" in requests_seen[0]["text"]
    assert res.ok is True
    assert res.status == "done"
    assert isinstance(res.data, _VerdictSchema)
    assert res.data.verdict == "pass"

    u1 = {"input_tokens": 12_000, "cached_tokens": 2_000, "output_tokens": 450}
    u2 = {"input_tokens": 13_000, "output_tokens": 200}
    c1, _ = compute_call_costs("gpt-6.1-terra", "platform", u1)
    c2, _ = compute_call_costs("gpt-6.1-terra", "platform", u2)
    expected_total = c1 + c2
    assert c1 > Decimal("0") and c2 > Decimal("0")

    assert res.cash_usd == pytest.approx(float(expected_total))
    assert res.usage["input_tokens"] == 25_000
    assert res.usage["output_tokens"] == 650
    assert res.usage["cached_tokens"] == 2_000
    assert len(res.usage["per_call_usages"]) == 2

    row = store.rows[res.invocation_id]
    assert row["cash_usd"] == expected_total
    att1 = store.attempts[(res.invocation_id, 1, 1)]
    att2 = store.attempts[(res.invocation_id, 1, 2)]
    assert att1["cash_usd"] == c1
    assert att2["cash_usd"] == c2
    assert att1["usage_known"] is True and att2["usage_known"] is True
    assert att1["unknown_usage_calls"] == 0 and att2["unknown_usage_calls"] == 0

    summary = await budget_svc.reconcile_and_summarize_day(store.conn(), "u1")
    assert summary["journal_cash_usd"] == pytest.approx(float(expected_total))
    assert summary["journal_unknown_usage_count"] == 0


@pytest.mark.asyncio
async def test_openai_chat_completions_length_finish_reason_bills_native_and_fallback_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """High #1 companion: real ChatOpenAI(use_responses_api=False) raising openai.LengthFinishReasonError
    on truncated native structured output still captures usage from the SDK exception before fallback."""
    store = _InMemoryJournalStore()
    _wire_journal_and_budget(monkeypatch, store)

    call_idx = 0

    def _mock_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1:
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl_trunc_1",
                    "object": "chat.completion",
                    "created": 1700000000,
                    "model": "gpt-5-mini",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": '{"verdict": "pass"'},
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 9_000,
                        "completion_tokens": 300,
                        "total_tokens": 9_300,
                        "prompt_tokens_details": {"cached_tokens": 1_000},
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_ok_2",
                "object": "chat.completion",
                "created": 1700000005,
                "model": "gpt-5-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": '{"verdict": "pass", "reason": "ok"}',
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10_000,
                    "completion_tokens": 150,
                    "total_tokens": 10_150,
                },
            },
        )

    sync_client = httpx.Client(transport=httpx.MockTransport(_mock_handler))
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler))
    try:
        real_llm = ChatOpenAI(
            model="gpt-6.1-terra",
            api_key="sk-test",
            use_responses_api=False,
            streaming=False,
            http_client=sync_client,
            http_async_client=http_client,
        )
        svc = _make_real_llm_service(
            monkeypatch, chat_model=real_llm, model_key="gpt-6.1-terra", max_parsing_retries=2
        )
        res = await dalton_complete(
            svc,
            user_id="u1",
            user_prompt="check report",
            scope_ref="review:chat-completions-length",
            purpose="publish.review",
            request_model="gpt-6.1-terra",
            response_schema=_VerdictSchema,
        )
    finally:
        await http_client.aclose()
        sync_client.close()

    u1 = {"input_tokens": 9_000, "cached_tokens": 1_000, "output_tokens": 300}
    u2 = {"input_tokens": 10_000, "output_tokens": 150}
    c1, _ = compute_call_costs("gpt-6.1-terra", "platform", u1)
    c2, _ = compute_call_costs("gpt-6.1-terra", "platform", u2)
    assert c1 > Decimal("0") and c2 > Decimal("0")
    assert res.ok is True
    assert res.cash_usd == pytest.approx(float(c1 + c2))
    assert len(res.usage["per_call_usages"]) == 2
    assert store.attempts[(res.invocation_id, 1, 1)]["cash_usd"] == c1
    assert store.attempts[(res.invocation_id, 1, 2)]["cash_usd"] == c2


@pytest.mark.asyncio
async def test_cross_day_resend_attributes_each_attempt_to_its_own_budget_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """High #2 (budget.py:526): Same invocation fails parsing on Day 1 (23:50 Asia/Shanghai)
    and is resent on Day 2 (00:10 Asia/Shanghai). Day 1 budget sees only c1; Day 2 budget
    sees only c2 (not $0 and not c1+c2); scoped task spend sees c1+c2."""
    # Asia/Shanghai = UTC+8:
    # Day 1 = 2026-11-20 23:50 CST -> 2026-11-20 15:50 UTC
    # Day 2 = 2026-11-21 00:10 CST -> 2026-11-20 16:10 UTC
    day1_utc = datetime(2026, 11, 20, 15, 50, 0, tzinfo=timezone.utc)
    day2_utc = datetime(2026, 11, 20, 16, 10, 0, tzinfo=timezone.utc)
    clock = {"now": day1_utc}
    store = _InMemoryJournalStore(now_fn=lambda: clock["now"])
    _wire_journal_and_budget(monkeypatch, store)

    u1 = {"input_tokens": 10_000, "output_tokens": 500}
    u2 = {"input_tokens": 20_000, "output_tokens": 900}
    c1, _ = compute_call_costs("grok-4.7", "platform", u1)
    c2, _ = compute_call_costs("grok-4.7", "platform", u2)
    assert c1 > Decimal("0") and c2 > Decimal("0") and c1 != c2

    # Day 1: attempt 1 fails parsing
    svc1 = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([_ai_msg("bad json", **u1)]),
        model_key="grok-4.7",
        max_parsing_retries=1,
    )
    res1 = await dalton_complete(
        svc1,
        user_id="u1",
        user_prompt="check report",
        scope_ref="review:cross-day@ws:ws-1",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
    )
    assert res1.status == "indeterminate"

    # Cross midnight in Asia/Shanghai into Day 2 and resend the SAME invocation_id
    clock["now"] = day2_utc
    svc2 = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([_ai_msg('{"verdict": "pass", "reason": "ok"}', **u2)]),
        model_key="grok-4.7",
        max_parsing_retries=1,
    )
    res2 = await dalton_complete(
        svc2,
        user_id="u1",
        user_prompt="check report",
        scope_ref="review:cross-day@ws:ws-1",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
        resend_indeterminate=True,
    )
    assert res2.ok is True
    assert res2.invocation_id == res1.invocation_id
    assert res2.attempt == 2
    # Parent journal row still keeps Day 1 created_at
    assert store.rows[res1.invocation_id]["created_at"] == day1_utc

    summary_day1 = await budget_svc.reconcile_and_summarize_day(
        store.conn(), "u1", now_utc=day1_utc
    )
    summary_day2 = await budget_svc.reconcile_and_summarize_day(
        store.conn(), "u1", now_utc=day2_utc
    )
    assert summary_day1["day"] == "2026-11-20"
    assert summary_day2["day"] == "2026-11-21"
    assert summary_day1["journal_cash_usd"] == pytest.approx(float(c1))
    assert summary_day1["spent_usd"] == pytest.approx(float(c1))
    assert summary_day2["journal_cash_usd"] == pytest.approx(float(c2))
    assert summary_day2["spent_usd"] == pytest.approx(float(c2))
    assert summary_day2["by_provider"]["xai"]["cash_usd"] == pytest.approx(float(c2))

    # Scoped task spend aggregates all attempts across days
    scoped = await budget_db.fetch_scoped_task_spend(
        store.conn(), "u1", workspace_id="ws-1", summary_rows=[]
    )
    assert scoped == c1 + c2


@pytest.mark.asyncio
async def test_stale_attempt_returning_after_takeover_still_bills_without_clobbering_newer_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """High #3 (llm_journal.py:494, 610): Attempt 1 goes past stale_after_s, Attempt 2 takes over
    and completes `done` with response B and cost c2, then stale Attempt 1 returns late with
    usage u1 and cost c1. Parent row keeps Attempt 2's state/response, while Attempt 1's cost
    is uniquely settled in `dalton_llm_journal_attempts` (no loss and no double-count on repeat)."""
    t0 = datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)
    clock = {"now": t0}
    store = _InMemoryJournalStore(now_fn=lambda: clock["now"])
    _wire_journal_and_budget(monkeypatch, store)
    monkeypatch.setattr(
        journal_db,
        "_is_stale",
        lambda claimed_at, stale_after_s, now_utc=None: (
            (clock["now"] - claimed_at) > timedelta(seconds=max(1, int(stale_after_s)))
            if isinstance(claimed_at, datetime)
            else False
        ),
    )

    u1 = {"input_tokens": 11_000, "output_tokens": 400}
    u2 = {"input_tokens": 14_000, "output_tokens": 600}
    c1, _ = compute_call_costs("grok-4.7", "platform", u1)
    c2, _ = compute_call_costs("grok-4.7", "platform", u2)

    attempt1_started = asyncio.Event()
    release_attempt1 = asyncio.Event()

    async def _slow_attempt1() -> AIMessage:
        attempt1_started.set()
        await release_attempt1.wait()
        return _ai_msg('{"verdict": "stale_v1", "reason": "from attempt 1"}', **u1)

    svc1 = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([_slow_attempt1]),
        model_key="grok-4.7",
        max_parsing_retries=1,
    )
    task1 = asyncio.create_task(
        dalton_complete(
            svc1,
            user_id="u1",
            user_prompt="check report",
            scope_ref="review:stale-takeover",
            purpose="publish.review",
            request_model="grok-4.7",
            response_schema=_VerdictSchema,
            stale_after_s=10,
            resend_indeterminate=True,
        )
    )
    await attempt1_started.wait()

    # Advance clock past stale_after_s -> Attempt 2 takes over and finishes first
    clock["now"] = t0 + timedelta(seconds=60)
    svc2 = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel(
            [_ai_msg('{"verdict": "fresh_v2", "reason": "from attempt 2"}', **u2)]
        ),
        model_key="grok-4.7",
        max_parsing_retries=1,
    )
    res2 = await dalton_complete(
        svc2,
        user_id="u1",
        user_prompt="check report",
        scope_ref="review:stale-takeover",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
        stale_after_s=10,
        resend_indeterminate=True,
    )
    assert res2.ok is True
    assert res2.attempt == 2
    assert res2.data.verdict == "fresh_v2"

    # Before attempt 1 returns, stale takeover left a crash placeholder for attempt 1
    inv_id = res2.invocation_id
    assert store.attempts[(inv_id, 1, 1)]["error_kind"] == "crash"
    assert store.attempts[(inv_id, 1, 1)]["usage_known"] is False

    # Now stale attempt 1 finishes late with known usage and cost c1
    clock["now"] = t0 + timedelta(seconds=90)
    release_attempt1.set()
    res1 = await task1
    assert res1.invocation_id == inv_id
    assert res1.attempt == 1

    # Parent row MUST still hold Attempt 2's state & response (never clobbered by stale Attempt 1)
    parent = store.rows[inv_id]
    assert parent["state"] == "done"
    assert parent["attempt"] == 2
    assert parent["response"]["data"]["verdict"] == "fresh_v2"

    # Attempt ledger uniquely settled BOTH attempts with known usage
    assert store.attempts[(inv_id, 1, 1)]["cash_usd"] == c1
    assert store.attempts[(inv_id, 1, 1)]["usage_known"] is True
    assert store.attempts[(inv_id, 1, 1)]["unknown_usage_calls"] == 0
    assert store.attempts[(inv_id, 2, 1)]["cash_usd"] == c2
    assert store.attempts[(inv_id, 2, 1)]["usage_known"] is True

    # Calling stale writeback for (inv_id, 1) a second time must be idempotent (no double count)
    dup = await journal_db.complete_journal_done(
        inv_id,
        attempt=1,
        response={"kind": "structured", "data": {"verdict": "dup"}},
        usage=u1,
        cash_usd=c1,
        notional_usd=Decimal("0"),
        conn=store.conn(),
    )
    assert dup is None
    assert store.attempts[(inv_id, 1, 1)]["cash_usd"] == c1

    summary = await budget_svc.reconcile_and_summarize_day(
        store.conn(), "u1", now_utc=clock["now"]
    )
    assert summary["journal_cash_usd"] == pytest.approx(float(c1 + c2))
    assert summary["spent_usd"] == pytest.approx(float(c1 + c2))
    assert summary["journal_unknown_usage_count"] == 0


@pytest.mark.asyncio
async def test_timeout_during_retry_backoff_sleep_does_not_report_false_unknown_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Medium #4a (complete.py:1329): First parse attempt returns bad JSON with usage, then
    `dalton_complete`'s timeout fires during retry backoff `asyncio.sleep` BEFORE the second
    request is sent. Attempt 1 is billed and unknown-usage count MUST be 0 (no false positive)."""
    store = _InMemoryJournalStore()
    _wire_journal_and_budget(monkeypatch, store)
    # Restore real asyncio.sleep in api_call so timeout actually fires during backoff sleep
    monkeypatch.setattr(api_call_mod.asyncio, "sleep", _REAL_ASYNCIO_SLEEP)

    u1 = {"input_tokens": 10_000, "output_tokens": 500}
    c1, _ = compute_call_costs("grok-4.7", "platform", u1)
    chat_model = _SequencedChatModel(
        [
            _ai_msg("not valid json", **u1),
            _ai_msg('{"verdict": "pass", "reason": "never sent"}', input_tokens=99_999, output_tokens=999),
        ]
    )
    svc = _make_real_llm_service(
        monkeypatch, chat_model=chat_model, model_key="grok-4.7", max_parsing_retries=3
    )

    res = await dalton_complete(
        svc,
        user_id="u1",
        user_prompt="check report",
        scope_ref="review:timeout-during-sleep",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
        timeout_s=0.05,
    )

    assert chat_model.ainvoke_calls == 1  # second request was never sent
    assert res.ok is False
    assert res.status == "indeterminate"
    assert res.error_kind == "timeout"
    assert res.cash_usd == pytest.approx(float(c1))
    assert res.usage["usage_known"] is True
    assert res.usage["unknown_usage_calls"] == 0

    att = store.attempts[(res.invocation_id, 1, 1)]
    assert att["cash_usd"] == c1
    assert att["usage_known"] is True
    assert att["unknown_usage_calls"] == 0

    summary = await budget_svc.reconcile_and_summarize_day(store.conn(), "u1")
    assert summary["journal_cash_usd"] == pytest.approx(float(c1))
    assert summary["journal_unknown_usage_count"] == 0
    assert summary["journal_unknown_usage_cash_count"] == 0
    assert "unknown_usage_calls" not in summary["by_provider"]["xai"]


@pytest.mark.asyncio
async def test_timeout_unknown_usage_preserved_after_same_invocation_resend_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Medium #4b (llm_journal.py:54): Attempt 1 times out mid-flight without usage, then
    Attempt 2 resends the SAME invocation_id and succeeds (`state='done'`). The daily summary
    must still report Attempt 1's unknown-usage call alongside Attempt 2's cash cost."""
    store = _InMemoryJournalStore()
    _wire_journal_and_budget(monkeypatch, store)

    never_set = asyncio.Event()

    async def _hang() -> AIMessage:
        await never_set.wait()
        return _ai_msg("never", input_tokens=1, output_tokens=1)

    svc_timeout = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([_hang]),
        model_key="grok-4.7",
        max_parsing_retries=1,
    )
    res1 = await dalton_complete(
        svc_timeout,
        user_id="u1",
        user_prompt="check report",
        scope_ref="review:timeout-then-resend",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
        timeout_s=0.03,
    )
    assert res1.status == "indeterminate"
    assert res1.error_kind == "timeout"
    assert store.attempts[(res1.invocation_id, 1, 1)]["usage_known"] is False
    assert store.attempts[(res1.invocation_id, 1, 1)]["unknown_usage_calls"] == 1

    # Resend same invocation_id and succeed
    u2 = {"input_tokens": 15_000, "output_tokens": 600}
    c2, _ = compute_call_costs("grok-4.7", "platform", u2)
    svc_ok = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([_ai_msg('{"verdict": "pass", "reason": "ok"}', **u2)]),
        model_key="grok-4.7",
        max_parsing_retries=1,
    )
    res2 = await dalton_complete(
        svc_ok,
        user_id="u1",
        user_prompt="check report",
        scope_ref="review:timeout-then-resend",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
        resend_indeterminate=True,
    )
    assert res2.ok is True
    assert res2.status == "done"
    assert res2.invocation_id == res1.invocation_id
    assert store.rows[res2.invocation_id]["state"] == "done"

    summary = await budget_svc.reconcile_and_summarize_day(store.conn(), "u1")
    assert summary["journal_cash_usd"] == pytest.approx(float(c2))
    assert summary["journal_unknown_usage_count"] == 1
    assert summary["journal_unknown_usage_cash_count"] == 1
    assert summary["by_provider"]["xai"]["unknown_usage_calls"] == 1
    assert summary["journal_unknown_usage_calls"][0]["invocation_id"] == res1.invocation_id
    assert summary["journal_unknown_usage_calls"][0]["attempt"] == 1


@pytest.mark.asyncio
async def test_deployment_credential_resend_preserves_prior_usage_and_strips_credential_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Medium #5 (llm_journal.py:409): Resending with `credential_user_id=None` (deployment
    credentials) must NOT clear prior `usage` — accumulated token usage must match accumulated
    `cash_usd`, while any `credential_user_id` marker is stripped."""
    store = _InMemoryJournalStore()
    _wire_journal_and_budget(monkeypatch, store)
    monkeypatch.setattr(journal_db, "is_team_credential_sharing_enabled", lambda: True)
    monkeypatch.setattr(journal_db, "credential_user_id", lambda _uid: "holder-user")
    monkeypatch.setattr(complete_mod, "is_team_credential_sharing_enabled", lambda: True)
    monkeypatch.setattr(complete_mod, "_resolve_credential_user_id", lambda _uid: "holder-user")

    u1 = {"input_tokens": 10_000, "output_tokens": 500}
    u2 = {"input_tokens": 14_000, "output_tokens": 700}
    c1, _ = compute_call_costs("grok-4.7", "platform", u1)
    c2, _ = compute_call_costs("grok-4.7", "platform", u2)

    # Attempt 1 with deployment credentials fails parsing with usage u1
    svc1 = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([_ai_msg("bad output", **u1)]),
        model_key="grok-4.7",
        max_parsing_retries=1,
    )
    res1 = await dalton_complete(
        svc1,
        user_id="u1",
        credential_user_id=None,
        user_prompt="check report",
        scope_ref="review:deploy-cred-resend",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
    )
    assert res1.status == "indeterminate"
    # Even if an earlier row carried credential_user_id, deployment-cred resend must strip ONLY that key
    store.rows[res1.invocation_id]["usage"]["credential_user_id"] = "holder-user"

    # Attempt 2 resends with credential_user_id=None and succeeds with usage u2
    svc2 = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([_ai_msg('{"verdict": "pass", "reason": "ok"}', **u2)]),
        model_key="grok-4.7",
        max_parsing_retries=1,
    )
    res2 = await dalton_complete(
        svc2,
        user_id="u1",
        credential_user_id=None,
        user_prompt="check report",
        scope_ref="review:deploy-cred-resend",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
        resend_indeterminate=True,
    )
    assert res2.ok is True
    row = store.rows[res2.invocation_id]
    assert row["cash_usd"] == c1 + c2
    assert row["usage"]["input_tokens"] == 24_000
    assert row["usage"]["output_tokens"] == 1_200
    assert len(row["usage"]["per_call_usages"]) == 2
    assert "credential_user_id" not in row["usage"]
    # Verify per-attempt rows also store exact individual usages matching individual costs
    assert store.attempts[(res2.invocation_id, 1, 1)]["cash_usd"] == c1
    assert store.attempts[(res2.invocation_id, 1, 1)]["usage"]["input_tokens"] == 10_000
    assert store.attempts[(res2.invocation_id, 2, 1)]["cash_usd"] == c2
    assert store.attempts[(res2.invocation_id, 2, 1)]["usage"]["input_tokens"] == 14_000


def test_d043_migration_revision_upgrade_and_downgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Dalton migration d043 chains off d042, splits FK DDL and batched backfill into
    separate transactions (preserving `j.created_at` as `occurred_at`), and rolls up attempts
    back into `dalton_llm_journal` before dropping the table on downgrade."""
    import contextlib
    from src.dalton.migrations.versions import d043_llm_journal_attempts as d043

    assert d043.revision == "d043"
    assert d043.down_revision == "d042"

    executed_up: list[str] = []
    executed_down: list[str] = []
    autocommit_events: list[str] = []

    class _FakeCtx:
        as_sql = True

        @contextlib.contextmanager
        def autocommit_block(self):
            autocommit_events.append("enter")
            executed_up.append("-- AUTOCOMMIT_ENTER")
            try:
                yield
            finally:
                executed_up.append("-- AUTOCOMMIT_EXIT")
                autocommit_events.append("exit")

    monkeypatch.setattr(d043.op, "get_context", lambda: _FakeCtx())
    monkeypatch.setattr(d043.op, "execute", lambda sql: executed_up.append(str(sql)))
    d043.upgrade()
    joined_up = "\n".join(executed_up)
    assert autocommit_events == ["enter", "exit"]
    assert "CREATE TABLE IF NOT EXISTS dalton_llm_journal_attempts" in joined_up
    assert "PRIMARY KEY (invocation_id, attempt, call_seq)" in joined_up
    assert "ix_dalton_llm_journal_attempts_user_occurred" in joined_up
    # FK DDL must finish BEFORE autocommit_block starts the batched backfill
    ddl_idx = joined_up.index("CREATE TABLE IF NOT EXISTS dalton_llm_journal_attempts")
    ac_idx = joined_up.index("-- AUTOCOMMIT_ENTER")
    bf_idx = joined_up.index("INSERT INTO dalton_llm_journal_attempts")
    assert ddl_idx < ac_idx < bf_idx
    assert "j.created_at AS occurred_at" in joined_up
    assert "COALESCE(j.completed_at, j.created_at)" not in joined_up
    assert "ON CONFLICT (invocation_id, attempt, call_seq) DO NOTHING" in joined_up

    monkeypatch.setattr(d043.op, "execute", lambda sql: executed_down.append(str(sql)))
    d043.downgrade()
    joined_down = "\n".join(executed_down)
    rollup_idx = joined_down.index("UPDATE dalton_llm_journal j")
    drop_idx = joined_down.index("DROP TABLE IF EXISTS dalton_llm_journal_attempts CASCADE")
    assert rollup_idx < drop_idx


# ---------------------------------------------------------------------------
# Round 3 review fixes (all High & Medium items from .agy/review_billing2_codex.md)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_status", [400, 503])
async def test_openai_responses_sdk_parse_error_then_fallback_error_no_double_bill_via_context(
    monkeypatch: pytest.MonkeyPatch,
    fallback_status: int,
) -> None:
    """Round 3 High #1 (api_call.py:271): Real ChatOpenAI(use_responses_api=True) + mock HTTP.
    Request 1 (native structured output) returns truncated JSON WITH usage (cost c1).
    Request 2 (fallback) returns HTTP 400 or 503 WITHOUT usage.
    Fallback exception must NOT inherit Request 1's usage via __context__/__cause__:
    - Total billed MUST be c1 (never 2 * c1).
    - For 400: unknown_usage_calls == 0 (unbilled rejection).
    - For 503: unknown_usage_calls == 1 (503 unknown usage is recorded, not masked)."""
    store = _InMemoryJournalStore()
    _wire_journal_and_budget(monkeypatch, store)

    requests_seen: list[dict[str, Any]] = []

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests_seen.append(body)
        if len(requests_seen) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "resp_trunc_r3",
                    "object": "response",
                    "created_at": 1700000000,
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "model": "gpt-5-mini",
                    "output": [
                        {
                            "id": "msg_1",
                            "type": "message",
                            "status": "incomplete",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": '{"verdict": "pass", "rea',
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 10_000,
                        "output_tokens": 500,
                        "total_tokens": 10_500,
                    },
                },
            )
        return httpx.Response(
            fallback_status,
            json={
                "error": {
                    "message": (
                        "invalid request payload"
                        if fallback_status == 400
                        else "service temporarily unavailable"
                    ),
                    "type": "api_error",
                }
            },
        )

    sync_client = httpx.Client(transport=httpx.MockTransport(_mock_handler))
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler))
    try:
        real_llm = ChatOpenAI(
            model="gpt-6.1-terra",
            api_key="sk-test",
            use_responses_api=True,
            streaming=False,
            max_retries=0,
            http_client=sync_client,
            http_async_client=http_client,
        )
        svc = _make_real_llm_service(
            monkeypatch, chat_model=real_llm, model_key="gpt-6.1-terra", max_parsing_retries=2
        )

        res = await dalton_complete(
            svc,
            user_id="u1",
            user_prompt="check report",
            scope_ref=f"review:r3-high1-{fallback_status}",
            purpose="publish.review",
            request_model="gpt-6.1-terra",
            response_schema=_VerdictSchema,
        )
    finally:
        await http_client.aclose()
        sync_client.close()

    assert len(requests_seen) == 2
    u1 = {"input_tokens": 10_000, "output_tokens": 500}
    c1, _ = compute_call_costs("gpt-6.1-terra", "platform", u1)
    assert c1 > Decimal("0")

    assert res.ok is False
    assert res.status == "indeterminate"
    # Critically: billed ONCE (c1), NEVER 2 * c1!
    assert res.cash_usd == pytest.approx(float(c1))
    assert res.usage["input_tokens"] == 10_000
    assert res.usage["output_tokens"] == 500
    assert len(res.usage["per_call_usages"]) == 1

    row = store.rows[res.invocation_id]
    assert row["cash_usd"] == c1

    att1 = store.attempts[(res.invocation_id, 1, 1)]
    att2 = store.attempts[(res.invocation_id, 1, 2)]
    assert att1["cash_usd"] == c1
    assert att1["usage_known"] is True
    assert att1["unknown_usage_calls"] == 0
    assert att2["cash_usd"] == Decimal("0")

    summary = await budget_svc.reconcile_and_summarize_day(store.conn(), "u1")
    assert summary["journal_cash_usd"] == pytest.approx(float(c1))
    assert summary["spent_usd"] == pytest.approx(float(c1))

    if fallback_status == 400:
        assert res.usage["unknown_usage_calls"] == 0
        assert res.usage["usage_known"] is True
        assert att2["outcome"] == "failed"
        assert att2["usage_known"] is True
        assert att2["unknown_usage_calls"] == 0
        assert summary["journal_unknown_usage_count"] == 0
    else:
        assert res.usage["unknown_usage_calls"] == 1
        assert res.usage["usage_known"] is False
        assert att2["outcome"] == "indeterminate"
        assert att2["usage_known"] is False
        assert att2["unknown_usage_calls"] == 1
        assert summary["journal_unknown_usage_count"] == 1
        assert summary["journal_unknown_usage_cash_count"] == 1

    # Direct unit check: `_extract_usage_from_sdk_exception` MUST NOT read __context__ or __cause__
    prior_exc = RuntimeError("prior")
    setattr(prior_exc, "usage", {"input_tokens": 9_999, "output_tokens": 999})
    chained_cause = RuntimeError("next_cause")
    chained_cause.__cause__ = prior_exc
    chained_ctx = RuntimeError("next_ctx")
    chained_ctx.__context__ = prior_exc
    assert api_call_mod._extract_usage_from_sdk_exception(chained_cause) == {}
    assert api_call_mod._extract_usage_from_sdk_exception(chained_ctx) == {}


@pytest.mark.asyncio
async def test_cross_midnight_send_time_and_multi_request_split_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 High #2 (llm_journal.py:611, d043:153):
    1) Single request sent before midnight (23:59:50 Asia/Shanghai) and settled after midnight
       (00:00:10 Asia/Shanghai) MUST be attributed to Day 1 (send time), NOT Day 2 (settlement).
    2) Single invocation whose 2 parse-retry requests span midnight (Req 1 on Day 1, Req 2 on Day 2)
       MUST attribute c1 to Day 1 and c2 to Day 2 with no duplication and no omission."""
    # Asia/Shanghai (UTC+8):
    # Day 1 = 2026-11-20 23:59:50 CST -> 2026-11-20 15:59:50 UTC
    # Day 2 = 2026-11-21 00:00:10 CST -> 2026-11-20 16:00:10 UTC
    day1_send_utc = datetime(2026, 11, 20, 15, 59, 50, tzinfo=timezone.utc)
    day2_settle_utc = datetime(2026, 11, 20, 16, 0, 10, tzinfo=timezone.utc)
    clock = {"now": day1_send_utc}
    store = _InMemoryJournalStore(now_fn=lambda: clock["now"])
    _wire_journal_and_budget(monkeypatch, store)
    monkeypatch.setattr(api_call_mod, "_utc_now", lambda: clock["now"])

    # --- Case 1: Single request sent on Day 1, returns/settles on Day 2 ---
    u_single = {"input_tokens": 10_000, "output_tokens": 400}
    c_single, _ = compute_call_costs("grok-4.7", "platform", u_single)
    assert c_single > Decimal("0")

    async def _slow_cross_midnight() -> AIMessage:
        # Clock crosses midnight BEFORE response returns and journal settles
        clock["now"] = day2_settle_utc
        return _ai_msg('{"verdict": "pass", "reason": "ok"}', **u_single)

    svc1 = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([_slow_cross_midnight]),
        model_key="grok-4.7",
        max_parsing_retries=1,
    )
    res1 = await dalton_complete(
        svc1,
        user_id="u1",
        user_prompt="single cross midnight",
        scope_ref="review:cross-midnight-single@ws:ws-cm",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
    )
    assert res1.ok is True
    assert store.attempts[(res1.invocation_id, 1, 1)]["occurred_at"] == day1_send_utc

    sum_d1 = await budget_svc.reconcile_and_summarize_day(store.conn(), "u1", now_utc=day1_send_utc)
    sum_d2 = await budget_svc.reconcile_and_summarize_day(store.conn(), "u1", now_utc=day2_settle_utc)
    assert sum_d1["day"] == "2026-11-20"
    assert sum_d2["day"] == "2026-11-21"
    assert sum_d1["journal_cash_usd"] == pytest.approx(float(c_single))
    assert sum_d2["journal_cash_usd"] == 0.0

    # --- Case 2: Single invocation with 2 requests spanning midnight ---
    clock["now"] = day1_send_utc
    u_req1 = {"input_tokens": 12_000, "output_tokens": 500}
    u_req2 = {"input_tokens": 18_000, "output_tokens": 700}
    c_req1, _ = compute_call_costs("grok-4.7", "platform", u_req1)
    c_req2, _ = compute_call_costs("grok-4.7", "platform", u_req2)
    assert c_req1 != c_req2

    async def _req1_day1_bad_json() -> AIMessage:
        return _ai_msg("broken json", **u_req1)

    async def _sleep_cross_midnight(_delay: float) -> None:
        clock["now"] = day2_settle_utc

    monkeypatch.setattr(
        api_call_mod.asyncio,
        "sleep",
        create_autospec(_REAL_ASYNCIO_SLEEP, side_effect=_sleep_cross_midnight),
    )

    svc2 = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel(
            [_req1_day1_bad_json, _ai_msg('{"verdict": "pass", "reason": "fixed"}', **u_req2)]
        ),
        model_key="grok-4.7",
        max_parsing_retries=2,
    )
    res2 = await dalton_complete(
        svc2,
        user_id="u1",
        user_prompt="multi request cross midnight",
        scope_ref="review:cross-midnight-multi@ws:ws-cm",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
    )
    assert res2.ok is True
    assert store.attempts[(res2.invocation_id, 1, 1)]["occurred_at"] == day1_send_utc
    assert store.attempts[(res2.invocation_id, 1, 1)]["cash_usd"] == c_req1
    assert store.attempts[(res2.invocation_id, 1, 2)]["occurred_at"] == day2_settle_utc
    assert store.attempts[(res2.invocation_id, 1, 2)]["cash_usd"] == c_req2

    sum_d1_after = await budget_svc.reconcile_and_summarize_day(
        store.conn(), "u1", now_utc=day1_send_utc
    )
    sum_d2_after = await budget_svc.reconcile_and_summarize_day(
        store.conn(), "u1", now_utc=day2_settle_utc
    )
    assert sum_d1_after["journal_cash_usd"] == pytest.approx(float(c_single + c_req1))
    assert sum_d2_after["journal_cash_usd"] == pytest.approx(float(c_req2))

    scoped = await budget_db.fetch_scoped_task_spend(
        store.conn(), "u1", workspace_id="ws-cm", summary_rows=[]
    )
    assert scoped == c_single + c_req1 + c_req2


@pytest.mark.asyncio
async def test_external_cancellation_persists_collected_usage_before_propagating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 High #3 (complete.py:1323):
    1) Request 1 returns bad JSON WITH usage u1 (c1), then caller cancels the task during retry
       backoff sleep -> CancelledError propagates, parent row is settled as `indeterminate`
       (not stuck in `pending`) with cash_usd=c1, and attempt ledger records c1 with usage_known=True.
    2) Request 1 returns bad JSON with u1 (c1), Request 2 is in flight when cancelled ->
       CancelledError propagates, c1 is billed AND Request 2 is recorded as unknown_usage_calls=1."""
    store = _InMemoryJournalStore()
    _wire_journal_and_budget(monkeypatch, store)

    # --- Case 1: Cancelled during retry backoff sleep after Req 1 returned usage ---
    u1 = {"input_tokens": 10_000, "output_tokens": 500}
    c1, _ = compute_call_costs("grok-4.7", "platform", u1)
    assert c1 > Decimal("0")

    entered_sleep = asyncio.Event()
    never_set = asyncio.Event()

    async def _blocking_sleep(_delay: float) -> None:
        entered_sleep.set()
        await never_set.wait()

    monkeypatch.setattr(
        api_call_mod.asyncio,
        "sleep",
        create_autospec(_REAL_ASYNCIO_SLEEP, side_effect=_blocking_sleep),
    )

    chat_model_1 = _SequencedChatModel(
        [
            _ai_msg("not json", **u1),
            _ai_msg('{"verdict": "pass", "reason": "never"}', input_tokens=99_000, output_tokens=999),
        ]
    )
    svc1 = _make_real_llm_service(
        monkeypatch, chat_model=chat_model_1, model_key="grok-4.7", max_parsing_retries=2
    )
    task1 = asyncio.create_task(
        dalton_complete(
            svc1,
            user_id="u1",
            user_prompt="cancel during sleep",
            scope_ref="review:cancel-sleep",
            purpose="publish.review",
            request_model="grok-4.7",
            response_schema=_VerdictSchema,
        )
    )
    await entered_sleep.wait()
    task1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task1

    assert chat_model_1.ainvoke_calls == 1
    assert len(store.rows) == 1
    inv1_id, row1 = next(iter(store.rows.items()))
    assert row1["state"] == "indeterminate"
    assert row1["error_kind"] == "cancelled"
    assert row1["cash_usd"] == c1
    att1 = store.attempts[(inv1_id, 1, 1)]
    assert att1["cash_usd"] == c1
    assert att1["usage_known"] is True
    assert att1["unknown_usage_calls"] == 0

    # --- Case 2: Cancelled while Req 2 is in flight after Req 1 returned usage ---
    monkeypatch.setattr(
        api_call_mod,
        "_utc_now",
        store.now,
    )
    monkeypatch.setattr(
        api_call_mod.asyncio,
        "sleep",
        create_autospec(_REAL_ASYNCIO_SLEEP, return_value=None),
    )
    req2_started = asyncio.Event()

    async def _hang_req2() -> AIMessage:
        req2_started.set()
        await never_set.wait()
        return _ai_msg("never", input_tokens=1, output_tokens=1)

    chat_model_2 = _SequencedChatModel([_ai_msg("not json", **u1), _hang_req2])
    svc2 = _make_real_llm_service(
        monkeypatch, chat_model=chat_model_2, model_key="grok-4.7", max_parsing_retries=2
    )
    task2 = asyncio.create_task(
        dalton_complete(
            svc2,
            user_id="u1",
            user_prompt="cancel in flight",
            scope_ref="review:cancel-inflight",
            purpose="publish.review",
            request_model="grok-4.7",
            response_schema=_VerdictSchema,
        )
    )
    await req2_started.wait()
    task2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task2

    inv2_id = next(k for k in store.rows if k != inv1_id)
    row2 = store.rows[inv2_id]
    assert row2["state"] == "indeterminate"
    assert row2["error_kind"] == "cancelled"
    assert row2["cash_usd"] == c1
    assert store.attempts[(inv2_id, 1, 1)]["cash_usd"] == c1
    assert store.attempts[(inv2_id, 1, 1)]["usage_known"] is True
    assert store.attempts[(inv2_id, 1, 2)]["cash_usd"] == Decimal("0")
    assert store.attempts[(inv2_id, 1, 2)]["usage_known"] is False
    assert store.attempts[(inv2_id, 1, 2)]["unknown_usage_calls"] == 1

    summary = await budget_svc.reconcile_and_summarize_day(store.conn(), "u1")
    assert summary["journal_cash_usd"] == pytest.approx(float(c1 * 2))
    assert summary["journal_unknown_usage_count"] == 1


@pytest.mark.asyncio
async def test_inspect_journal_state_stale_race_does_not_mark_takeover_as_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 Medium #5 (llm_journal.py:262): Process A reads stale Attempt 1; before Process A's
    stale UPDATE executes, Process B takes over as Attempt 2 (`pending`, fresh `claimed_at`).
    Process A's guarded UPDATE (`attempt = %s AND claimed_at IS NOT DISTINCT FROM %s`) MUST match
    0 rows, leave Attempt 2 intact without inserting a crash attempt row, re-read the row, and
    return `('in_flight', row_attempt_2)`."""
    t0 = datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)
    t_stale = t0 + timedelta(seconds=60)
    clock = {"now": t0}
    store = _InMemoryJournalStore(now_fn=lambda: clock["now"])
    monkeypatch.setattr(
        journal_db,
        "_is_stale",
        lambda claimed_at, stale_after_s, now_utc=None: (
            (clock["now"] - claimed_at) > timedelta(seconds=max(1, int(stale_after_s)))
            if isinstance(claimed_at, datetime)
            else False
        ),
    )

    # Seed Attempt 1 at t0
    action_c1, _row_c1 = await journal_db.claim_journal_invocation(
        invocation_id="inv-race-1",
        user_id="u1",
        scope_ref="review:race",
        purpose="publish.review",
        model_key="grok-4.7",
        provider="xai",
        billing_type="platform",
        request_hash="h-race",
        stale_after_s=10,
        conn=store.conn(),
    )
    assert action_c1 == "claimed"

    # Advance past stale_after_s so Process A sees Attempt 1 as stale
    clock["now"] = t_stale

    async def _worker_b_takes_over() -> None:
        # Process B takes over as Attempt 2 right before Process A's stale UPDATE runs
        act_b, row_b = await journal_db.claim_journal_invocation(
            invocation_id="inv-race-1",
            user_id="u1",
            scope_ref="review:race",
            purpose="publish.review",
            model_key="grok-4.7",
            provider="xai",
            billing_type="platform",
            request_hash="h-race",
            stale_after_s=10,
            resend_indeterminate=True,
            conn=store.conn(),
        )
        assert act_b == "claimed"
        assert row_b["attempt"] == 2

    store.before_stale_update_hook = _worker_b_takes_over

    # Process A inspects with resend_indeterminate=False
    action_a, row_a = await journal_db.inspect_journal_state(
        "inv-race-1",
        request_hash="h-race",
        stale_after_s=10,
        resend_indeterminate=False,
        conn=store.conn(),
    )

    assert action_a == "in_flight"
    assert row_a is not None
    assert row_a["attempt"] == 2
    assert row_a["state"] == "pending"
    # Parent row MUST still be Attempt 2 pending (NOT overwritten to indeterminate/crash)
    assert store.rows["inv-race-1"]["state"] == "pending"
    assert store.rows["inv-race-1"]["attempt"] == 2
    assert store.rows["inv-race-1"]["error_kind"] is None
    # Only Attempt 1 has the crash placeholder from B's takeover; Attempt 2 has NO bogus crash row
    assert ("inv-race-1", 1, 1) in store.attempts
    assert ("inv-race-1", 2, 1) not in store.attempts


@pytest.mark.asyncio
async def test_unsent_config_timeout_and_xai_402_pre_stream_not_counted_as_unknown_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 Medium #6 (complete.py:1363, api_call.py:285):
    1) Timeout during `resolve_llm_config` (before any upstream request is sent) MUST record
       `unknown_usage_calls == 0` and `status == 'failed'`.
    2) xAI HTTP 402 ('usage balance exhausted') before stream starts MUST reuse provider error
       classification so `unknown_usage_calls == 0` and `status == 'failed'`.
    3) xAI 402 AFTER stream started (`stream_started=True`) MUST still record `unknown_usage_calls == 1`."""
    from src.dalton.llm.providers.xai_grok.client import mark_xai_exception

    store = _InMemoryJournalStore()
    _wire_journal_and_budget(monkeypatch, store)

    # --- Case 1: Timeout during resolve_llm_config (0 upstream sends) ---
    never_set = asyncio.Event()

    async def _hanging_resolve(*_args: Any, **_kwargs: Any) -> Any:
        await never_set.wait()

    monkeypatch.setattr(
        llm_svc_mod,
        "resolve_llm_config",
        create_autospec(_REAL_RESOLVE_LLM_CONFIG, side_effect=_hanging_resolve),
    )
    svc_cfg_timeout = llm_svc_mod.LLMService(
        agent_config=SimpleNamespace(llm=SimpleNamespace(background="grok-4.7", name="grok-4.7"))
    )
    res_cfg = await dalton_complete(
        svc_cfg_timeout,
        user_id="u1",
        user_prompt="config timeout",
        scope_ref="review:r3-cfg-timeout",
        purpose="publish.review",
        request_model="grok-4.7",
        timeout_s=0.03,
    )
    assert res_cfg.ok is False
    assert res_cfg.status == "failed"
    assert res_cfg.error_kind == "timeout"
    assert res_cfg.cash_usd == 0.0
    assert store.rows[res_cfg.invocation_id]["state"] == "failed"
    assert store.attempts[(res_cfg.invocation_id, 1, 1)]["usage_known"] is True
    assert store.attempts[(res_cfg.invocation_id, 1, 1)]["unknown_usage_calls"] == 0

    # --- Case 2: xAI 402 'usage balance exhausted' before stream starts ---
    xai_402_pre = mark_xai_exception(
        RuntimeError("HTTP 402 Payment Required: usage balance exhausted"),
        stream_started=False,
        chunks_received=0,
    )
    setattr(xai_402_pre, "status_code", 402)
    svc_xai_pre = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([xai_402_pre]),
        model_key="grok-4.7-oauth",
        credential_source=CredentialSource.OAUTH,
        max_parsing_retries=1,
    )
    res_xai_pre = await dalton_complete(
        svc_xai_pre,
        user_id="u1",
        user_prompt="xai 402 pre stream",
        scope_ref="review:r3-xai-402-pre",
        purpose="publish.review",
        request_model="grok-4.7-oauth",
        response_schema=_VerdictSchema,
    )
    assert res_xai_pre.ok is False
    assert res_xai_pre.status == "failed"
    assert res_xai_pre.error_kind == "billing"
    assert res_xai_pre.cash_usd == 0.0
    assert store.rows[res_xai_pre.invocation_id]["state"] == "failed"
    assert store.attempts[(res_xai_pre.invocation_id, 1, 1)]["usage_known"] is True
    assert store.attempts[(res_xai_pre.invocation_id, 1, 1)]["unknown_usage_calls"] == 0

    summary_before_mid = await budget_svc.reconcile_and_summarize_day(store.conn(), "u1")
    assert summary_before_mid["journal_unknown_usage_count"] == 0
    assert summary_before_mid["journal_unknown_usage_cash_count"] == 0

    # --- Case 3: xAI 402 mid-stream (stream already started) MUST still be unknown ---
    xai_402_mid = mark_xai_exception(
        RuntimeError("HTTP 402 Payment Required: usage balance exhausted"),
        stream_started=True,
        chunks_received=2,
    )
    setattr(xai_402_mid, "status_code", 402)
    svc_xai_mid = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([xai_402_mid]),
        model_key="grok-4.7-oauth",
        credential_source=CredentialSource.OAUTH,
        max_parsing_retries=1,
    )
    res_xai_mid = await dalton_complete(
        svc_xai_mid,
        user_id="u1",
        user_prompt="xai 402 mid stream",
        scope_ref="review:r3-xai-402-mid",
        purpose="publish.review",
        request_model="grok-4.7-oauth",
        response_schema=_VerdictSchema,
    )
    assert res_xai_mid.ok is False
    assert res_xai_mid.status == "indeterminate"
    assert store.attempts[(res_xai_mid.invocation_id, 1, 1)]["usage_known"] is False
    assert store.attempts[(res_xai_mid.invocation_id, 1, 1)]["unknown_usage_calls"] == 1
    summary_after_mid = await budget_svc.reconcile_and_summarize_day(store.conn(), "u1")
    assert summary_after_mid["journal_unknown_usage_count"] == 1


def test_d043_upgrade_separates_fk_ddl_and_batched_backfill_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 Medium #7 (d043:37) & High #2 backfill:
    Online upgrade runs FK table + index creation in Transaction 1, exits into `autocommit_block()`
    (releasing the SHARE ROW EXCLUSIVE lock on `dalton_llm_journal`), then backfills historical
    rows across multiple keyset batches using `j.created_at AS occurred_at`."""
    import contextlib
    from src.dalton.migrations.versions import d043_llm_journal_attempts as d043

    events: list[str] = []
    batch_params: list[dict[str, Any]] = []
    batch_responses = [
        ("lj1:1000", d043._BACKFILL_BATCH_SIZE),
        ("lj1:2000", d043._BACKFILL_BATCH_SIZE),
        ("lj1:2250", 250),
    ]

    class _FakeResult:
        def __init__(self, row: tuple[Any, ...]) -> None:
            self._row = row

        def fetchone(self) -> tuple[Any, ...]:
            return self._row

    class _FakeBind:
        def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _FakeResult:
            sql_text = str(stmt)
            if "INSERT INTO dalton_llm_journal_attempts" in sql_text:
                assert "j.created_at AS occurred_at" in sql_text
                assert "COALESCE(j.completed_at, j.created_at)" not in sql_text
                assert "LIMIT :batch_size" in sql_text
                events.append("backfill_batch")
                batch_params.append(dict(params or {}))
                return _FakeResult(batch_responses[len(batch_params) - 1])
            events.append(f"conn_sql:{sql_text.strip()}")
            return _FakeResult((None, 0))

    class _OnlineCtx:
        as_sql = False

        @contextlib.contextmanager
        def autocommit_block(self):
            events.append("autocommit_enter")
            try:
                yield
            finally:
                events.append("autocommit_exit")

    monkeypatch.setattr(d043.op, "get_context", lambda: _OnlineCtx())
    monkeypatch.setattr(d043.op, "get_bind", lambda: _FakeBind())
    monkeypatch.setattr(
        d043.op,
        "execute",
        lambda sql: events.append(
            "create_table"
            if "CREATE TABLE" in str(sql)
            else ("create_index" if "CREATE INDEX" in str(sql) else f"op_sql:{str(sql).strip()}")
        ),
    )

    d043.upgrade()

    assert events.index("create_table") < events.index("autocommit_enter")
    assert events.index("create_index") < events.index("autocommit_enter")
    ac_enter = events.index("autocommit_enter")
    ac_exit = events.index("autocommit_exit")
    batch_indices = [i for i, e in enumerate(events) if e == "backfill_batch"]
    assert len(batch_indices) == 3
    assert all(ac_enter < i < ac_exit for i in batch_indices)
    assert batch_params == [
        {"last_id": "", "batch_size": 1000},
        {"last_id": "lj1:1000", "batch_size": 1000},
        {"last_id": "lj1:2000", "batch_size": 1000},
    ]
    assert "conn_sql:SET lock_timeout = '5s'" in events
    assert "conn_sql:SET lock_timeout = '0'" in events


@pytest.mark.asyncio
async def test_d043_downgrade_rolls_up_late_settled_attempts_into_parent_before_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 High #4 (d043:164): After Attempt 2 takes over stale Attempt 1 and finishes `done`
    (parent row holds only c2/u2), stale Attempt 1 returns late and settles c1/u1 ONLY into
    `dalton_llm_journal_attempts`. Running `d043.downgrade()` MUST roll up c1+c2, summed token
    usage, `per_call_usages`, and `attempt_details` into `dalton_llm_journal` BEFORE dropping
    `dalton_llm_journal_attempts`, so d042's budget query retains the full c1+c2 spend."""
    from src.dalton.migrations.versions import d043_llm_journal_attempts as d043

    t0 = datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)
    clock = {"now": t0}
    store = _InMemoryJournalStore(now_fn=lambda: clock["now"])
    _wire_journal_and_budget(monkeypatch, store)
    monkeypatch.setattr(
        journal_db,
        "_is_stale",
        lambda claimed_at, stale_after_s, now_utc=None: (
            (clock["now"] - claimed_at) > timedelta(seconds=max(1, int(stale_after_s)))
            if isinstance(claimed_at, datetime)
            else False
        ),
    )

    u1 = {"input_tokens": 11_000, "output_tokens": 400}
    u2 = {"input_tokens": 14_000, "output_tokens": 600}
    c1, _ = compute_call_costs("grok-4.7", "platform", u1)
    c2, _ = compute_call_costs("grok-4.7", "platform", u2)
    assert c1 > Decimal("0") and c2 > Decimal("0")

    # Attempt 1 starts and hangs; Attempt 2 takes over and completes done; Attempt 1 settles late.
    att1_started = asyncio.Event()
    release_att1 = asyncio.Event()

    async def _slow_att1() -> AIMessage:
        att1_started.set()
        await release_att1.wait()
        return _ai_msg('{"verdict": "v1", "reason": "late"}', **u1)

    svc1 = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([_slow_att1]),
        model_key="grok-4.7",
        max_parsing_retries=1,
    )
    task1 = asyncio.create_task(
        dalton_complete(
            svc1,
            user_id="u1",
            user_prompt="downgrade rollup test",
            scope_ref="review:d043-downgrade",
            purpose="publish.review",
            request_model="grok-4.7",
            response_schema=_VerdictSchema,
            stale_after_s=10,
            resend_indeterminate=True,
        )
    )
    await att1_started.wait()

    clock["now"] = t0 + timedelta(seconds=60)
    svc2 = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([_ai_msg('{"verdict": "v2", "reason": "fresh"}', **u2)]),
        model_key="grok-4.7",
        max_parsing_retries=1,
    )
    res2 = await dalton_complete(
        svc2,
        user_id="u1",
        user_prompt="downgrade rollup test",
        scope_ref="review:d043-downgrade",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
        stale_after_s=10,
        resend_indeterminate=True,
    )
    inv_id = res2.invocation_id

    clock["now"] = t0 + timedelta(seconds=90)
    release_att1.set()
    await task1

    # Confirm the exact pre-downgrade hazard: parent row only has c2, while attempts has c1 + c2.
    assert store.rows[inv_id]["cash_usd"] == c2
    assert store.attempts[(inv_id, 1, 1)]["cash_usd"] == c1
    assert store.attempts[(inv_id, 2, 1)]["cash_usd"] == c2

    # Execute d043.downgrade(), verifying rollup SQL runs BEFORE DROP TABLE and mirrors its semantics
    downgrade_steps: list[str] = []
    attempts_dropped = False

    def _exec_downgrade_sql(sql: Any) -> None:
        nonlocal attempts_dropped
        s = str(sql)
        if "UPDATE dalton_llm_journal j" in s:
            assert not attempts_dropped, "rollup MUST run before dropping attempts table"
            assert "GREATEST(COALESCE(j.cash_usd, 0), agg.total_cash_usd)" in s
            assert "GREATEST(COALESCE(j.notional_usd, 0), agg.total_notional_usd)" in s
            assert "'per_call_usages', pca.per_call_usages" in s
            assert "'attempt_details', agg.attempt_details" in s
            downgrade_steps.append("rollup")
            # Apply the exact rollup aggregation onto in-memory tables
            by_inv: dict[str, list[dict[str, Any]]] = {}
            for rec in store.attempts.values():
                by_inv.setdefault(rec["invocation_id"], []).append(rec)
            for j_inv, recs in by_inv.items():
                recs.sort(key=lambda r: (r["attempt"], r["call_seq"]))
                parent = store.rows[j_inv]
                tot_cash = sum((Decimal(str(r["cash_usd"])) for r in recs), Decimal("0"))
                tot_notional = sum((Decimal(str(r["notional_usd"])) for r in recs), Decimal("0"))
                tot_unk = sum(int(r["unknown_usage_calls"]) for r in recs)
                all_known = all(bool(r["usage_known"]) for r in recs) and tot_unk == 0
                per_call: list[dict[str, Any]] = []
                sums: dict[str, int] = {}
                for r in recs:
                    ru = r.get("usage") or {}
                    if isinstance(ru.get("per_call_usages"), list) and ru["per_call_usages"]:
                        per_call.extend(ru["per_call_usages"])
                    elif any((ru.get(k) or 0) > 0 for k in ("input_tokens", "output_tokens", "total_tokens")):
                        per_call.append(
                            {k: ru[k] for k in ("input_tokens", "output_tokens", "total_tokens") if k in ru}
                        )
                    for k in ("input_tokens", "output_tokens", "total_tokens"):
                        if isinstance(ru.get(k), (int, float)):
                            sums[k] = sums.get(k, 0) + int(ru[k])
                parent["cash_usd"] = max(Decimal(str(parent["cash_usd"])), tot_cash)
                parent["notional_usd"] = max(Decimal(str(parent["notional_usd"])), tot_notional)
                if parent["state"] != "done" and (tot_cash > 0 or tot_notional > 0 or tot_unk > 0):
                    parent["state"] = "indeterminate"
                new_usage = dict(parent.get("usage") or {})
                new_usage.update(sums)
                new_usage["per_call_usages"] = per_call
                new_usage["usage_known"] = all_known
                new_usage["unknown_usage_calls"] = tot_unk
                new_usage["attempt_details"] = [
                    {
                        "attempt": r["attempt"],
                        "call_seq": r["call_seq"],
                        "cash_usd": r["cash_usd"],
                        "occurred_at": r["occurred_at"],
                    }
                    for r in recs
                ]
                parent["usage"] = new_usage
        elif "DROP TABLE IF EXISTS dalton_llm_journal_attempts" in s:
            downgrade_steps.append("drop")
            store.attempts.clear()
            attempts_dropped = True

    monkeypatch.setattr(d043.op, "execute", _exec_downgrade_sql)
    d043.downgrade()

    assert downgrade_steps == ["rollup", "drop"]
    assert attempts_dropped is True
    assert store.attempts == {}

    # Under d042 (reading parent `dalton_llm_journal` only), ZERO cost or usage was lost!
    parent = store.rows[inv_id]
    assert parent["state"] == "done"
    assert parent["cash_usd"] == c1 + c2
    assert parent["usage"]["input_tokens"] == 25_000
    assert parent["usage"]["output_tokens"] == 1_000
    assert parent["usage"]["total_tokens"] == 26_000
    assert len(parent["usage"]["per_call_usages"]) == 2
    assert len(parent["usage"]["attempt_details"]) == 2
    assert parent["usage"]["usage_known"] is True
    assert parent["usage"]["unknown_usage_calls"] == 0

    # d042 budget query (legacy branch when attempts table is gone/empty) sees full c1 + c2!
    d042_cash = sum(
        (
            Decimal(str(r["cash_usd"]))
            for r in store.rows.values()
            if r["state"] in ("done", "indeterminate")
        ),
        Decimal("0"),
    )
    assert d042_cash == c1 + c2
