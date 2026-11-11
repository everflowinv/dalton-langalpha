"""Offline prepare and paged read for one AI-finance batch.

Collectors and AlphaEngine calls are local doubles. No test starts a
service or reads a production credential.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import inspect
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

import pytest

from scripts.dalton.ai_finance.prepare import prepare_batch
from scripts.dalton import ai_finance_access as access
from src.dalton.settings import get_ai_finance_owner_user_id

HKT = ZoneInfo("Asia/Hong_Kong")
OWNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "99999999-9999-4999-8999-999999999999"
EXECUTION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
EXECUTION_2 = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
FIRST = datetime(2026, 11, 11, 20, 0, tzinfo=HKT)
LATER = datetime(2026, 11, 11, 22, 30, tzinfo=HKT)
DATE = "2026-11-11"
SECRET = "SECRET-RESEARCH-BODY"


def test_script_imports_from_neutral_cwd_without_pythonpath(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[3] / "scripts" / "dalton" / "ai_finance_access.py"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-B", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "ModuleNotFoundError" not in completed.stderr
    assert "Traceback" not in completed.stderr


def test_prepare_batch_stays_keyword_only() -> None:
    signature = inspect.signature(prepare_batch)
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )
    assert "period" not in signature.parameters


def test_owner_getters_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DALTON_AI_FINANCE_OWNER_USER_ID", raising=False)
    assert get_ai_finance_owner_user_id() is None
    assert access.configured_owner_user_id() is None
    monkeypatch.setenv("DALTON_AI_FINANCE_OWNER_USER_ID", OWNER.upper())
    assert get_ai_finance_owner_user_id() == OWNER
    assert access.configured_owner_user_id() == OWNER
    monkeypatch.setenv("DALTON_AI_FINANCE_OWNER_USER_ID", "not-a-uuid-value-of-length-36")
    assert get_ai_finance_owner_user_id() is None
    assert access.configured_owner_user_id() is None


def test_prepare_request_canonicalizes_and_rejects_unknown_fields() -> None:
    body = _prepare_fields(EXECUTION.upper(), FIRST, FIRST)
    parsed = access.parse_prepare_request(body)
    assert parsed["ok"] is True
    assert parsed["execution_id"] == EXECUTION
    assert parsed["owner_user_id"] == OWNER
    body["token"] = SECRET
    rejected = access.parse_prepare_request(body)
    assert rejected["ok"] is False
    assert rejected["code"] == "unknown_field"
    assert SECRET not in json.dumps(rejected)


@pytest.mark.parametrize("operation", ["auth_status", "search_library", "get_document"])
@pytest.mark.parametrize("error_flag", [None, False, True, "false"])
def test_optional_inner_error_flag(operation: str, error_flag: object) -> None:
    structured = {"authenticated": True, "status": "ready", "text": "page"}
    data: dict[str, Any] = {"structuredContent": structured}
    if error_flag is not None:
        data["isError"] = error_flag
    envelope = _envelope({"ok": True, "data": data})
    if error_flag is None or error_flag is False:
        assert access.unwrap_research_envelope(operation, envelope) == structured
    else:
        with pytest.raises(access.AeCallError, match="research_failed"):
            access.unwrap_research_envelope(operation, envelope)


def test_research_envelope_and_single_submit() -> None:
    auth = access.unwrap_research_envelope("auth_status", _envelope(_business({
        "authenticated": True,
        "status": "ready",
    })))
    assert auth == {"authenticated": True, "status": "ready"}
    search = access.unwrap_research_envelope("search_library", _envelope(_business({
        "results": [{"doc_id": "doc1", "title": "AI"}],
        "cursor": "cursor-1",
        "has_more": True,
        "total_upstream": 1,
        "filters": {},
    })))
    assert search["has_more"] is True
    assert search["results"][0]["doc_id"] == "doc1"
    document = access.unwrap_research_envelope("get_document", _envelope(_business({
        "text": "page",
        "next_offset": None,
        "complete": True,
        "content_sha256": "ab" * 32,
        "content_chars": 4,
    })))
    assert document["next_offset"] is None
    with pytest.raises(access.AeCallError) as failed:
        access.unwrap_research_envelope(
            "get_document",
            {"result": {"isError": True, "content": [{"type": "text", "text": SECRET}]}},
        )
    assert failed.value.code == "research_failed"
    assert SECRET not in str(failed.value)
    with pytest.raises(access.AeCallError) as not_ready:
        access.unwrap_research_envelope("auth_status", _envelope(_business({
            "authenticated": False,
            "status": "login",
        })))
    assert not_ready.value.code == "auth_not_ready"
    with pytest.raises(access.AeCallError) as rejected:
        access.research_params("shell", {})
    assert rejected.value.code == "operation_rejected"
    params = access.research_params("auth_status", {})
    assert params["arguments"]["route"] == "alphaengine"
    assert params["arguments"]["operation"] == "auth_status"
    assert "url" not in json.dumps(params)

    future = create_autospec(concurrent.futures.Future, instance=True)
    future.result.side_effect = TimeoutError()
    calls: list[dict[str, Any]] = []

    def submit(call: dict[str, Any]) -> concurrent.futures.Future:
        calls.append(call)
        return future

    with pytest.raises(access.AeCallError) as timed_out:
        access.run_research_call(
            submit=submit,
            tool="get_document",
            arguments={"doc_id": "doc1", "offset": 0, "max_chars": 100},
            timeout=1,
        )
    assert timed_out.value.code == "alphaengine_timeout"
    assert len(calls) == 1
    assert calls[0]["arguments"]["operation"] == "get_document"
    future.cancel.assert_called_once_with()
    future.result.side_effect = None
    future.result.return_value = _envelope(_business({"authenticated": True, "status": "ready"}))
    ready = access.run_research_call(
        submit=submit,
        tool="auth_status",
        arguments={},
        timeout=1,
    )
    assert ready["authenticated"] is True
    assert len(calls) == 2


def test_clock_disagreement_does_not_prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _owner(monkeypatch)
    calls: list[datetime] = []
    real = access.prepare_batch

    def _record(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["started_at"])
        return real(**kwargs)

    monkeypatch.setattr(access, "prepare_batch", create_autospec(real, side_effect=_record))
    result = access.prepare_ai_finance_batch(
        state_root=tmp_path,
        scheduled_at=datetime(2026, 11, 11, 23, 30, tzinfo=HKT),
        started_at=datetime(2026, 11, 12, 0, 30, tzinfo=HKT),
        execution_id=EXECUTION,
        owner_user_id=OWNER,
        transport=_unused_transport,
    )
    assert result["ok"] is False
    assert result["code"] == "clock_disagreement"
    assert calls == []
    assert not (tmp_path / "batches").exists()


def test_same_date_recovery_keeps_the_first_instant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _owner(monkeypatch)
    passed: list[datetime] = []
    documents: list[str] = []
    real = access.prepare_batch

    def _record(**kwargs: Any) -> dict[str, Any]:
        passed.append(kwargs["started_at"])
        return real(**kwargs)

    monkeypatch.setattr(access, "prepare_batch", create_autospec(real, side_effect=_record))

    def earnings(request: dict[str, Any]) -> dict[str, Any]:
        documents.append("get_document")
        request["transport"]("get_document", {"doc_id": "doc1"})
        return _earnings(request, body="AI body")

    first = _prepare(tmp_path, FIRST, EXECUTION, earnings)
    clock_bytes = (tmp_path / "batch_clocks" / f"{DATE}.json").read_bytes()
    assert first["ok"] is True
    assert first["recovered"] is False
    assert documents == ["get_document"]
    second = _prepare(tmp_path, LATER, EXECUTION_2, earnings)
    assert second["ok"] is True
    assert second["recovered"] is True
    assert "earnings" in second["reused"]
    assert documents == ["get_document"]
    assert (tmp_path / "batch_clocks" / f"{DATE}.json").read_bytes() == clock_bytes
    assert datetime.fromisoformat(second["started_at"]).astimezone(timezone.utc) == FIRST.astimezone(timezone.utc)
    assert [item.astimezone(timezone.utc) for item in passed] == [FIRST.astimezone(timezone.utc), FIRST.astimezone(timezone.utc)]
    saved = json.loads(clock_bytes)
    assert saved["first_execution_id"] == EXECUTION
    assert SECRET not in json.dumps(second)


def test_corrupt_clock_and_other_owner_are_not_rewritten(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _owner(monkeypatch)
    first = _prepare(tmp_path, FIRST, EXECUTION, lambda request: _earnings(request))
    assert first["ok"] is True
    clock = tmp_path / "batch_clocks" / f"{DATE}.json"
    original = clock.read_bytes()
    monkeypatch.setenv("DALTON_AI_FINANCE_OWNER_USER_ID", OTHER)
    mismatch = access.prepare_ai_finance_batch(
        state_root=tmp_path,
        scheduled_at=LATER,
        started_at=LATER,
        execution_id=EXECUTION_2,
        owner_user_id=OTHER,
        transport=_unused_transport,
        github_collector=lambda _request: {"version": 3, "candidates": []},
        earnings_collector=lambda request: _earnings(request),
    )
    assert mismatch["ok"] is False
    assert mismatch["code"] == "owner_mismatch"
    assert clock.read_bytes() == original
    clock.write_bytes(b"{not-json " + SECRET.encode())
    corrupt = access.prepare_ai_finance_batch(
        state_root=tmp_path,
        scheduled_at=FIRST,
        started_at=FIRST,
        execution_id=EXECUTION,
        owner_user_id=OTHER,
        transport=_unused_transport,
        github_collector=lambda _request: {"version": 3, "candidates": []},
        earnings_collector=lambda request: _earnings(request),
    )
    assert corrupt["ok"] is False
    assert corrupt["code"] == "clock_inconsistent"
    assert SECRET not in json.dumps(corrupt)
    assert clock.read_bytes().endswith(SECRET.encode())


def test_failure_is_not_reported_as_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _owner(monkeypatch)

    def earnings(_request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(SECRET + " /tmp/research")

    result = _prepare(tmp_path, FIRST, EXECUTION, earnings)
    blob = json.dumps(result)
    assert result["ok"] is False
    assert "earnings_failed:RuntimeError" in result["errors"]
    assert SECRET not in blob
    assert "/tmp/research" not in blob


def test_read_pages_full_text_and_rejects_damage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _owner(monkeypatch)
    marker = "G" * 50000
    prepared = _prepare(
        tmp_path,
        FIRST,
        EXECUTION,
        lambda request: _earnings(request, body="AI body"),
        github=lambda _request: {"version": 3, "marker": marker, "candidates": []},
    )
    assert prepared["ok"] is True
    assert prepared["candidate_doc_ids"] == ["doc1"]
    github_path = tmp_path / "batches" / DATE / "github_candidates.json"
    github_text = github_path.read_text(encoding="utf-8")
    pages, digest = _read_all(tmp_path, "github", max_chars=16000)
    assert pages == github_text
    assert digest == hashlib.sha256(github_path.read_bytes()).hexdigest()
    assert str(tmp_path) not in pages
    narrowed = access.read_ai_finance_page(tmp_path, _read_fields("github", max_chars=32000))
    assert narrowed["ok"] is True
    long_body = "\u0001" * 40000
    candidate = tmp_path / "batches" / DATE / "candidates" / "doc1.txt"
    candidate.write_text(long_body, encoding="utf-8")
    _retarget_candidate_hash(tmp_path, candidate.read_bytes())
    first_page = access.read_ai_finance_page(
        tmp_path,
        _read_fields("candidate", doc_id="doc1", max_chars=32000),
    )
    assert first_page["ok"] is True
    assert first_page["complete"] is False
    assert first_page["next_offset"] is not None
    assert len(first_page["text"]) < 32000
    assert len(json.dumps(first_page, ensure_ascii=False).encode("utf-8")) <= access.MAX_RESPONSE_BYTES
    paged, page_digest = _read_all(tmp_path, "candidate", doc_id="doc1", max_chars=32000)
    assert paged == long_body
    assert page_digest == hashlib.sha256(candidate.read_bytes()).hexdigest()
    past = access.read_ai_finance_page(tmp_path, _read_fields("github", offset=10**7))
    assert past["ok"] is False
    assert past["code"] == "offset_past_end"
    assert "text" not in past
    original = github_path.read_bytes()
    github_path.write_bytes(original + SECRET.encode())
    damaged = access.read_ai_finance_page(tmp_path, _read_fields("github"))
    assert damaged["ok"] is False
    assert damaged["code"] == "hash_mismatch"
    assert SECRET not in json.dumps(damaged)
    assert github_path.read_bytes() == original + SECRET.encode()


def test_candidate_symlink_and_parent_escape_do_not_return_outside_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _owner(monkeypatch)
    prepared = _prepare(tmp_path, FIRST, EXECUTION, lambda request: _earnings(request, body="AI body"))
    assert prepared["ok"] is True
    outside = tmp_path / "outside.txt"
    outside.write_text(SECRET, encoding="utf-8")
    candidate = tmp_path / "batches" / DATE / "candidates" / "doc1.txt"
    candidate.unlink()
    candidate.symlink_to(outside)
    linked = access.read_ai_finance_page(tmp_path, _read_fields("candidate", doc_id="doc1"))
    assert linked["ok"] is False
    assert linked["code"] == "not_regular"
    assert SECRET not in json.dumps(linked)
    candidate.unlink()
    candidate.write_text("AI body", encoding="utf-8")
    _rewrite_candidate_path(tmp_path, "candidates/../../outside.txt")
    escaped = access.read_ai_finance_page(tmp_path, _read_fields("candidate", doc_id="doc1"))
    assert escaped["ok"] is False
    assert escaped["code"] == "path_escape"
    assert SECRET not in json.dumps(escaped)


def test_observation_pool_writes_are_serialized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _owner(monkeypatch)
    order: list[str] = []
    entered = threading.Event()
    release = threading.Event()
    real = access.collect_trending

    def collect(**_kwargs: Any) -> dict[str, Any]:
        path = _kwargs["state_file"]
        current = int(path.read_text(encoding="utf-8")) if path.exists() else 0
        if not entered.is_set():
            order.append("first-enter")
            entered.set()
            assert release.wait(5)
            path.write_text(str(current + 1), encoding="utf-8")
            order.append("first-exit")
        else:
            order.append("second-enter")
            path.write_text(str(current + 1), encoding="utf-8")
            order.append("second-exit")
        return {"version": 3, "candidates": []}

    monkeypatch.setattr(access, "collect_trending", create_autospec(real, side_effect=collect))
    errors: list[Exception] = []
    results: list[dict[str, Any]] = []

    def run(started: datetime, execution_id: str) -> None:
        try:
            results.append(access.prepare_ai_finance_batch(
                state_root=tmp_path,
                scheduled_at=started,
                started_at=started,
                execution_id=execution_id,
                owner_user_id=OWNER,
                transport=_unused_transport,
                earnings_collector=lambda request: _earnings(request, body="body"),
            ))
        except Exception as exc:
            errors.append(exc)

    first_thread = threading.Thread(target=run, args=(FIRST, EXECUTION))
    second_started = datetime(2026, 11, 12, 20, 0, tzinfo=HKT)
    second_thread = threading.Thread(target=run, args=(second_started, EXECUTION_2))
    first_thread.start()
    assert entered.wait(5)
    second_thread.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and "second-enter" not in order:
        time.sleep(0.01)
    assert "second-enter" not in order
    assert second_thread.is_alive()
    release.set()
    first_thread.join(5)
    second_thread.join(5)
    assert errors == []
    assert order == ["first-enter", "first-exit", "second-enter", "second-exit"]
    assert (tmp_path / "github_trending_history.json").read_text(encoding="utf-8") == "2"
    assert [item["ok"] for item in results] == [True, True]


def test_operator_json_rejects_unknown_keys_and_links(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "operator.json"
    config.write_text(json.dumps({"state_root": str(state), "token": SECRET}), encoding="utf-8")
    with pytest.raises(access.AccessError) as rejected:
        access.load_operator_config(config)
    assert rejected.value.code == "config_invalid"
    assert SECRET not in str(rejected.value)
    config.write_text(json.dumps({"state_root": str(state)}), encoding="utf-8")
    loaded = access.load_operator_config(config)
    assert loaded.state_root.stat().st_ino == state.stat().st_ino
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(state)
    config.write_text(json.dumps({"state_root": str(linked_root)}), encoding="utf-8")
    with pytest.raises(access.AccessError) as linked:
        access.load_operator_config(config)
    assert linked.value.code == "config_invalid"


def _owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DALTON_AI_FINANCE_OWNER_USER_ID", OWNER)


def _prepare_fields(execution_id: str, scheduled_at: datetime, started_at: datetime) -> dict[str, str]:
    return {
        "automation_execution_id": execution_id,
        "scheduled_at": scheduled_at.isoformat(),
        "started_at": started_at.isoformat(),
        "owner_user_id": OWNER,
        "user_id": OWNER,
        "workspace_id": "22222222-2222-4222-8222-222222222222",
        "thread_id": "33333333-3333-4333-8333-333333333333",
        "profile": "ai-finance-daily",
    }


def _prepare(
    root: Path,
    started_at: datetime,
    execution_id: str,
    earnings: Any,
    github: Any = None,
) -> dict[str, Any]:
    return access.prepare_ai_finance_batch(
        state_root=root,
        scheduled_at=started_at,
        started_at=started_at,
        execution_id=execution_id,
        owner_user_id=OWNER,
        transport=_unused_transport,
        github_collector=github if github is not None else (lambda _request: {"version": 3, "candidates": []}),
        earnings_collector=earnings,
    )


def _unused_transport(tool: str, arguments: dict[str, Any], timeout: float = 150) -> dict[str, Any]:
    del tool, arguments, timeout
    return {"authenticated": True, "status": "ready"}


def _earnings(request: dict[str, Any], *, body: str = "AI body", doc_id: str = "doc1") -> dict[str, Any]:
    (request["candidate_dir"] / f"{doc_id}.txt").write_text(body, encoding="utf-8")
    return {
        "window": request["window"],
        "candidates": [{"doc_id": doc_id, "full_text_path": f"candidates/{doc_id}.txt"}],
    }


def _read_fields(
    resource: str,
    *,
    doc_id: str | None = None,
    offset: int = 0,
    max_chars: int = 16000,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "automation_execution_id": EXECUTION,
        "owner_user_id": OWNER,
        "profile": "ai-finance-daily",
        "batch_id": DATE,
        "resource": resource,
        "offset": offset,
        "max_chars": max_chars,
    }
    if doc_id is not None:
        body["doc_id"] = doc_id
    return body


def _read_all(root: Path, resource: str, *, doc_id: str | None = None, max_chars: int = 16000) -> tuple[str, str]:
    parts: list[str] = []
    offset = 0
    digest = ""
    for _ in range(20):
        page = access.read_ai_finance_page(
            root,
            _read_fields(resource, doc_id=doc_id, offset=offset, max_chars=max_chars),
        )
        assert page["ok"] is True
        digest = page["content_sha256"]
        parts.append(page["text"])
        if page["next_offset"] is None:
            assert page["complete"] is True
            return "".join(parts), digest
        assert page["complete"] is False
        offset = page["next_offset"]
    raise AssertionError("page did not complete")


def _retarget_candidate_hash(root: Path, raw: bytes) -> None:
    batch = root / "batches" / DATE
    digest = hashlib.sha256(raw).hexdigest()
    metadata_path = batch / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["stages"]["earnings"]["candidate_files"]["candidates/doc1.txt"] = digest
    manifest_path = batch / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["candidates"][0]["sha256"] = digest
    manifest_raw = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    manifest_path.write_bytes(manifest_raw)
    metadata["manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")


def _rewrite_candidate_path(root: Path, relative: str) -> None:
    batch = root / "batches" / DATE
    manifest_path = batch / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["candidates"][0]["path"] = relative
    raw = json.dumps(manifest).encode("utf-8")
    manifest_path.write_bytes(raw)
    metadata_path = batch / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")


def _business(structured: dict[str, Any], *, ok: bool = True) -> dict[str, Any]:
    return {
        "ok": ok,
        "status": "ok" if ok else "error",
        "executed": True,
        "data": {"isError": False, "structuredContent": structured},
    }


def _envelope(business: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {
            "content": [{"type": "text", "text": json.dumps(business)}],
            "isError": False,
        },
    }
