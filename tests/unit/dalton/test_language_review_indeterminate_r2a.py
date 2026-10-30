"""R2a-2 + R2a fix1 (2026-10-28 HSAI×15 / Visa×5): language review must not stay
locked on an indeterminate journal row.

The language review calls ``assist_json`` with one explicit model and no
``fallback_models``, so ``own_chain`` is False and the subscription resend
allowance never applied: one ConnectError left the journal row ``indeterminate``
and every later review of the same chunk hit the same invocation_id and was
refused with "可能已经计费" — permanently. Here: the language-review task may
re-send after an indeterminate result but only ONCE per request hash (the
journal caps it by attempt), the re-send is noted in the ledger only by the
worker that really sends, a still-uncertain result becomes a clear retry-or-skip
error, and every other purpose keeps the old guard.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

SCRIPTS_DALTON = Path(__file__).resolve().parents[3] / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

from src.dalton.database import llm_journal as journal_mod  # noqa: E402
from src.dalton.llm import assist as assist_mod  # noqa: E402
from src.dalton.llm import complete as complete_mod  # noqa: E402
from src.dalton.llm.assist import AssistResult, assist_json  # noqa: E402
from src.dalton.llm.complete import DaltonCompleteResult, dalton_complete  # noqa: E402
from src.dalton.publish import language as language_mod  # noqa: E402
from src.dalton.publish.language import run_check_language  # noqa: E402
from ptc_agent.agent.backends.sandbox import SandboxBackend  # noqa: E402
from ptc_agent.core.paths import DEFAULT_LAYOUT, resolve_agent_path  # noqa: E402

SCHEMA = {"type": "object", "properties": {"issues": {"type": "array"}}, "required": ["issues"]}
LANG_SUB = "lang-review-oauth"  # "-oauth" suffix: subscription-billed
SUB_FB = "sub-fallback-oauth"
CASH = "cash-primary"

WORKSPACE_DIR = DEFAULT_LAYOUT.root
DRAFT = "drafts/2026-10-28-call-HSAI.md"
DRAFT_TEXT = "# Conviction Call\n\n值得注意的是，本轮实现了对回测框架的调整。\n"


def _ok(model: str) -> DaltonCompleteResult:
    return DaltonCompleteResult(
        ok=True, status="done", data={"issues": []}, text='{"issues": []}', model=model
    )


def _indeterminate(model: str) -> DaltonCompleteResult:
    return DaltonCompleteResult(
        ok=False,
        status="indeterminate",
        error="上次调用在返回前中断，供应商可能已经计费。确认后可重新发起（会再计一次费用）。",
        error_kind="indeterminate",
        model=model,
    )


def _patch_complete(monkeypatch: pytest.MonkeyPatch, behaviour) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _complete(**kwargs: Any) -> DaltonCompleteResult:
        calls.append(kwargs)
        return behaviour(str(kwargs.get("request_model")))

    monkeypatch.setattr(
        assist_mod, "dalton_complete", create_autospec(dalton_complete, side_effect=_complete)
    )
    return calls


# ----------------------------------------------- assist layer: who may resend


@pytest.mark.asyncio
async def test_language_review_subscription_model_is_resent_after_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One ConnectError used to lock the review chunk's journal row forever; the
    language-review task now opts into the same subscription resend allowance the
    own-chain rule gives the audit chain."""
    calls = _patch_complete(monkeypatch, _ok)
    res = await assist_json(
        "language.review", "x", {"excerpt": "chunk"}, SCHEMA, user_id="u1", model=LANG_SUB
    )
    assert res.ok and res.model == LANG_SUB
    assert [c["request_model"] for c in calls] == [LANG_SUB]
    assert calls[0]["resend_indeterminate"] is True


@pytest.mark.asyncio
async def test_language_review_resends_at_most_once_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The re-send allowance is bounded: one send per assist_json call even when
    the result is indeterminate again — no retry loop."""
    calls = _patch_complete(monkeypatch, _indeterminate)
    res = await assist_json(
        "language.review", "x", {"excerpt": "chunk"}, SCHEMA, user_id="u1", model=LANG_SUB
    )
    assert not res.ok and res.status == "indeterminate"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_audit_and_other_purposes_keep_the_double_charge_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single-model audit call (no own chain) and any other purpose stay on the
    old rule: indeterminate rows are NOT re-sent."""
    calls = _patch_complete(monkeypatch, _indeterminate)
    await assist_json("audit.judge", "x", {}, SCHEMA, user_id="u1", model=LANG_SUB)
    await assist_json("conversation_extract", "x", {}, SCHEMA, user_id="u1", model=LANG_SUB)
    assert [c["resend_indeterminate"] for c in calls] == [False, False]


@pytest.mark.asyncio
async def test_cash_language_model_keeps_the_double_charge_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_complete(monkeypatch, _indeterminate)
    await assist_json("language.review", "x", {}, SCHEMA, user_id="u1", model=CASH)
    assert calls[0]["resend_indeterminate"] is False


# --------------------------------------- dalton_complete layer: the ledger note


def _patch_journal(monkeypatch: pytest.MonkeyPatch, *, inspect_action: str, inspect_row: dict):
    events: list[dict[str, Any]] = []

    async def _inspect(*args: Any, **kwargs: Any):
        return inspect_action, inspect_row

    async def _claim(**kwargs: Any):
        return "claimed", {"attempt": 2}

    async def _done(*args: Any, **kwargs: Any):
        return {"state": "done"}

    async def _event(provider: str, kind: str, **kwargs: Any) -> int:
        events.append({"provider": provider, "kind": kind, **kwargs})
        return 1

    monkeypatch.setattr(
        complete_mod,
        "inspect_journal_state",
        create_autospec(complete_mod.inspect_journal_state, side_effect=_inspect),
    )
    monkeypatch.setattr(
        complete_mod,
        "claim_journal_invocation",
        create_autospec(complete_mod.claim_journal_invocation, side_effect=_claim),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_done",
        create_autospec(complete_mod.complete_journal_done, side_effect=_done),
    )
    monkeypatch.setattr(
        complete_mod,
        "record_provider_event",
        create_autospec(complete_mod.record_provider_event, side_effect=_event),
    )
    return events


@pytest.mark.asyncio
async def test_resend_after_indeterminate_notes_it_in_the_ledger_then_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With resend_indeterminate=True an indeterminate journal row no longer
    refuses the call: the re-send happens (once) and a "因上次状态不确定而重发"
    note is recorded in the ledger before it."""
    from src.server.services.llm.service import LLMService

    service = create_autospec(LLMService, instance=True)
    service.complete.return_value = ({"issues": []}, {})
    events = _patch_journal(
        monkeypatch,
        inspect_action="proceed",
        inspect_row={"state": "indeterminate", "attempt": 1, "error_kind": "timeout"},
    )

    res = await dalton_complete(
        service,
        user_id="u1",
        user_prompt="p",
        request_model=LANG_SUB,
        resend_indeterminate=True,
    )
    assert res.ok and res.status == "done"
    service.complete.assert_awaited_once()
    resend_notes = [e for e in events if e["kind"] == "journal_resend_indeterminate"]
    assert len(resend_notes) == 1
    assert resend_notes[0]["detail"]["note"] == "因上次状态不确定而重发"
    assert resend_notes[0]["detail"]["previous_state"] == "indeterminate"
    assert resend_notes[0]["model_key"] == LANG_SUB


@pytest.mark.asyncio
async def test_resend_that_dies_before_sending_leaves_no_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim succeeds but the deadline runs out between the claim and the
    send: nothing is sent, so no "因上次状态不确定而重发" note may be recorded."""
    import time as real_time

    from src.server.services.llm.service import LLMService

    service = create_autospec(LLMService, instance=True)
    events = _patch_journal(
        monkeypatch,
        inspect_action="proceed",
        inspect_row={"state": "indeterminate", "attempt": 1, "error_kind": "timeout"},
    )

    clock = {"now": 1000.0}

    class _Clock:
        @staticmethod
        def monotonic() -> float:
            return clock["now"]

        def __getattr__(self, name: str) -> Any:
            return getattr(real_time, name)

    async def _claim_then_time_runs_out(**kwargs: Any):
        clock["now"] = 2000.0  # the deadline (1500) passes right after the claim
        return "claimed", {"attempt": 2}

    async def _failed(*args: Any, **kwargs: Any):
        return {"state": "failed"}

    from src.dalton.database import llm_journal as journal_impl

    monkeypatch.setattr(complete_mod, "time", _Clock())
    monkeypatch.setattr(
        complete_mod,
        "claim_journal_invocation",
        create_autospec(journal_impl.claim_journal_invocation, side_effect=_claim_then_time_runs_out),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_failed",
        create_autospec(journal_impl.complete_journal_failed, side_effect=_failed),
    )

    res = await dalton_complete(
        service,
        user_id="u1",
        user_prompt="p",
        request_model=LANG_SUB,
        resend_indeterminate=True,
        deadline=1500.0,
    )
    assert not res.ok
    service.complete.assert_not_awaited()
    assert [e for e in events if e["kind"] == "journal_resend_indeterminate"] == []


@pytest.mark.asyncio
async def test_resend_note_is_written_after_the_request_goes_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Writing the ledger note must not sit between the deadline check and the
    send: the request goes out first, the note follows."""
    from src.server.services.llm.service import LLMService

    order: list[str] = []
    service = create_autospec(LLMService, instance=True)

    async def _send(**kwargs: Any):
        order.append("send")
        return ({"issues": []}, {})

    service.complete.side_effect = _send
    events = _patch_journal(
        monkeypatch,
        inspect_action="proceed",
        inspect_row={"state": "indeterminate", "attempt": 1, "error_kind": "timeout"},
    )
    real_event = complete_mod.record_provider_event

    async def _event_after(*args: Any, **kwargs: Any):
        order.append("note")
        return await real_event(*args, **kwargs)

    monkeypatch.setattr(complete_mod, "record_provider_event", _event_after)

    res = await dalton_complete(
        service, user_id="u1", user_prompt="p", request_model=LANG_SUB, resend_indeterminate=True
    )
    assert res.ok
    assert order == ["send", "note"]
    assert len([e for e in events if e["kind"] == "journal_resend_indeterminate"]) == 1


@pytest.mark.asyncio
async def test_a_stuck_resend_note_does_not_hold_up_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ledger write blocked on a lock is cut off quickly: the call still
    returns its own result."""
    from src.server.services.llm.service import LLMService

    service = create_autospec(LLMService, instance=True)
    service.complete.return_value = ({"issues": []}, {})
    _patch_journal(
        monkeypatch,
        inspect_action="proceed",
        inspect_row={"state": "indeterminate", "attempt": 1, "error_kind": "timeout"},
    )

    async def _stuck(*args: Any, **kwargs: Any) -> int:
        await asyncio.sleep(3600)
        return 1

    monkeypatch.setattr(complete_mod, "record_provider_event", _stuck)
    monkeypatch.setattr(complete_mod, "_RESEND_NOTE_TIMEOUT_S", 0.05, raising=False)

    res = await asyncio.wait_for(
        dalton_complete(
            service, user_id="u1", user_prompt="p", request_model=LANG_SUB, resend_indeterminate=True
        ),
        timeout=2,
    )
    assert res.ok and res.status == "done"


@pytest.mark.asyncio
async def test_refused_indeterminate_sends_nothing_and_records_no_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.services.llm.service import LLMService

    service = create_autospec(LLMService, instance=True)
    events = _patch_journal(
        monkeypatch,
        inspect_action="indeterminate",
        inspect_row={"state": "indeterminate", "attempt": 1},
    )

    res = await dalton_complete(
        service, user_id="u1", user_prompt="p", request_model=CASH, resend_indeterminate=False
    )
    assert not res.ok and res.status == "indeterminate"
    service.complete.assert_not_called()
    assert events == []


# ------------------------------------------- check_language layer: clear error


def _backend(files: dict[str, str]) -> Any:
    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = WORKSPACE_DIR
    backend.computer_root = WORKSPACE_DIR
    backend.normalize_path.side_effect = lambda p: resolve_agent_path(
        p,
        workspace=WORKSPACE_DIR,
        root=WORKSPACE_DIR,
        allowed=DEFAULT_LAYOUT.allowed_directories,
    )

    async def _aread(path: str) -> str | None:
        return files.get(path)

    async def _awrite(path: str, text: str) -> bool:
        files[path] = text
        return True

    backend.aread_text.side_effect = _aread
    backend.awrite_text.side_effect = _awrite
    return backend


@pytest.fixture
def _lang_config(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _verify(workspace_id: str, user_id: str) -> dict[str, Any]:
        return {}

    async def _model(user_id: str | None = None) -> str:
        return LANG_SUB

    async def _timeout(user_id: str | None = None) -> float:
        return 60.0

    monkeypatch.setattr(language_mod, "verify_workspace_access", _verify)
    monkeypatch.setattr(language_mod, "resolve_dalton_language_model", _model)
    monkeypatch.setattr(language_mod, "resolve_dalton_audit_timeout_s", _timeout)


@pytest.mark.asyncio
async def test_still_indeterminate_review_returns_clear_error_and_next_call_runs(
    monkeypatch: pytest.MonkeyPatch, _lang_config: None
) -> None:
    """The re-send itself ends uncertain: the round fails with the retry-or-skip
    wording (never "可能已经计费"), no sidecar is written, and the next
    check_language call goes through to the model instead of being refused."""
    files = {DRAFT: DRAFT_TEXT}
    results: list[AssistResult] = [
        AssistResult(
            ok=False,
            data=None,
            status="indeterminate",
            error="上次调用在返回前中断，供应商可能已经计费。确认后可重新发起（会再计一次费用）。",
            error_kind="indeterminate",
            model=LANG_SUB,
        )
    ]

    async def _assist(*args: Any, **kwargs: Any) -> AssistResult:
        return results.pop(0) if results else AssistResult(
            ok=True, data={"issues": []}, model=LANG_SUB
        )

    stub = create_autospec(assist_json, side_effect=_assist)
    monkeypatch.setattr(language_mod, "assist_json", stub)

    first = await run_check_language(
        workspace_id="ws-1", draft_path=DRAFT, round=1, backend=_backend(files), user_id="user-1"
    )
    assert first["ok"] is False
    assert "语言审查服务暂时不可用" in first["message"]
    assert "可稍后重试或跳过" in first["message"]
    assert "可能已经计费" not in first["message"]
    assert f"{DRAFT}.language.json" not in files
    assert f"{DRAFT}.language.md" not in files

    # The follow-up call is not locked out: it reaches the model and completes.
    second = await run_check_language(
        workspace_id="ws-1", draft_path=DRAFT, round=1, backend=_backend(files), user_id="user-1"
    )
    assert second["ok"] is True, second["message"]
    assert stub.await_count == 2
    assert f"{DRAFT}.language.json" in files


@pytest.mark.asyncio
async def test_language_review_recovers_end_to_end_after_indeterminate(
    monkeypatch: pytest.MonkeyPatch, _lang_config: None
) -> None:
    """Through the real assist_json (only the model call stubbed): the chunk whose
    journal row went indeterminate is re-sent on the next check_language call and
    the round completes."""
    calls: list[dict[str, Any]] = []
    send_no = [0]

    async def _complete(**kwargs: Any) -> DaltonCompleteResult:
        calls.append(kwargs)
        send_no[0] += 1
        # First send of the chunk died uncertain; the re-send succeeds.
        return _indeterminate(str(kwargs.get("request_model"))) if send_no[0] == 1 else _ok(
            str(kwargs.get("request_model"))
        )

    monkeypatch.setattr(
        assist_mod, "dalton_complete", create_autospec(dalton_complete, side_effect=_complete)
    )

    files = {DRAFT: DRAFT_TEXT}
    first = await run_check_language(
        workspace_id="ws-1", draft_path=DRAFT, round=1, backend=_backend(files), user_id="user-1"
    )
    assert first["ok"] is False

    second = await run_check_language(
        workspace_id="ws-1", draft_path=DRAFT, round=1, backend=_backend(files), user_id="user-1"
    )
    assert second["ok"] is True, second["message"]
    # Both sends were allowed to repeat the same request (resend_indeterminate),
    # and there was exactly one send per check_language call — no retry loop.
    assert [c["resend_indeterminate"] for c in calls] == [True, True]
    doc_json = f"{DRAFT}.language.json"
    assert doc_json in files and len(json.loads(files[doc_json])["rounds"]) == 1


# ------------------- R2a fix1: cumulative resend cap + ledger note on real sends


class _FakeJournal:
    """An in-memory `dalton_llm_journal` single-row store; a cursor that
    dispatches on the SQL text exactly the way the real table behaves, so the
    REAL `inspect_journal_state` / `claim_journal_invocation` /
    `complete_journal_failed` run against it."""

    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row

    def conn(self) -> "_FakeJournalConn":
        return _FakeJournalConn(self)


class _FakeJournalCursor:
    def __init__(self, journal: _FakeJournal) -> None:
        self._j = journal
        self._result: dict[str, Any] | None = None

    async def __aenter__(self) -> "_FakeJournalCursor":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, sql: str, params: tuple = ()) -> None:
        j = self._j
        text = " ".join(sql.split()).upper()
        self._result = None
        if text.startswith("SELECT * FROM DALTON_LLM_JOURNAL WHERE INVOCATION_ID"):
            self._result = dict(j.row) if j.row is not None else None
        elif text.startswith("INSERT INTO DALTON_LLM_JOURNAL"):
            if j.row is None:
                j.row = {
                    "invocation_id": params[0],
                    "user_id": params[1],
                    "scope_ref": params[2],
                    "purpose": params[3],
                    "model_key": params[4],
                    "provider": params[5],
                    "billing_type": params[6],
                    "request_hash": params[7],
                    "state": "pending",
                    "attempt": 1,
                    "claimed_at": datetime.now(timezone.utc),
                }
                self._result = dict(j.row)
        elif "SET STATE = 'INDETERMINATE', ERROR_KIND = COALESCE" in text:
            if j.row is not None:
                j.row["state"] = "indeterminate"
                j.row.setdefault("error_kind", "crash")
                self._result = dict(j.row)
        elif "ATTEMPT = ATTEMPT + 1" in text:
            if j.row is not None:
                j.row.update(
                    state="pending",
                    attempt=int(j.row.get("attempt") or 1) + 1,
                    claimed_at=datetime.now(timezone.utc),
                    completed_at=None,
                    error_kind=None,
                    error_text=None,
                )
                self._result = dict(j.row)
        elif "SET STATE = 'DONE'" in text:
            if j.row is not None:
                j.row.update(state="done", completed_at=datetime.now(timezone.utc))
                self._result = dict(j.row)
        elif text.startswith("UPDATE DALTON_LLM_JOURNAL") and "SET STATE = %S" in text:
            # complete_journal_failed: (state, error_kind, error_text, cash, notional, inv)
            if j.row is not None:
                j.row.update(state=params[0], error_kind=params[1], error_text=params[2])
                self._result = dict(j.row)

    async def fetchone(self) -> dict[str, Any] | None:
        return self._result


class _FakeJournalConn:
    def __init__(self, journal: _FakeJournal) -> None:
        self._journal = journal

    def cursor(self, **kwargs: Any) -> _FakeJournalCursor:
        return _FakeJournalCursor(self._journal)


def _wire_real_journal(
    monkeypatch: pytest.MonkeyPatch, journal: _FakeJournal
) -> list[dict[str, Any]]:
    """Point complete.py's journal calls at the REAL journal functions, which
    run against the fake in-memory table; capture provider events."""
    events: list[dict[str, Any]] = []
    conn = journal.conn()

    async def _inspect(invocation_id: str, **kwargs: Any):
        return await journal_mod.inspect_journal_state(
            invocation_id, conn=conn, **kwargs
        )

    async def _claim(**kwargs: Any):
        return await journal_mod.claim_journal_invocation(conn=conn, **kwargs)

    async def _failed(invocation_id: str, **kwargs: Any):
        return await journal_mod.complete_journal_failed(invocation_id, conn=conn, **kwargs)

    async def _done(invocation_id: str, **kwargs: Any):
        return await journal_mod.complete_journal_done(invocation_id, conn=conn, **kwargs)

    async def _event(provider: str, kind: str, **kwargs: Any) -> int:
        events.append({"provider": provider, "kind": kind, **kwargs})
        return 1

    monkeypatch.setattr(
        complete_mod,
        "inspect_journal_state",
        create_autospec(journal_mod.inspect_journal_state, side_effect=_inspect),
    )
    monkeypatch.setattr(
        complete_mod,
        "claim_journal_invocation",
        create_autospec(journal_mod.claim_journal_invocation, side_effect=_claim),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_failed",
        create_autospec(journal_mod.complete_journal_failed, side_effect=_failed),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_done",
        create_autospec(journal_mod.complete_journal_done, side_effect=_done),
    )
    monkeypatch.setattr(
        complete_mod,
        "record_provider_event",
        create_autospec(complete_mod.record_provider_event, side_effect=_event),
    )
    return events


@pytest.mark.asyncio
async def test_resend_is_capped_after_one_resend_per_request_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2a fix1-1: the same request hash may be re-sent ONCE after the original
    send went indeterminate. The third look at the same invocation (and every
    later one) sends nothing and returns the clear indeterminate refusal, which
    check_language maps to 语言审查服务暂时不可用."""
    from src.server.services.llm.service import LLMService
    from src.dalton.llm.complete import compute_invocation_id_and_hash

    service = create_autospec(LLMService, instance=True)
    service.complete.side_effect = TimeoutError("upstream timeout")
    # Seed the row with the exact request_hash dalton_complete computes, so the
    # fake table is the same invocation it will look up.
    _, request_hash = compute_invocation_id_and_hash(
        scope_ref="assist:u1:language.review:same",
        purpose="assist.language.review",
        model_key=LANG_SUB,
        system_prompt=None,
        user_prompt="same-prompt",
        response_schema=None,
        reasoning_effort=None,
        slot="background",
    )
    # The original send already died uncertain: attempt 1, state indeterminate.
    journal = _FakeJournal(
        {
            "invocation_id": "inv-lang-1",
            "request_hash": request_hash,
            "state": "indeterminate",
            "attempt": 1,
            "error_kind": "timeout",
            "claimed_at": datetime.now(timezone.utc),
        }
    )
    events = _wire_real_journal(monkeypatch, journal)

    results = [
        await dalton_complete(
            service,
            user_id="u1",
            user_prompt="same-prompt",
            scope_ref="assist:u1:language.review:same",
            purpose="assist.language.review",
            request_model=LANG_SUB,
            resend_indeterminate=True,
            resend_max_attempts=2,
        )
        for _ in range(3)
    ]
    # Round 1 is the one allowed re-send: it really went out (and timed out
    # uncertain again, so the journal row is indeterminate attempt 2).
    assert results[0].status == "indeterminate"
    assert journal.row["state"] == "indeterminate" and journal.row["attempt"] == 2
    # Rounds 2 and 3 refuse without sending and without another ledger note.
    assert results[1].status == "indeterminate" and not results[1].ok
    assert results[2].status == "indeterminate" and not results[2].ok
    assert "供应商可能已经计费" in (results[1].error or "")
    assert service.complete.await_count == 1
    resend_notes = [e for e in events if e["kind"] == "journal_resend_indeterminate"]
    assert len(resend_notes) == 1


@pytest.mark.asyncio
async def test_two_concurrent_resends_record_exactly_one_ledger_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2a fix1-4: only the worker that wins the claim (and therefore really
    sends) records the re-send note; the worker told `in_flight` records
    nothing, so concurrent callers append exactly one event."""
    from src.server.services.llm.service import LLMService

    service = create_autospec(LLMService, instance=True)
    service.complete.return_value = ({"issues": []}, {})
    indet_row = {"state": "indeterminate", "attempt": 1, "error_kind": "timeout"}
    claim_answers = [("claimed", {"attempt": 2}), ("in_flight", {"attempt": 2})]
    events: list[dict[str, Any]] = []

    async def _inspect(invocation_id: str, **kwargs: Any):
        return "proceed", dict(indet_row)

    async def _claim(**kwargs: Any):
        return claim_answers.pop(0)

    async def _event(provider: str, kind: str, **kwargs: Any) -> int:
        events.append({"provider": provider, "kind": kind, **kwargs})
        return 1

    monkeypatch.setattr(
        complete_mod,
        "inspect_journal_state",
        create_autospec(journal_mod.inspect_journal_state, side_effect=_inspect),
    )
    monkeypatch.setattr(
        complete_mod,
        "claim_journal_invocation",
        create_autospec(journal_mod.claim_journal_invocation, side_effect=_claim),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_done",
        create_autospec(journal_mod.complete_journal_done, return_value={"state": "done"}),
    )
    monkeypatch.setattr(
        complete_mod,
        "record_provider_event",
        create_autospec(complete_mod.record_provider_event, side_effect=_event),
    )

    first, second = await asyncio.gather(
        dalton_complete(
            service,
            user_id="u1",
            user_prompt="p",
            scope_ref="assist:u1:language.review:same",
            purpose="assist.language.review",
            request_model=LANG_SUB,
            resend_indeterminate=True,
        ),
        dalton_complete(
            service,
            user_id="u1",
            user_prompt="p",
            scope_ref="assist:u1:language.review:same",
            purpose="assist.language.review",
            request_model=LANG_SUB,
            resend_indeterminate=True,
        ),
    )
    assert first.ok and second.status == "in_flight"
    assert service.complete.await_count == 1
    assert [e["kind"] for e in events if e["kind"] == "journal_resend_indeterminate"] == [
        "journal_resend_indeterminate"
    ]


@pytest.mark.asyncio
async def test_replay_worker_records_no_resend_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker whose claim turns out to be a cached `done` result replays it
    without sending — and records no re-send note."""
    from src.server.services.llm.service import LLMService

    service = create_autospec(LLMService, instance=True)
    events: list[dict[str, Any]] = []

    async def _inspect(invocation_id: str, **kwargs: Any):
        return "proceed", {"state": "indeterminate", "attempt": 1}

    async def _claim(**kwargs: Any):
        return (
            "done",
            {
                "state": "done",
                "attempt": 1,
                "model_key": LANG_SUB,
                "response": {"kind": "json", "data": {"issues": []}, "text": "{}"},
                "usage": {},
                "notional_usd": 0,
            },
        )

    async def _event(provider: str, kind: str, **kwargs: Any) -> int:
        events.append({"provider": provider, "kind": kind, **kwargs})
        return 1

    monkeypatch.setattr(
        complete_mod,
        "inspect_journal_state",
        create_autospec(journal_mod.inspect_journal_state, side_effect=_inspect),
    )
    monkeypatch.setattr(
        complete_mod,
        "claim_journal_invocation",
        create_autospec(journal_mod.claim_journal_invocation, side_effect=_claim),
    )
    monkeypatch.setattr(
        complete_mod,
        "record_journal_replay",
        create_autospec(complete_mod.record_journal_replay),
    )
    monkeypatch.setattr(
        complete_mod,
        "record_provider_event",
        create_autospec(complete_mod.record_provider_event, side_effect=_event),
    )

    res = await dalton_complete(
        service,
        user_id="u1",
        user_prompt="p",
        scope_ref="assist:u1:language.review:same",
        purpose="assist.language.review",
        request_model=LANG_SUB,
        resend_indeterminate=True,
    )
    assert res.ok and res.replayed
    service.complete.assert_not_called()
    assert events == []


@pytest.mark.asyncio
async def test_assist_passes_the_cumulative_cap_only_for_the_allowlisted_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """assist_json threads the journal cap (original + one re-send) for the
    language-review task only: an explicit opt-in and the audit chain keep
    their own uncapped policy."""
    calls = _patch_complete(monkeypatch, _indeterminate)
    await assist_json("language.review", "x", {}, SCHEMA, user_id="u1", model=LANG_SUB)
    await assist_json("audit.judge", "x", {}, SCHEMA, user_id="u1", model=LANG_SUB)
    await assist_json(
        "audit.judge", "x", {}, SCHEMA, user_id="u1", model=LANG_SUB, fallback_models=[SUB_FB]
    )
    await assist_json(
        "language.review", "x", {}, SCHEMA, user_id="u1", model=LANG_SUB,
        resend_indeterminate=True,
    )
    assert [c["resend_max_attempts"] for c in calls] == [2, None, None, None]
