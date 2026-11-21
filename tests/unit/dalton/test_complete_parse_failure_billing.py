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
            return candidate
        if insert_only:
            return None
        if self._is_crash_placeholder(existing):
            self._store.attempts[key] = candidate
            return candidate
        return None

    def _record_call_records(
        self,
        *,
        parent: dict[str, Any],
        attempt: int,
        records_json: str,
        now: datetime,
        provider: str | None = None,
        billing_type: str | None = None,
    ) -> list[dict[str, Any]]:
        inv_id = str(parent["invocation_id"])
        if self._attempt_already_settled(inv_id, attempt):
            return []
        records = json.loads(records_json)
        written: list[dict[str, Any]] = []
        for item in records:
            raw_occ = item.get("occurred_at")
            if isinstance(raw_occ, str) and raw_occ:
                occ = datetime.fromisoformat(raw_occ)
            else:
                occ = parent.get("claimed_at") or now
            res = self._upsert_attempt(
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
            if res is not None:
                written.append(res)
        return written

    @staticmethod
    def _extract_calls_from_usage(u: Any) -> list[dict[str, Any]]:
        if not isinstance(u, dict):
            return []
        raw_calls = u.get("per_call_usages")
        if isinstance(raw_calls, list) and raw_calls:
            return [dict(c) for c in raw_calls if isinstance(c, dict)]
        tok_keys = (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "cache_5m_tokens",
            "cache_1h_tokens",
            "reasoning_tokens",
            "audio_input_tokens",
            "audio_output_tokens",
        )
        single = {
            k: u[k]
            for k in tok_keys
            if isinstance(u.get(k), (int, float)) and not isinstance(u.get(k), bool)
        }
        if any(single.get(k, 0) > 0 for k in tok_keys[:6]):
            return [single]
        return []

    @classmethod
    def _merge_usage_dicts_sql_equiv(
        cls,
        base: Any,
        delta: Any,
        *,
        strip_credential_holder: bool = False,
    ) -> dict[str, Any]:
        tok_keys = (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "cache_5m_tokens",
            "cache_1h_tokens",
            "reasoning_tokens",
            "audio_input_tokens",
            "audio_output_tokens",
        )
        b_dict = dict(base) if isinstance(base, dict) else {}
        d_dict = dict(delta) if isinstance(delta, dict) else {}
        merged = {**b_dict, **d_dict}
        for k in tok_keys:
            bv = b_dict.get(k)
            dv = d_dict.get(k)
            b_num = bv if isinstance(bv, (int, float)) and not isinstance(bv, bool) else 0
            d_num = dv if isinstance(dv, (int, float)) and not isinstance(dv, bool) else 0
            if b_num + d_num > 0:
                merged[k] = b_num + d_num
        combined_calls = cls._extract_calls_from_usage(b_dict) + cls._extract_calls_from_usage(d_dict)
        if combined_calls:
            merged["per_call_usages"] = combined_calls
        if strip_credential_holder:
            merged.pop("credential_user_id", None)
        return merged

    @classmethod
    def _apply_late_parent_sync(cls, row: dict[str, Any], written: list[dict[str, Any]]) -> None:
        if not written:
            return
        tok_keys = (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "cache_5m_tokens",
            "cache_1h_tokens",
            "reasoning_tokens",
            "audio_input_tokens",
            "audio_output_tokens",
        )
        add_cash = sum((Decimal(str(r.get("cash_usd") or "0")) for r in written), Decimal("0"))
        add_notional = sum((Decimal(str(r.get("notional_usd") or "0")) for r in written), Decimal("0"))
        add_tokens: dict[str, int | float] = {}
        add_calls: list[dict[str, Any]] = []
        for r in sorted(written, key=lambda item: int(item.get("call_seq") or 1)):
            ru = r.get("usage")
            if isinstance(ru, dict):
                for k in tok_keys:
                    v = ru.get(k)
                    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                        add_tokens[k] = add_tokens.get(k, 0) + v
                add_calls.extend(cls._extract_calls_from_usage(ru))
        if add_cash <= Decimal("0") and add_notional <= Decimal("0") and not add_tokens:
            return
        row["cash_usd"] = Decimal(str(row.get("cash_usd") or "0")) + add_cash
        row["notional_usd"] = Decimal(str(row.get("notional_usd") or "0")) + add_notional
        if add_tokens:
            base_u = dict(row.get("usage") or {})
            for k in tok_keys:
                bv = base_u.get(k)
                b_num = bv if isinstance(bv, (int, float)) and not isinstance(bv, bool) else 0
                tot = b_num + add_tokens.get(k, 0)
                if tot > 0:
                    base_u[k] = tot
            combined_calls = cls._extract_calls_from_usage(row.get("usage")) + add_calls
            if combined_calls:
                base_u["per_call_usages"] = combined_calls
            row["usage"] = base_u

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
                updated_parent = False
                if (
                    int(row.get("attempt") or 1) == attempt
                    and row.get("state") in ("pending", "indeterminate")
                ):
                    updated_parent = True
                    resp_val = json.loads(params[0]) if params[0] is not None else None
                    usage_val = json.loads(params[1]) if params[1] is not None else {}
                    upd_part = text.split("ALREADY_SETTLED")[0]
                    if "FROM (VALUES (%S::JSONB)) AS D(U)" in upd_part:
                        usage_val = self._merge_usage_dicts_sql_equiv(
                            row.get("usage"),
                            usage_val,
                            strip_credential_holder="- 'CREDENTIAL_USER_ID'" in upd_part,
                        )
                    elif "COALESCE(USAGE" in upd_part and isinstance(row.get("usage"), dict):
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
                    written = self._record_call_records(
                        parent=parent_snapshot,
                        attempt=int(params[-1]),
                        records_json=str(params[12]),
                        now=now,
                        provider=params[10],
                        billing_type=params[11],
                    )
                    if not updated_parent and "LATE_PARENT_SYNC" in text:
                        self._apply_late_parent_sync(row, written)
            return

        if "UPDATE DALTON_LLM_JOURNAL" in text and "SET STATE = %S" in text:
            upd_part = text.split("ALREADY_SETTLED")[0]
            has_usage_param = "USAGE = " in upd_part
            upd_inv_idx = 6 if has_usage_param else 5
            inv_id = str(params[upd_inv_idx])
            attempt = int(params[upd_inv_idx + 1])
            row = rows.get(inv_id)
            if row is not None:
                parent_snapshot = dict(row)
                updated_parent = False
                if (
                    int(row.get("attempt") or 1) == attempt
                    and row.get("state") in ("pending", "indeterminate")
                ):
                    updated_parent = True
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
                        if "FROM (VALUES (%S::JSONB)) AS D(U)" in upd_part:
                            usage_val = self._merge_usage_dicts_sql_equiv(
                                row.get("usage"),
                                usage_val,
                                strip_credential_holder="- 'CREDENTIAL_USER_ID'" in upd_part,
                            )
                        elif "COALESCE(USAGE" in upd_part and isinstance(row.get("usage"), dict):
                            merged_u = dict(row["usage"])
                            merged_u.update(usage_val)
                            usage_val = merged_u
                        row["usage"] = usage_val
                    self._one = dict(row)
                if "DALTON_LLM_JOURNAL_ATTEMPTS" in text:
                    # params end with (..., inv_id, att, records_json, inv_id, att)
                    written = self._record_call_records(
                        parent=parent_snapshot,
                        attempt=int(params[-1]),
                        records_json=str(params[-3]),
                        now=now,
                    )
                    if not updated_parent and "LATE_PARENT_SYNC" in text:
                        self._apply_late_parent_sync(row, written)
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


def test_d044_migration_revision_upgrade_and_downgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Dalton migration d044 chains off d043, splits FK DDL and batched backfill into
    separate transactions (preserving `j.created_at` as `occurred_at`), and rolls up attempts
    back into `dalton_llm_journal` before dropping the table on downgrade."""
    import contextlib
    from src.dalton.migrations.versions import d044_llm_journal_attempts as d044

    assert d044.revision == "d044"
    assert d044.down_revision == "d043"

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

    monkeypatch.setattr(d044.op, "get_context", lambda: _FakeCtx())
    monkeypatch.setattr(d044.op, "execute", lambda sql: executed_up.append(str(sql)))
    d044.upgrade()
    joined_up = "\n".join(executed_up)
    assert autocommit_events == ["enter", "exit"]
    assert "CREATE TABLE IF NOT EXISTS dalton_llm_journal_attempts" in joined_up
    assert "PRIMARY KEY (invocation_id, attempt, call_seq)" in joined_up
    assert "ix_dalton_llm_journal_attempts_user_occurred" in joined_up
    # Carbon Medium #3: table created WITHOUT inline FK -> autocommit batched backfill ->
    # secondary indexes -> ADD CONSTRAINT ... FOREIGN KEY ... NOT VALID -> VALIDATE CONSTRAINT.
    ddl_stmt = next(s for s in executed_up if "CREATE TABLE IF NOT EXISTS dalton_llm_journal_attempts" in s)
    assert "REFERENCES dalton_llm_journal" not in ddl_stmt
    ddl_idx = joined_up.index("CREATE TABLE IF NOT EXISTS dalton_llm_journal_attempts")
    ac_idx = joined_up.index("-- AUTOCOMMIT_ENTER")
    bf_idx = joined_up.index("INSERT INTO dalton_llm_journal_attempts")
    ix_idx = joined_up.index("ix_dalton_llm_journal_attempts_user_occurred")
    fk_add_idx = joined_up.index("NOT VALID")
    fk_val_idx = joined_up.index("VALIDATE CONSTRAINT fk_dalton_llm_journal_attempts_invocation")
    assert ddl_idx < ac_idx < bf_idx < ix_idx < fk_add_idx < fk_val_idx
    assert "j.created_at AS occurred_at" in joined_up
    assert "COALESCE(j.completed_at, j.created_at)" not in joined_up
    assert "ON CONFLICT (invocation_id, attempt, call_seq) DO NOTHING" in joined_up

    autocommit_down_events: list[str] = []

    class _FakeDownCtx:
        as_sql = True

        @contextlib.contextmanager
        def autocommit_block(self):
            autocommit_down_events.append("enter")
            executed_down.append("-- DOWN_AUTOCOMMIT_ENTER")
            try:
                yield
            finally:
                executed_down.append("-- DOWN_AUTOCOMMIT_EXIT")
                autocommit_down_events.append("exit")

    monkeypatch.setattr(d044.op, "get_context", lambda: _FakeDownCtx())
    monkeypatch.setattr(d044.op, "execute", lambda sql: executed_down.append(str(sql)))
    d044.downgrade()
    assert autocommit_down_events == ["enter", "exit"]
    joined_down = "\n".join(executed_down)
    down_ac_enter = joined_down.index("-- DOWN_AUTOCOMMIT_ENTER")
    rollup_idx = joined_down.index("UPDATE dalton_llm_journal j")
    down_ac_exit = joined_down.index("-- DOWN_AUTOCOMMIT_EXIT")
    drop_fk_idx = joined_down.index("DROP CONSTRAINT IF EXISTS fk_dalton_llm_journal_attempts_invocation")
    drop_idx = joined_down.index("DROP TABLE IF EXISTS dalton_llm_journal_attempts CASCADE")
    assert down_ac_enter < rollup_idx < down_ac_exit < drop_fk_idx < drop_idx


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
    """Round 3 High #2 (llm_journal.py:611, d044:153):
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


def test_d044_upgrade_separates_fk_ddl_and_batched_backfill_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 Medium #7 & Round 4 Carbon Medium #3 (d044):
    Online upgrade creates the table WITHOUT inline FK in Transaction 1, enters `autocommit_block()`,
    runs bounded keyset backfill batches (`j.created_at AS occurred_at`), then builds secondary
    indexes, then attaches FK as `NOT VALID`, and finally validates the FK constraint separately
    so `dalton_llm_journal` writes are never blocked during backfill."""
    import contextlib
    from src.dalton.migrations.versions import d044_llm_journal_attempts as d044

    events: list[str] = []
    batch_params: list[dict[str, Any]] = []
    batch_responses = [
        ("lj1:1000", d044._BACKFILL_BATCH_SIZE),
        ("lj1:2000", d044._BACKFILL_BATCH_SIZE),
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
            if "CREATE INDEX" in sql_text:
                events.append("create_index")
                return _FakeResult((None, 0))
            if "NOT VALID" in sql_text and "ADD CONSTRAINT fk_dalton_llm_journal_attempts_invocation" in sql_text:
                events.append("add_fk_not_valid")
                return _FakeResult((None, 0))
            if "VALIDATE CONSTRAINT fk_dalton_llm_journal_attempts_invocation" in sql_text:
                events.append("validate_fk")
                return _FakeResult((None, 0))
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

    monkeypatch.setattr(d044.op, "get_context", lambda: _OnlineCtx())
    monkeypatch.setattr(d044.op, "get_bind", lambda: _FakeBind())

    def _rec_op_exec(sql: Any) -> None:
        s = str(sql)
        if "CREATE TABLE" in s:
            assert "REFERENCES dalton_llm_journal" not in s
            events.append("create_table_no_fk")
        elif "CREATE INDEX" in s:
            events.append("create_index")
        else:
            events.append(f"op_sql:{s.strip()}")

    monkeypatch.setattr(d044.op, "execute", _rec_op_exec)

    d044.upgrade()

    ac_enter = events.index("autocommit_enter")
    ac_exit = events.index("autocommit_exit")
    assert events.index("create_table_no_fk") < ac_enter
    batch_indices = [i for i, e in enumerate(events) if e == "backfill_batch"]
    index_indices = [i for i, e in enumerate(events) if e == "create_index"]
    fk_add_idx = events.index("add_fk_not_valid")
    fk_val_idx = events.index("validate_fk")
    assert len(batch_indices) == 3
    assert len(index_indices) == 2
    assert all(ac_enter < i < ac_exit for i in batch_indices)
    assert max(batch_indices) < min(index_indices)
    assert max(index_indices) < fk_add_idx < fk_val_idx < ac_exit
    assert batch_params == [
        {"last_id": "", "batch_size": 1000},
        {"last_id": "lj1:1000", "batch_size": 1000},
        {"last_id": "lj1:2000", "batch_size": 1000},
    ]
    assert "conn_sql:SET lock_timeout = '5s'" in events
    assert "conn_sql:SET lock_timeout = '0'" in events


@pytest.mark.asyncio
async def test_d044_downgrade_rolls_up_late_settled_attempts_into_parent_before_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 High #4 (d044:164): After Attempt 2 takes over stale Attempt 1 and finishes `done`
    (parent row holds only c2/u2), stale Attempt 1 returns late and settles c1/u1 ONLY into
    `dalton_llm_journal_attempts`. Running `d044.downgrade()` MUST roll up c1+c2, summed token
    usage, `per_call_usages`, and `attempt_details` into `dalton_llm_journal` BEFORE dropping
    `dalton_llm_journal_attempts`, so d042's budget query retains the full c1+c2 spend."""
    from src.dalton.migrations.versions import d044_llm_journal_attempts as d044

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
            scope_ref="review:d044-downgrade",
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
        scope_ref="review:d044-downgrade",
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

    # Round 4 Carbon Medium #4 already syncs late attempt 1 back into parent; also seed a
    # lagging parent row (simulating a pre-r4 row where parent only holds c2 while attempts has c1+c2)
    # to prove d044.downgrade() rolls up lagging rows AND is idempotent on already-synced rows.
    assert store.rows[inv_id]["cash_usd"] == c1 + c2
    assert store.attempts[(inv_id, 1, 1)]["cash_usd"] == c1
    assert store.attempts[(inv_id, 2, 1)]["cash_usd"] == c2
    store.rows[inv_id]["cash_usd"] = c2
    store.rows[inv_id]["usage"] = {**u2, "total_tokens": u2["input_tokens"] + u2["output_tokens"]}

    # Execute d044.downgrade(), verifying batched rollup SQL runs inside autocommit_block()
    # BEFORE the separate short DDL transaction drops FK and table.
    import contextlib

    downgrade_steps: list[str] = []
    attempts_dropped = False

    class _RollupResult:
        def __init__(self, row: tuple[Any, ...]) -> None:
            self._row = row

        def fetchone(self) -> tuple[Any, ...]:
            return self._row

    class _RollupBind:
        def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _RollupResult:
            s = str(stmt)
            if "SELECT to_regclass('dalton_llm_journal_attempts')" in s:
                return _RollupResult(("dalton_llm_journal_attempts",))
            if "UPDATE dalton_llm_journal j" in s:
                assert not attempts_dropped, "rollup MUST run before dropping attempts table"
                assert "GREATEST(COALESCE(j.cash_usd, 0), agg.total_cash_usd)" in s
                assert "GREATEST(COALESCE(j.notional_usd, 0), agg.total_notional_usd)" in s
                assert "'per_call_usages', pca.per_call_usages" in s
                assert "'attempt_details', agg.attempt_details" in s
                assert "agg.attempt_row_count > 1" in s
                downgrade_steps.append("rollup")
                # Apply the exact rollup aggregation onto in-memory tables
                by_inv: dict[str, list[dict[str, Any]]] = {}
                for rec in store.attempts.values():
                    by_inv.setdefault(rec["invocation_id"], []).append(rec)
                last_id = str((params or {}).get("last_id") or "")
                inv_keys = sorted(k for k in by_inv if k > last_id)
                for j_inv in inv_keys:
                    recs = by_inv[j_inv]
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
                return _RollupResult((inv_keys[-1] if inv_keys else None, len(inv_keys)))
            return _RollupResult((None, 0))

    class _DownOnlineCtx:
        as_sql = False

        @contextlib.contextmanager
        def autocommit_block(self):
            downgrade_steps.append("autocommit_enter")
            try:
                yield
            finally:
                downgrade_steps.append("autocommit_exit")

    def _exec_downgrade_sql(sql: Any) -> None:
        nonlocal attempts_dropped
        s = str(sql)
        if "DROP TABLE IF EXISTS dalton_llm_journal_attempts" in s:
            downgrade_steps.append("drop")
            store.attempts.clear()
            attempts_dropped = True

    monkeypatch.setattr(d044.op, "get_context", lambda: _DownOnlineCtx())
    monkeypatch.setattr(d044.op, "get_bind", lambda: _RollupBind())
    monkeypatch.setattr(d044.op, "execute", _exec_downgrade_sql)
    d044.downgrade()

    assert downgrade_steps == ["autocommit_enter", "rollup", "autocommit_exit", "drop"]
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


# ---------------------------------------------------------------------------
# Round 4 review fixes (all High & Medium items from .agy/review_billing2_carbon.md)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d044_backfill_pending_row_with_prior_cost_maps_outcome_and_prior_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carbon High #1 (d044:45, 93-101, 155-160):
    A historical `done` row (`persist_raw=False` / `persist_response=False` / pruned `response`)
    with `cash_usd > 0` (and an OAuth row with `notional_usd > 0`) is reclaimed into
    `state = 'pending', attempt >= 2` when `d044` backfill runs.
    Backfill MUST:
    1) Never insert `outcome = 'pending'` (which violates `CHECK (outcome IN ('done', 'failed', 'indeterminate'))`).
    2) Write prior cost to `GREATEST(1, j.attempt - 1)` instead of occupying in-flight `j.attempt`.
    3) Leave in-flight `attempt = 2` free so when attempt 2 completes, BOTH c1 and c2 are billed."""
    from src.dalton.migrations.versions import d044_llm_journal_attempts as d044

    # Verify both online and offline backfill SQL use safe attempt/outcome mapping
    for sql_text in (d044._ONLINE_BACKFILL_BATCH_SQL, d044._OFFLINE_BATCHED_BACKFILL_SQL):
        assert "WHEN j.state = 'pending' THEN GREATEST(1, j.attempt - 1)" in sql_text
        assert "WHEN j.state IN ('done', 'failed', 'indeterminate') THEN j.state" in sql_text
        assert "ELSE 'indeterminate'" in sql_text
        assert "WHEN j.state IN ('indeterminate', 'pending') AND (" in sql_text

    t0 = datetime(2026, 11, 21, 9, 0, 0, tzinfo=timezone.utc)
    store = _InMemoryJournalStore(now_fn=lambda: t0)
    _wire_journal_and_budget(monkeypatch, store)

    u1 = {"input_tokens": 10_000, "output_tokens": 500}
    u2 = {"input_tokens": 12_000, "output_tokens": 600}
    c1, _ = compute_call_costs("grok-4.7", "platform", u1)
    c2, _ = compute_call_costs("grok-4.7", "platform", u2)
    _, n1 = compute_call_costs("muse-ember-1.0-oauth", "oauth", u1)
    assert c1 > Decimal("0") and c2 > Decimal("0") and n1 > Decimal("0")

    # 1. Historical done row with persist_response=False (response=None) -> reclaimed into pending attempt=2
    svc1 = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([_ai_msg('{"verdict": "pass", "reason": "v1"}', **u1)]),
        model_key="grok-4.7",
        max_parsing_retries=1,
    )
    res1 = await dalton_complete(
        svc1,
        user_id="u1",
        user_prompt="reclaimed pending backfill",
        scope_ref="review:carbon-h1-cash",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
        persist_response=False,
    )
    assert res1.ok is True
    inv_cash = res1.invocation_id
    # Simulate pre-d044 state (attempts table doesn't exist yet) and reclaim into state='pending', attempt=2
    store.attempts.clear()
    act2, row2 = await journal_db.claim_journal_invocation(
        invocation_id=inv_cash,
        user_id="u1",
        scope_ref="review:carbon-h1-cash",
        purpose="publish.review",
        model_key="grok-4.7",
        provider="xai",
        billing_type="platform",
        request_hash=store.rows[inv_cash]["request_hash"],
        conn=store.conn(),
    )
    assert act2 == "claimed"
    assert row2["state"] == "pending"
    assert row2["attempt"] == 2
    assert row2["cash_usd"] == c1

    # Also seed a pre-d044 OAuth row reclaimed into state='pending', attempt=3 with prior notional_usd
    # and prior unknown_usage_calls=1 (from an earlier indeterminate attempt)
    inv_oauth = "lj1:carbon-h1-oauth-pending"
    store.rows[inv_oauth] = {
        "invocation_id": inv_oauth,
        "user_id": "u1",
        "scope_ref": "review:carbon-h1-oauth",
        "purpose": "publish.review",
        "model_key": "muse-ember-1.0-oauth",
        "provider": "muse-oauth",
        "billing_type": "oauth",
        "request_hash": "hash-oauth",
        "state": "pending",
        "attempt": 3,
        "cash_usd": Decimal("0"),
        "notional_usd": n1,
        "usage": {**u1, "usage_known": False, "unknown_usage_calls": 1},
        "response": None,
        "error_kind": None,
        "error_text": None,
        "claimed_at": t0,
        "completed_at": None,
        "created_at": t0,
    }

    # Emulate d044 backfill SQL semantics WITH strict CHECK constraint enforcement
    allowed_outcomes = {"done", "failed", "indeterminate"}
    for inv_id, j in store.rows.items():
        if not (
            j["state"] in ("done", "indeterminate")
            or Decimal(str(j["cash_usd"])) > Decimal("0")
            or Decimal(str(j["notional_usd"])) > Decimal("0")
        ):
            continue
        if any(a_inv == inv_id for a_inv, _att, _seq in store.attempts):
            continue
        bf_attempt = max(1, int(j["attempt"]) - 1) if j["state"] == "pending" else int(j["attempt"])
        bf_outcome = j["state"] if j["state"] in allowed_outcomes else "indeterminate"
        # Strict CHECK (outcome IN ('done', 'failed', 'indeterminate'))
        assert bf_outcome in allowed_outcomes, f"CHECK violation: outcome={bf_outcome!r}"
        j_usage = j.get("usage") or {}
        u_known_flag = j_usage.get("usage_known")
        u_unk_raw = j_usage.get("unknown_usage_calls")
        if isinstance(u_unk_raw, int) and u_unk_raw >= 0:
            bf_unk = u_unk_raw
        elif j["state"] in ("indeterminate", "pending") and u_known_flag is False:
            bf_unk = 1
        else:
            bf_unk = 0
        bf_known = not (j["state"] in ("indeterminate", "pending") and u_known_flag is False)
        store.attempts[(inv_id, bf_attempt, 1)] = {
            "invocation_id": inv_id,
            "attempt": bf_attempt,
            "call_seq": 1,
            "user_id": j["user_id"],
            "scope_ref": j["scope_ref"],
            "purpose": j["purpose"],
            "model_key": j["model_key"],
            "provider": j["provider"],
            "billing_type": j["billing_type"],
            "outcome": bf_outcome,
            "error_kind": j["error_kind"],
            "usage": dict(j_usage),
            "cash_usd": Decimal(str(j["cash_usd"])),
            "notional_usd": Decimal(str(j["notional_usd"])),
            "usage_known": bf_known,
            "unknown_usage_calls": bf_unk,
            "occurred_at": j["created_at"],
            "created_at": j["created_at"],
        }

    # Verify backfilled rows landed on prior attempt (attempt-1), NOT in-flight attempt
    assert (inv_cash, 1, 1) in store.attempts
    assert (inv_cash, 2, 1) not in store.attempts
    assert store.attempts[(inv_cash, 1, 1)]["outcome"] == "indeterminate"
    assert store.attempts[(inv_cash, 1, 1)]["cash_usd"] == c1
    assert store.attempts[(inv_cash, 1, 1)]["usage_known"] is True
    assert store.attempts[(inv_cash, 1, 1)]["unknown_usage_calls"] == 0

    assert (inv_oauth, 2, 1) in store.attempts
    assert (inv_oauth, 3, 1) not in store.attempts
    assert store.attempts[(inv_oauth, 2, 1)]["outcome"] == "indeterminate"
    assert store.attempts[(inv_oauth, 2, 1)]["notional_usd"] == n1
    assert store.attempts[(inv_oauth, 2, 1)]["usage_known"] is False
    assert store.attempts[(inv_oauth, 2, 1)]["unknown_usage_calls"] == 1

    # Now in-flight Attempt 2 completes `done` with c2 -> MUST NOT collide with backfilled Attempt 1!
    done_row = await journal_db.complete_journal_done(
        inv_cash,
        attempt=2,
        response={"kind": "structured", "data": {"verdict": "pass", "reason": "v2"}},
        usage={**u1, **u2},
        attempt_usage=u2,
        cash_usd=c2,
        notional_usd=Decimal("0"),
        provider="xai",
        billing_type="platform",
        occurred_at=t0,
        conn=store.conn(),
    )
    assert done_row is not None
    assert store.attempts[(inv_cash, 1, 1)]["cash_usd"] == c1
    assert store.attempts[(inv_cash, 2, 1)]["cash_usd"] == c2
    assert store.rows[inv_cash]["cash_usd"] == c1 + c2

    summary = await budget_svc.reconcile_and_summarize_day(store.conn(), "u1", now_utc=t0)
    assert summary["journal_cash_usd"] == pytest.approx(float(c1 + c2))
    assert summary["subscription_notional_usd"] == pytest.approx(float(n1))
    assert summary["journal_unknown_usage_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_kind", ["http_400", "http_422", "xai_pre_stream_billing"])
async def test_native_unknown_failure_then_fallback_unbilled_4xx_marks_indeterminate_not_failed(
    monkeypatch: pytest.MonkeyPatch,
    fallback_kind: str,
) -> None:
    """Carbon High #2 (complete.py:1346-1374, api_call.py:458-524):
    With `response_schema`, native `client.ainvoke` fails with a 503/timeout without usage
    (`unknown_calls=1`, `has_usage=False`), then `make_api_call` enters fallback where `llm.ainvoke`
    fails with a deterministic unbilled error (HTTP 400 / HTTP 422 / xAI pre-stream billing).
    Parent `state` and `DaltonCompleteResult.status` MUST be escalated to `'indeterminate'`
    (NOT `'failed'`), blocking auto-resend (`resend_indeterminate=False`) and `assist_json` fallback."""
    from src.dalton.llm import assist as assist_mod
    from src.dalton.llm.providers.xai_grok.client import mark_xai_exception

    store = _InMemoryJournalStore()
    _wire_journal_and_budget(monkeypatch, store)

    native_err = RuntimeError("HTTP 503 Service Unavailable")
    setattr(native_err, "status_code", 503)

    if fallback_kind == "http_400":
        fb_err: BaseException = RuntimeError("HTTP 400 Bad Request: context length exceeded")
        setattr(fb_err, "status_code", 400)
        model_key = "grok-4.7"
        cred_src = CredentialSource.PLATFORM
    elif fallback_kind == "http_422":
        fb_err = RuntimeError("HTTP 422 Unprocessable Entity: schema too large")
        setattr(fb_err, "status_code", 422)
        model_key = "grok-4.7"
        cred_src = CredentialSource.PLATFORM
    else:
        fb_err = mark_xai_exception(
            RuntimeError("HTTP 402 Payment Required: usage balance exhausted"),
            stream_started=False,
            chunks_received=0,
        )
        setattr(fb_err, "status_code", 402)
        model_key = "grok-4.7-oauth"
        cred_src = CredentialSource.OAUTH

    chat_model = _SequencedChatModel(
        [fb_err],
        structured_responses=[native_err],
    )
    svc = _make_real_llm_service(
        monkeypatch,
        chat_model=chat_model,
        model_key=model_key,
        credential_source=cred_src,
        max_parsing_retries=2,
    )

    res = await dalton_complete(
        svc,
        user_id="u1",
        user_prompt="check report",
        scope_ref=f"review:carbon-h2-{fallback_kind}",
        purpose="publish.review",
        request_model=model_key,
        response_schema=_VerdictSchema,
    )

    assert chat_model.structured_calls == 1
    assert chat_model.ainvoke_calls == 1
    assert res.ok is False
    assert res.status == "indeterminate"
    assert res.usage["usage_known"] is False
    assert res.usage["unknown_usage_calls"] == 1

    row = store.rows[res.invocation_id]
    assert row["state"] == "indeterminate"
    assert row["usage"]["usage_known"] is False
    assert row["usage"]["unknown_usage_calls"] == 1

    # Attempt ledger distinguishes Request 1 (unknown 503) vs Request 2 (unbilled 4xx/billing)
    assert store.attempts[(res.invocation_id, 1, 1)]["outcome"] == "indeterminate"
    assert store.attempts[(res.invocation_id, 1, 1)]["unknown_usage_calls"] == 1
    assert store.attempts[(res.invocation_id, 1, 2)]["outcome"] == "failed"
    assert store.attempts[(res.invocation_id, 1, 2)]["unknown_usage_calls"] == 0

    # `assist_json` MUST NOT treat this result as safe to fall back (would double-bill!)
    assert assist_mod._is_safe_to_fallback(res) is False

    # Re-calling `dalton_complete` with `resend_indeterminate=False` MUST refuse auto-resend!
    res_again = await dalton_complete(
        svc,
        user_id="u1",
        user_prompt="check report",
        scope_ref=f"review:carbon-h2-{fallback_kind}",
        purpose="publish.review",
        request_model=model_key,
        response_schema=_VerdictSchema,
        resend_indeterminate=False,
    )
    assert res_again.ok is False
    assert res_again.status == "indeterminate"
    assert chat_model.structured_calls == 1  # ZERO extra upstream calls
    assert chat_model.ainvoke_calls == 1


@pytest.mark.asyncio
async def test_stale_attempt_late_settlement_syncs_parent_for_cache_replay_and_pending_takeover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carbon Medium #4 (llm_journal.py:580-629, 773-819):
    Case A: Attempt 1 goes past `stale_after_s`, Attempt 2 takes over and completes `done`
      (`u2, c2, n2`), then stale Attempt 1 returns late (`u1, c1, n1`). Parent row MUST keep
      Attempt 2's `state='done'`, `attempt=2`, `response='fresh_v2'` while syncing `c1+c2`,
      `n1+n2`, and summed token `usage` (`per_call_usages` len 2) back into `dalton_llm_journal`,
      so a subsequent cache replay returns full cumulative `usage` and `notional_usd`!
    Case B: Stale Attempt 1 returns late while Attempt 2 is STILL `pending`, then Attempt 2
      finishes `done`. Attempt 2's settlement MUST NOT overwrite Attempt 1's late-synced `usage`."""
    t0 = datetime(2026, 11, 21, 11, 0, 0, tzinfo=timezone.utc)
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

    u1 = {"input_tokens": 100_000, "output_tokens": 1_000}
    u2 = {"input_tokens": 150_000, "output_tokens": 1_500}
    _, n1 = compute_call_costs("muse-ember-1.0-oauth", "oauth", u1)
    _, n2 = compute_call_costs("muse-ember-1.0-oauth", "oauth", u2)
    assert n1 > Decimal("0") and n2 > Decimal("0")

    # --- Case A: Late Attempt 1 returns AFTER Attempt 2 completed `done` (OAuth notional + cache replay) ---
    a1_started = asyncio.Event()
    a1_release = asyncio.Event()

    async def _slow_a1() -> AIMessage:
        a1_started.set()
        await a1_release.wait()
        return _ai_msg('{"verdict": "stale_v1", "reason": "late"}', **u1)

    svc_a1 = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([_slow_a1]),
        model_key="muse-ember-1.0-oauth",
        credential_source=CredentialSource.OAUTH,
        max_parsing_retries=1,
    )
    task_a1 = asyncio.create_task(
        dalton_complete(
            svc_a1,
            user_id="u1",
            user_prompt="stale replay check",
            scope_ref="review:carbon-m4-case-a",
            purpose="publish.review",
            request_model="muse-ember-1.0-oauth",
            response_schema=_VerdictSchema,
            stale_after_s=10,
            resend_indeterminate=True,
        )
    )
    await a1_started.wait()

    clock["now"] = t0 + timedelta(seconds=60)
    svc_a2 = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel(
            [_ai_msg('{"verdict": "fresh_v2", "reason": "winner"}', **u2)]
        ),
        model_key="muse-ember-1.0-oauth",
        credential_source=CredentialSource.OAUTH,
        max_parsing_retries=1,
    )
    res_a2 = await dalton_complete(
        svc_a2,
        user_id="u1",
        user_prompt="stale replay check",
        scope_ref="review:carbon-m4-case-a",
        purpose="publish.review",
        request_model="muse-ember-1.0-oauth",
        response_schema=_VerdictSchema,
        stale_after_s=10,
        resend_indeterminate=True,
    )
    inv_a = res_a2.invocation_id
    assert res_a2.ok is True and res_a2.attempt == 2

    # Stale Attempt 1 now returns late -> syncs notional_usd & usage back to parent without clobbering v2
    clock["now"] = t0 + timedelta(seconds=90)
    a1_release.set()
    await task_a1

    parent_a = store.rows[inv_a]
    assert parent_a["state"] == "done"
    assert parent_a["attempt"] == 2
    assert parent_a["response"]["data"]["verdict"] == "fresh_v2"
    assert parent_a["cash_usd"] == Decimal("0")
    assert parent_a["notional_usd"] == n1 + n2
    assert parent_a["usage"]["input_tokens"] == 250_000
    assert parent_a["usage"]["output_tokens"] == 2_500
    assert len(parent_a["usage"]["per_call_usages"]) == 2

    # Duplicate late writeback for Attempt 1 MUST be idempotent (zero double count)
    dup_res = await journal_db.complete_journal_done(
        inv_a,
        attempt=1,
        response={"kind": "structured", "data": {"verdict": "dup"}},
        usage=u1,
        cash_usd=Decimal("0"),
        notional_usd=n1,
        conn=store.conn(),
    )
    assert dup_res is None
    assert store.rows[inv_a]["notional_usd"] == n1 + n2
    assert store.rows[inv_a]["usage"]["input_tokens"] == 250_000

    # Subsequent cache replay returns full cumulative usage and notional_usd!
    replay_svc = create_autospec(llm_svc_mod.LLMService, instance=True)
    replayed = await dalton_complete(
        replay_svc,
        user_id="u1",
        user_prompt="stale replay check",
        scope_ref="review:carbon-m4-case-a",
        purpose="publish.review",
        request_model="muse-ember-1.0-oauth",
        response_schema=_VerdictSchema,
    )
    replay_svc.complete.assert_not_awaited()
    assert replayed.ok is True
    assert replayed.cached is True
    assert replayed.replayed is True
    assert replayed.data.verdict == "fresh_v2"
    assert replayed.cash_usd == 0.0
    assert replayed.notional_usd == pytest.approx(float(n1 + n2))
    assert replayed.usage["input_tokens"] == 250_000
    assert replayed.usage["output_tokens"] == 2_500
    assert len(replayed.usage["per_call_usages"]) == 2

    # --- Case B: Late Attempt 1 returns WHILE Attempt 2 is still `pending`, then Attempt 2 completes ---
    c1, _ = compute_call_costs("grok-4.7", "platform", u1)
    c2, _ = compute_call_costs("grok-4.7", "platform", u2)
    b1_started = asyncio.Event()
    b1_release = asyncio.Event()
    b2_started = asyncio.Event()
    b2_release = asyncio.Event()

    async def _slow_b1() -> AIMessage:
        b1_started.set()
        await b1_release.wait()
        return _ai_msg('{"verdict": "b1_late", "reason": "late"}', **u1)

    async def _slow_b2() -> AIMessage:
        b2_started.set()
        await b2_release.wait()
        return _ai_msg('{"verdict": "b2_fresh", "reason": "winner"}', **u2)

    clock["now"] = t0
    svc_b1 = _make_real_llm_service(
        monkeypatch, chat_model=_SequencedChatModel([_slow_b1]), model_key="grok-4.7", max_parsing_retries=1
    )
    task_b1 = asyncio.create_task(
        dalton_complete(
            svc_b1,
            user_id="u1",
            user_prompt="stale pending race",
            scope_ref="review:carbon-m4-case-b",
            purpose="publish.review",
            request_model="grok-4.7",
            response_schema=_VerdictSchema,
            stale_after_s=10,
            resend_indeterminate=True,
        )
    )
    await b1_started.wait()

    clock["now"] = t0 + timedelta(seconds=60)
    svc_b2 = _make_real_llm_service(
        monkeypatch, chat_model=_SequencedChatModel([_slow_b2]), model_key="grok-4.7", max_parsing_retries=1
    )
    task_b2 = asyncio.create_task(
        dalton_complete(
            svc_b2,
            user_id="u1",
            user_prompt="stale pending race",
            scope_ref="review:carbon-m4-case-b",
            purpose="publish.review",
            request_model="grok-4.7",
            response_schema=_VerdictSchema,
            stale_after_s=10,
            resend_indeterminate=True,
        )
    )
    await b2_started.wait()

    # Attempt 1 finishes while Attempt 2 is still in flight (pending)
    clock["now"] = t0 + timedelta(seconds=70)
    b1_release.set()
    res_b1 = await task_b1
    inv_b = res_b1.invocation_id
    assert store.rows[inv_b]["state"] == "pending"
    assert store.rows[inv_b]["attempt"] == 2
    assert store.rows[inv_b]["cash_usd"] == c1
    assert store.rows[inv_b]["usage"]["input_tokens"] == 100_000

    # Now Attempt 2 finishes -> parent MUST keep c1+c2 AND u1+u2 (never clobber u1!)
    clock["now"] = t0 + timedelta(seconds=80)
    b2_release.set()
    res_b2 = await task_b2
    assert res_b2.ok is True and res_b2.attempt == 2
    parent_b = store.rows[inv_b]
    assert parent_b["state"] == "done"
    assert parent_b["attempt"] == 2
    assert parent_b["response"]["data"]["verdict"] == "b2_fresh"
    assert parent_b["cash_usd"] == c1 + c2
    assert parent_b["usage"]["input_tokens"] == 250_000
    assert parent_b["usage"]["output_tokens"] == 2_500
    assert len(parent_b["usage"]["per_call_usages"]) == 2


@pytest.mark.asyncio
async def test_unbilled_402_billing_muse_invalid_api_key_and_text_4xx_never_count_unknown_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carbon Medium #5 (api_call.py:277-305, 341-345, complete.py:1356-1376, llm_journal.py:56-65):
    1) Single calls encountering non-xAI pre-stream HTTP 402, xAI pre-stream 'used all credits'
       (without status_code), muse-oauth structured `invalid_api_key` (without status_code), or
       text-only 'HTTP 400'/'status 401' MUST record `state='failed', unknown_usage_calls=0`.
    2) Parse retry where Call 1 returns bad JSON WITH usage u1 (c1) and Call 2 hits pre-stream
       HTTP 402 (or xAI 'usage balance exhausted') MUST bill c1 with `usage_known=True,
       unknown_usage_calls=0` (no false unknown-usage in summary or by_provider).
    3) Direct `_is_unbilled_sdk_error` aligns with `_classify_exception` and STILL returns False
       for mid-stream 402 (`stream_started=True` / `chunks_received > 0`)."""
    from src.dalton.llm.providers.xai_grok.client import mark_xai_exception

    store = _InMemoryJournalStore()
    _wire_journal_and_budget(monkeypatch, store)

    # 1a. Non-xAI pre-stream HTTP 402
    err_402 = RuntimeError("Payment required: insufficient balance")
    setattr(err_402, "status_code", 402)
    assert api_call_mod._is_unbilled_sdk_error(err_402) is True
    svc_402 = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([err_402]),
        model_key="gpt-6.1-terra",
        max_parsing_retries=1,
    )
    res_402 = await dalton_complete(
        svc_402,
        user_id="u1",
        user_prompt="single 402",
        scope_ref="review:carbon-m5-402",
        purpose="publish.review",
        request_model="gpt-6.1-terra",
        response_schema=_VerdictSchema,
    )
    assert res_402.ok is False
    assert res_402.status == "failed"
    assert res_402.error_kind == "http_402"
    assert store.attempts[(res_402.invocation_id, 1, 1)]["unknown_usage_calls"] == 0
    assert store.attempts[(res_402.invocation_id, 1, 1)]["usage_known"] is True

    # 1b. xAI pre-stream "used all credits" WITHOUT numeric status_code
    err_xai_credits = mark_xai_exception(
        RuntimeError("You have used all credits for this billing cycle"),
        stream_started=False,
        chunks_received=0,
    )
    assert api_call_mod._is_unbilled_sdk_error(err_xai_credits) is True
    svc_xai = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([err_xai_credits]),
        model_key="grok-4.7-oauth",
        credential_source=CredentialSource.OAUTH,
        max_parsing_retries=1,
    )
    res_xai = await dalton_complete(
        svc_xai,
        user_id="u1",
        user_prompt="xai credits",
        scope_ref="review:carbon-m5-xai-credits",
        purpose="publish.review",
        request_model="grok-4.7-oauth",
        response_schema=_VerdictSchema,
    )
    assert res_xai.status == "failed"
    assert res_xai.error_kind == "billing"
    assert store.attempts[(res_xai.invocation_id, 1, 1)]["unknown_usage_calls"] == 0

    # 1c. muse-oauth structured `invalid_api_key` WITHOUT numeric status_code
    err_muse_key = RuntimeError("Authentication rejected")
    setattr(err_muse_key, "code", "invalid_api_key")
    assert api_call_mod._is_unbilled_sdk_error(err_muse_key) is True
    svc_muse = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([err_muse_key]),
        model_key="muse-ember-1.0-oauth",
        credential_source=CredentialSource.OAUTH,
        max_parsing_retries=1,
    )
    res_muse = await dalton_complete(
        svc_muse,
        user_id="u1",
        user_prompt="muse invalid key",
        scope_ref="review:carbon-m5-muse-key",
        purpose="publish.review",
        request_model="muse-ember-1.0-oauth",
        response_schema=_VerdictSchema,
    )
    assert res_muse.status == "failed"
    assert res_muse.error_kind == "auth_expired"
    assert store.attempts[(res_muse.invocation_id, 1, 1)]["unknown_usage_calls"] == 0

    # 1d. Text-only 4xx ("HTTP 400" / "status 401") WITHOUT numeric status_code
    err_text_400 = RuntimeError("Provider returned HTTP 400: malformed prompt")
    err_text_401 = RuntimeError("Upstream rejected with status 401 unauthorized")
    assert api_call_mod._is_unbilled_sdk_error(err_text_400) is True
    assert api_call_mod._is_unbilled_sdk_error(err_text_401) is True
    svc_t400 = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([err_text_400]),
        model_key="grok-4.7",
        max_parsing_retries=1,
    )
    res_t400 = await dalton_complete(
        svc_t400,
        user_id="u1",
        user_prompt="text 400",
        scope_ref="review:carbon-m5-text-400",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
    )
    assert res_t400.status == "failed"
    assert res_t400.error_kind == "http_4xx"
    assert store.attempts[(res_t400.invocation_id, 1, 1)]["unknown_usage_calls"] == 0

    # 2. Parse retry: Call 1 returns bad JSON WITH usage u1 (c1), Call 2 hits pre-stream HTTP 402
    u1 = {"input_tokens": 10_000, "output_tokens": 500}
    c1, _ = compute_call_costs("gpt-6.1-terra", "platform", u1)
    assert c1 > Decimal("0")
    retry_402 = RuntimeError("HTTP 402 Payment Required: balance exhausted")
    setattr(retry_402, "status_code", 402)
    svc_retry_402 = _make_real_llm_service(
        monkeypatch,
        chat_model=_SequencedChatModel([_ai_msg("not json", **u1), retry_402]),
        model_key="gpt-6.1-terra",
        max_parsing_retries=2,
    )
    res_retry = await dalton_complete(
        svc_retry_402,
        user_id="u1",
        user_prompt="retry then 402",
        scope_ref="review:carbon-m5-retry-402",
        purpose="publish.review",
        request_model="gpt-6.1-terra",
        response_schema=_VerdictSchema,
    )
    assert res_retry.ok is False
    assert res_retry.status == "indeterminate"  # Call 1 was billed!
    assert res_retry.cash_usd == pytest.approx(float(c1))
    assert res_retry.usage["usage_known"] is True
    assert res_retry.usage["unknown_usage_calls"] == 0
    assert store.attempts[(res_retry.invocation_id, 1, 1)]["cash_usd"] == c1
    assert store.attempts[(res_retry.invocation_id, 1, 1)]["unknown_usage_calls"] == 0
    assert store.attempts[(res_retry.invocation_id, 1, 2)]["outcome"] == "failed"
    assert store.attempts[(res_retry.invocation_id, 1, 2)]["unknown_usage_calls"] == 0

    summary = await budget_svc.reconcile_and_summarize_day(store.conn(), "u1")
    assert summary["journal_cash_usd"] == pytest.approx(float(c1))
    assert summary["journal_unknown_usage_count"] == 0
    assert summary["journal_unknown_usage_cash_count"] == 0
    for prov_stats in summary["by_provider"].values():
        assert "unknown_usage_calls" not in prov_stats

    # 3. Mid-stream 402 MUST still return False from `_is_unbilled_sdk_error`
    err_mid_402 = mark_xai_exception(
        RuntimeError("HTTP 402 Payment Required: usage balance exhausted"),
        stream_started=True,
        chunks_received=2,
    )
    setattr(err_mid_402, "status_code", 402)
    assert api_call_mod._is_unbilled_sdk_error(err_mid_402) is False


# ---------------------------------------------------------------------------
# Round 5 final fixes (3 narrow items from .agy/review_billing3_{carbon,codex}.md)
# ---------------------------------------------------------------------------


def test_d044_downgrade_batches_divergent_rollup_in_autocommit_then_drops_in_short_tx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5 Item 1 (Carbon High / Codex Medium):
    `d044.downgrade()` MUST:
    1) Run rollup inside `ctx.autocommit_block()` in keyset batches of 1000 `invocation_id`s
       (both online and offline `as_sql=True` with `DO $$ ... LOOP ... COMMIT; ... END $$`).
    2) Filter UPDATE to only divergent rows (higher cash/notional/unknown calls, multi-row
       attempts, or non-done rows needing escalation to indeterminate), skipping already-consistent
       backfilled historical rows.
    3) Exit `autocommit_block()` first, then execute `SET LOCAL lock_timeout = '5s'`,
       `DROP CONSTRAINT`, and `DROP TABLE` in a separate short DDL transaction."""
    import contextlib
    from src.dalton.migrations.versions import d044_llm_journal_attempts as d044

    # --- Part A: Online mode (`as_sql=False`) multi-batch keyset pagination + diff filter ---
    online_events: list[str] = []
    batch_params: list[dict[str, Any]] = []
    batch_responses = [
        ("lj1:1000", d044._BACKFILL_BATCH_SIZE),
        ("lj1:2000", d044._BACKFILL_BATCH_SIZE),
        ("lj1:2150", 150),
    ]

    class _FakeResult:
        def __init__(self, row: tuple[Any, ...]) -> None:
            self._row = row

        def fetchone(self) -> tuple[Any, ...]:
            return self._row

    class _FakeBind:
        def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _FakeResult:
            s = str(stmt)
            if "SELECT to_regclass('dalton_llm_journal_attempts')" in s:
                online_events.append("check_regclass")
                return _FakeResult(("dalton_llm_journal_attempts",))
            if "UPDATE dalton_llm_journal j" in s:
                assert "SELECT DISTINCT a.invocation_id" in s
                assert "WHERE a.invocation_id > :last_id" in s
                assert "LIMIT :batch_size" in s
                # Diff filter skips consistent backfilled historical rows
                assert "agg.total_cash_usd > COALESCE(j.cash_usd, 0)" in s
                assert "agg.total_notional_usd > COALESCE(j.notional_usd, 0)" in s
                assert "agg.total_unknown_calls >" in s
                assert "agg.attempt_row_count > 1" in s
                assert "j.state NOT IN ('done', 'indeterminate')" in s
                online_events.append("rollup_batch")
                batch_params.append(dict(params or {}))
                return _FakeResult(batch_responses[len(batch_params) - 1])
            online_events.append(f"conn:{s.strip()}")
            return _FakeResult((None, 0))

    class _OnlineCtx:
        as_sql = False

        @contextlib.contextmanager
        def autocommit_block(self):
            online_events.append("ac_enter")
            try:
                yield
            finally:
                online_events.append("ac_exit")

    def _rec_op(sql: Any) -> None:
        s = str(sql).strip()
        if "SET LOCAL lock_timeout = '5s'" in s:
            online_events.append("ddl_lock_timeout")
        elif "DROP CONSTRAINT IF EXISTS fk_dalton_llm_journal_attempts_invocation" in s:
            online_events.append("drop_fk")
        elif "DROP TABLE IF EXISTS dalton_llm_journal_attempts CASCADE" in s:
            online_events.append("drop_table")
        else:
            online_events.append(f"op:{s}")

    monkeypatch.setattr(d044.op, "get_context", lambda: _OnlineCtx())
    monkeypatch.setattr(d044.op, "get_bind", lambda: _FakeBind())
    monkeypatch.setattr(d044.op, "execute", _rec_op)
    d044.downgrade()

    ac_enter = online_events.index("ac_enter")
    ac_exit = online_events.index("ac_exit")
    batch_idxs = [i for i, e in enumerate(online_events) if e == "rollup_batch"]
    assert len(batch_idxs) == 3
    assert all(ac_enter < i < ac_exit for i in batch_idxs)
    assert batch_params == [
        {"last_id": "", "batch_size": 1000},
        {"last_id": "lj1:1000", "batch_size": 1000},
        {"last_id": "lj1:2000", "batch_size": 1000},
    ]
    assert "conn:SET lock_timeout = '5s'" in online_events
    assert "conn:SET lock_timeout = '0'" in online_events
    # Drop DDL runs strictly AFTER exiting autocommit_block() in its own short transaction
    ddl_lt = online_events.index("ddl_lock_timeout")
    drop_fk = online_events.index("drop_fk")
    drop_tbl = online_events.index("drop_table")
    assert ac_exit < ddl_lt < drop_fk < drop_tbl

    # --- Part B: Offline mode (`as_sql=True`) emits batched DO $$ ... COMMIT loop + short drop ---
    offline_stmts: list[str] = []

    class _OfflineCtx:
        as_sql = True

        @contextlib.contextmanager
        def autocommit_block(self):
            offline_stmts.append("-- AC_ENTER")
            try:
                yield
            finally:
                offline_stmts.append("-- AC_EXIT")

    monkeypatch.setattr(d044.op, "get_context", lambda: _OfflineCtx())
    monkeypatch.setattr(d044.op, "execute", lambda sql: offline_stmts.append(str(sql)))
    d044.downgrade()

    joined_off = "\n".join(offline_stmts)
    off_enter = joined_off.index("-- AC_ENTER")
    off_do = joined_off.index("DO $$")
    off_commit = joined_off.index("COMMIT;")
    off_exit = joined_off.index("-- AC_EXIT")
    off_local_lt = joined_off.index("SET LOCAL lock_timeout = '5s'")
    off_drop_fk = joined_off.index("DROP CONSTRAINT IF EXISTS fk_dalton_llm_journal_attempts_invocation")
    off_drop_tbl = joined_off.index("DROP TABLE IF EXISTS dalton_llm_journal_attempts CASCADE")
    assert off_enter < off_do < off_commit < off_exit < off_local_lt < off_drop_fk < off_drop_tbl
    assert "agg.total_cash_usd > COALESCE(j.cash_usd, 0)" in joined_off
    assert "agg.attempt_row_count > 1" in joined_off
    assert f"LIMIT {d044._BACKFILL_BATCH_SIZE}" in joined_off

    # --- Part C: Verify diff filter semantics skip consistent historical rows and update diffs ---
    def _would_update_parent(parent: dict[str, Any], recs: list[dict[str, Any]]) -> bool:
        tot_cash = sum((Decimal(str(r["cash_usd"])) for r in recs), Decimal("0"))
        tot_notional = sum((Decimal(str(r["notional_usd"])) for r in recs), Decimal("0"))
        tot_unk = sum(int(r["unknown_usage_calls"]) for r in recs)
        has_billed = any(r["outcome"] in ("done", "indeterminate") for r in recs)
        p_usage = parent.get("usage") or {}
        if isinstance(p_usage.get("unknown_usage_calls"), int):
            p_unk = int(p_usage["unknown_usage_calls"])
        elif parent["state"] == "indeterminate" and (
            p_usage.get("usage_known") is False
            or (
                p_usage.get("usage_known") is not True
                and Decimal(str(parent.get("cash_usd") or "0")) <= 0
                and Decimal(str(parent.get("notional_usd") or "0")) <= 0
            )
        ):
            p_unk = 1
        else:
            p_unk = 0
        return (
            tot_cash > Decimal(str(parent.get("cash_usd") or "0"))
            or tot_notional > Decimal(str(parent.get("notional_usd") or "0"))
            or tot_unk > p_unk
            or len(recs) > 1
            or (
                parent["state"] not in ("done", "indeterminate")
                and (tot_cash > 0 or tot_notional > 0 or tot_unk > 0 or has_billed)
            )
        )

    # Consistent single-row backfilled done row -> SKIPPED
    assert (
        _would_update_parent(
            {"state": "done", "cash_usd": Decimal("0.05"), "notional_usd": Decimal("0"), "usage": {"input_tokens": 1000}},
            [{"outcome": "done", "cash_usd": Decimal("0.05"), "notional_usd": Decimal("0"), "unknown_usage_calls": 0}],
        )
        is False
    )
    # Consistent single-row backfilled unknown-usage indeterminate row -> SKIPPED
    assert (
        _would_update_parent(
            {"state": "indeterminate", "cash_usd": Decimal("0"), "notional_usd": Decimal("0"), "usage": None},
            [{"outcome": "indeterminate", "cash_usd": Decimal("0"), "notional_usd": Decimal("0"), "unknown_usage_calls": 1}],
        )
        is False
    )
    # Divergent rows -> UPDATED
    assert (
        _would_update_parent(
            {"state": "done", "cash_usd": Decimal("0.05"), "notional_usd": Decimal("0"), "usage": {}},
            [{"outcome": "done", "cash_usd": Decimal("0.09"), "notional_usd": Decimal("0"), "unknown_usage_calls": 0}],
        )
        is True
    )
    assert (
        _would_update_parent(
            {"state": "done", "cash_usd": Decimal("0.05"), "notional_usd": Decimal("0"), "usage": {}},
            [
                {"outcome": "indeterminate", "cash_usd": Decimal("0.02"), "notional_usd": Decimal("0"), "unknown_usage_calls": 0},
                {"outcome": "done", "cash_usd": Decimal("0.03"), "notional_usd": Decimal("0"), "unknown_usage_calls": 0},
            ],
        )
        is True
    )
    assert (
        _would_update_parent(
            {"state": "pending", "cash_usd": Decimal("0"), "notional_usd": Decimal("0"), "usage": None},
            [{"outcome": "indeterminate", "cash_usd": Decimal("0.02"), "notional_usd": Decimal("0"), "unknown_usage_calls": 0}],
        )
        is True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("err_kind", ["value_error", "validation_error"])
async def test_fallback_pre_send_client_value_or_validation_error_does_not_retry_and_allows_assist_fallback(
    monkeypatch: pytest.MonkeyPatch,
    err_kind: str,
) -> None:
    """Round 5 Item 2 (Carbon Medium):
    When `llm.ainvoke(messages)` itself raises a client-side pre-send `ValueError` or
    `pydantic.ValidationError` WITHOUT usage:
    1) `make_api_call` fallback loop MUST NOT treat it as an output parsing failure or retry
       `max_parsing_retries` times; it re-raises immediately after 1 call with 0 sleep.
    2) `dalton_complete` sees all `call_records` are `unbilled_error`, `not has_usage`,
       `unknown_usage_calls == 0`, so parent `state` is `'failed'` (NOT `'indeterminate'`)
       and `error_kind == 'pre_send'`.
    3) `assist_json` treats this as safe to fall back (`_is_safe_to_fallback(res) is True`)
       and successfully switches to its fallback model."""
    from pydantic import ValidationError
    from src.dalton.llm import assist as assist_mod

    store = _InMemoryJournalStore()
    _wire_journal_and_budget(monkeypatch, store)

    def _make_client_err() -> BaseException:
        if err_kind == "value_error":
            return ValueError("Unsupported content block type in request payload")
        try:
            _VerdictSchema.model_validate({"verdict": 123})
        except ValidationError as ve:
            return ve
        raise AssertionError("unreachable")

    # 1. `dalton_complete` with Pydantic `response_schema`: fallback `llm.ainvoke` raises pre-send error
    sleep_mock = create_autospec(_REAL_ASYNCIO_SLEEP, return_value=None)
    monkeypatch.setattr(api_call_mod.asyncio, "sleep", sleep_mock)

    chat_model = _SequencedChatModel([_make_client_err()])
    svc = _make_real_llm_service(
        monkeypatch, chat_model=chat_model, model_key="grok-4.7", max_parsing_retries=5
    )
    res = await dalton_complete(
        svc,
        user_id="u1",
        user_prompt="check client error",
        scope_ref=f"review:r5-client-{err_kind}",
        purpose="publish.review",
        request_model="grok-4.7",
        response_schema=_VerdictSchema,
    )

    assert chat_model.ainvoke_calls == 1  # MUST NOT retry 5 times!
    sleep_mock.assert_not_awaited()
    assert res.ok is False
    assert res.status == "failed"
    assert res.error_kind == "pre_send"
    assert res.cash_usd == 0.0
    assert store.rows[res.invocation_id]["state"] == "failed"
    assert store.attempts[(res.invocation_id, 1, 1)]["outcome"] == "failed"
    assert store.attempts[(res.invocation_id, 1, 1)]["unknown_usage_calls"] == 0
    assert store.attempts[(res.invocation_id, 1, 1)]["usage_known"] is True
    assert assist_mod._is_safe_to_fallback(res) is True

    # 2. End-to-end `assist_json` switches cleanly to fallback model when primary hits pre-send error
    primary_model = _SequencedChatModel([_make_client_err()])
    u_fb = {"input_tokens": 5_000, "output_tokens": 120}
    fallback_model = _SequencedChatModel(
        [_ai_msg('{"verdict": "pass", "reason": "from fallback"}', **u_fb)]
    )

    async def _resolve_by_model(*_args: Any, **kwargs: Any) -> Any:
        req_m = kwargs.get("request_model") or "grok-4.7"
        client = primary_model if req_m == "grok-4.7" else fallback_model
        return SimpleNamespace(
            llm_client=client,
            llm=SimpleNamespace(background=req_m, name=req_m),
            credential_source=CredentialSource.PLATFORM,
        )

    def _create_by_model(model_name: str, **_kwargs: Any) -> Any:
        return primary_model if model_name == "grok-4.7" else fallback_model

    monkeypatch.setattr(
        llm_svc_mod,
        "resolve_llm_config",
        create_autospec(_REAL_RESOLVE_LLM_CONFIG, side_effect=_resolve_by_model),
    )
    monkeypatch.setattr(
        llm_svc_mod,
        "create_llm",
        create_autospec(_REAL_CREATE_LLM, side_effect=_create_by_model),
    )
    multi_svc = llm_svc_mod.LLMService(
        agent_config=SimpleNamespace(llm=SimpleNamespace(background="grok-4.7", name="grok-4.7"))
    )

    assist_res = await assist_mod.assist_json(
        task=f"test_pre_send_{err_kind}",
        instructions="Return JSON verdict",
        payload={"doc": "sample"},
        schema={
            "type": "object",
            "properties": {"verdict": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["verdict", "reason"],
        },
        user_id="u1",
        model="grok-4.7",
        fallback_models=["gpt-6.1-terra"],
        llm_service=multi_svc,
        scope_ref=f"assist:r5-fallback-{err_kind}",
    )

    assert primary_model.ainvoke_calls == 1
    assert fallback_model.ainvoke_calls == 1
    assert assist_res.ok is True
    assert assist_res.model == "gpt-6.1-terra"
    assert assist_res.data == {"verdict": "pass", "reason": "from fallback"}


def test_api_call_billing_additions_all_have_dalton_hook_within_25_lines() -> None:
    """Round 5 Item 3 (Codex Medium):
    Every billing-added region in upstream `src/llms/api_call.py` (including `_track_upstream_call`
    and all billing helpers/fallback branches) MUST stay within 25 lines of a
    `DALTON-HOOK(complete-parse-failure-billing)` marker."""
    from pathlib import Path
    import inspect

    api_call_path = Path(inspect.getsourcefile(api_call_mod) or "")
    lines = api_call_path.read_text(encoding="utf-8").splitlines()
    billing_hook_lines = [
        idx + 1
        for idx, line in enumerate(lines)
        if "DALTON-HOOK(complete-parse-failure-billing)" in line
    ]
    assert billing_hook_lines, "expected DALTON-HOOK(complete-parse-failure-billing) markers in api_call.py"

    # All Dalton billing helper definitions added between StructuredOutputParseError and create_messages
    start_line = next(i + 1 for i, l in enumerate(lines) if l.startswith("class StructuredOutputParseError"))
    end_line = next(i + 1 for i, l in enumerate(lines) if l.startswith("def create_messages(")) - 1
    assert end_line > start_line

    violations: list[tuple[int, int, str]] = []
    for ln in range(start_line, end_line + 1):
        if not lines[ln - 1].strip():
            continue
        dist = min(abs(ln - h) for h in billing_hook_lines)
        if dist > 25:
            violations.append((ln, dist, lines[ln - 1].strip()[:60]))
    assert violations == [], f"Lines >25 lines from DALTON-HOOK(complete-parse-failure-billing): {violations}"

    # Also verify `_track_upstream_call` specifically (flagged in Codex review)
    track_lines, track_start = inspect.getsourcelines(api_call_mod._track_upstream_call)
    for offset in range(len(track_lines)):
        ln = track_start + offset
        if not track_lines[offset].strip():
            continue
        dist = min(abs(ln - h) for h in billing_hook_lines)
        assert dist <= 25, f"_track_upstream_call line {ln} is {dist} lines from nearest billing hook"
