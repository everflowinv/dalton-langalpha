"""Per-batch retrieval recovery. Transport is an exact-signature double."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

import pytest

import scripts.dalton.ai_finance.batch_retrieval as retrieval_mod
import scripts.dalton.ai_finance.prepare as prepare_mod
from scripts.dalton.ai_finance.alphaengine_earnings_ai_scan import scan
from scripts.dalton.ai_finance.batch_retrieval import UnresolvedRetrieval, open_batch_retrieval
from scripts.dalton.ai_finance.prepare import prepare_batch

HKT = ZoneInfo("Asia/Hong_Kong")
FIXED = datetime(2026, 10, 11, 12, 0, tzinfo=HKT)
BATCH = "batch-20261011"
QUERY = "artificial intelligence"


def rpc_template(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
    """Real callback signature used by create_autospec."""
    del tool, arguments, timeout
    raise AssertionError("rpc template is not a transport")


def _document(text: str) -> dict[str, Any]:
    return {
        "text": text,
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "content_chars": len(text),
        "next_offset": None,
        "metadata": {},
    }


def _hit(doc_id: str, rank: str) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "title": "AI update",
        "snippet": "AI revenue",
        "rank_date": rank,
    }


def _github(request: dict[str, Any]) -> dict[str, Any]:
    assert request["now"] == FIXED
    return {"version": 3, "candidates": []}


def _prepare(tmp_path: Path, transport: Any, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "state_root": tmp_path,
        "batch_id": BATCH,
        "started_at": FIXED,
        "transport": transport,
        "github_collector": _github,
        "queries": [QUERY],
    }
    values.update(overrides)
    return prepare_batch(**values)


def _transport(handle: Any) -> Any:
    return create_autospec(rpc_template, side_effect=handle)


def test_artifact_write_failure_retrieves_each_document_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    texts = {"doc-1": "AI revenue grew."}

    def handle(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
        del timeout
        calls.append(tool)
        if tool == "auth_status":
            return {"authenticated": True}
        if tool == "search_library":
            if calls.count("search_library") > 1:
                return {"results": [_hit("extra", "2026-10-11T09:30:00+08:00"), _hit("doc-1", "2026-10-11T09:00:00+08:00")], "has_more": False}
            return {"results": [_hit("doc-1", "2026-10-11T09:00:00+08:00")], "has_more": False}
        assert tool == "get_document"
        assert arguments["doc_id"] == "doc-1"
        assert arguments["offset"] == 0
        assert arguments["max_chars"] == 30000
        return _document(texts["doc-1"])

    real_write = prepare_mod._atomic_write
    armed = {"once": True}

    def flaky_write(path: Path, payload: bytes) -> None:
        if armed["once"] and path.name == "earnings_scan.json":
            armed["once"] = False
            raise OSError("artifact")
        real_write(path, payload)

    monkeypatch.setattr(prepare_mod, "_atomic_write", flaky_write)
    transport = _transport(handle)
    first = _prepare(tmp_path, transport)
    batch = tmp_path / "batches" / BATCH
    candidate = batch / "candidates" / "doc-1.txt"
    assert first["ok"] is False
    assert first["errors"] == ["earnings_failed:OSError"]
    assert not (batch / "manifest.json").exists()
    assert not (batch / "earnings_scan.json").exists()
    assert candidate.read_text(encoding="utf-8") == "AI revenue grew."
    assert calls.count("get_document") == 1
    assert calls.count("search_library") == 1

    second = _prepare(tmp_path, transport)
    assert second["ok"] is True
    assert second["manifest"] == "manifest.json"
    assert calls.count("auth_status") == 2
    assert calls.count("search_library") == 1
    assert calls.count("get_document") == 1
    assert candidate.read_text(encoding="utf-8") == "AI revenue grew."
    assert not (batch / "candidates" / "extra.txt").exists()
    assert not (tmp_path / "github_seen.json").exists()
    assert list(tmp_path.rglob("*.sqlite")) == []


def test_crash_between_documents_keeps_the_saved_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetched: list[str] = []
    texts = {"doc-1": "AI revenue grew.", "doc-2": "AI demand expanded."}

    def handle(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
        del timeout
        if tool == "auth_status":
            return {"authenticated": True}
        if tool == "search_library":
            return {
                "results": [
                    _hit("doc-1", "2026-10-11T09:00:00+08:00"),
                    _hit("doc-2", "2026-10-11T10:00:00+08:00"),
                ],
                "has_more": False,
            }
        assert tool == "get_document"
        fetched.append(arguments["doc_id"])
        return _document(texts[arguments["doc_id"]])

    real_write = retrieval_mod._atomic_write
    crash = {"armed": True}

    def crash_after_first_page(path: Path, payload: bytes) -> None:
        real_write(path, payload)
        if not crash["armed"] or not path.name.startswith("get_document-"):
            return
        record = json.loads(payload)
        if record.get("state") == "confirmed" and record.get("outcome") == "value":
            crash["armed"] = False
            raise SystemExit("crash between documents")

    monkeypatch.setattr(retrieval_mod, "_atomic_write", crash_after_first_page)
    transport = _transport(handle)
    with pytest.raises(SystemExit, match="crash between documents"):
        _prepare(tmp_path, transport, workers=1)
    saved_id = fetched[0]
    journal_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "batches" / BATCH / ".retrieval" / "calls").glob("get_document-*.json")
    )
    assert texts[saved_id] in journal_text
    assert fetched == [saved_id]

    crash["armed"] = False
    result = _prepare(tmp_path, transport, workers=1)
    batch = tmp_path / "batches" / BATCH
    assert result["ok"] is True
    assert fetched.count(saved_id) == 1
    assert sorted(fetched) == ["doc-1", "doc-2"]
    assert (batch / "candidates" / f"{saved_id}.txt").read_text(encoding="utf-8") == texts[saved_id]
    assert (batch / "manifest.json").is_file()


def test_inflight_document_is_not_refetched_or_published(tmp_path: Path) -> None:
    calls: list[str] = []

    def handle(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
        del arguments, timeout
        calls.append(tool)
        if tool == "auth_status":
            return {"authenticated": True}
        if tool == "search_library":
            return {"results": [_hit("doc-1", "2026-10-11T09:00:00+08:00")], "has_more": False}
        raise SystemExit("interrupted get_document")

    transport = _transport(handle)
    with pytest.raises(SystemExit, match="interrupted get_document"):
        _prepare(tmp_path, transport, workers=1)
    assert calls.count("get_document") == 1
    batch = tmp_path / "batches" / BATCH
    records = list((batch / ".retrieval" / "calls").glob("get_document-*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text(encoding="utf-8"))["state"] == "inflight"

    result = _prepare(tmp_path, transport, workers=1)
    assert result["ok"] is False
    assert result["manifest"] is None
    assert result["errors"] == ["earnings_failed:UnresolvedRetrieval"]
    assert result["stages"]["earnings"] != "complete"
    assert calls.count("get_document") == 1
    assert not (batch / "earnings_scan.json").exists()
    assert not (batch / "manifest.json").exists()


def test_corrupt_retrieval_record_refuses_another_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def handle(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
        del arguments, timeout
        calls.append(tool)
        if tool == "auth_status":
            return {"authenticated": True}
        if tool == "search_library":
            return {"results": [_hit("doc-1", "2026-10-11T09:00:00+08:00")], "has_more": False}
        return _document("AI revenue grew.")

    real_write = prepare_mod._atomic_write
    armed = {"once": True}

    def flaky_write(path: Path, payload: bytes) -> None:
        if armed["once"] and path.name == "earnings_scan.json":
            armed["once"] = False
            raise OSError("artifact")
        real_write(path, payload)

    monkeypatch.setattr(prepare_mod, "_atomic_write", flaky_write)
    transport = _transport(handle)
    failed = _prepare(tmp_path, transport)
    assert failed["ok"] is False
    record = next((tmp_path / "batches" / BATCH / ".retrieval" / "calls").glob("get_document-*.json"))
    original = record.read_bytes()
    damaged = bytearray(original)
    damaged[-2] ^= 0x1
    record.write_bytes(damaged)
    saved_calls = list(calls)

    result = _prepare(tmp_path, transport)
    batch = tmp_path / "batches" / BATCH
    assert result["ok"] is False
    assert result["manifest"] is None
    assert result["errors"] == ["earnings_failed:RetrievalCacheCorrupt"]
    assert calls == [*saved_calls, "auth_status"]
    assert calls.count("get_document") == 1
    assert calls.count("search_library") == 1
    assert record.read_bytes() == bytes(damaged)
    assert not (batch / "manifest.json").exists()


def test_same_page_from_two_workers_calls_upstream_once(tmp_path: Path) -> None:
    calls = {"get_document": 0}
    lock = threading.Lock()

    def handle(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
        assert timeout == 150
        if tool == "auth_status":
            return {"authenticated": True}
        if tool == "search_library":
            assert arguments["query"] == QUERY
            assert arguments["limit"] == 50
            return {"results": [_hit("doc-1", "2026-10-11T09:00:00+08:00")], "has_more": False}
        assert tool == "get_document"
        assert arguments == {"doc_id": "doc-1", "offset": 0, "max_chars": 30000}
        with lock:
            calls["get_document"] += 1
        time.sleep(0.05)
        return _document("AI revenue grew.")

    batch = tmp_path / "batches" / BATCH
    batch.mkdir(parents=True)
    transport = _transport(handle)
    found: list[str] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            payload = scan(_scan_args(batch, transport))
        except BaseException as exc:
            errors.append(exc)
            return
        found.append(payload["candidates"][0]["doc_id"])

    threads = [threading.Thread(target=run), threading.Thread(target=run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert found == ["doc-1", "doc-1"]
    assert calls["get_document"] == 1
    assert (batch / "candidates" / "doc-1.txt").read_text(encoding="utf-8") == "AI revenue grew."


def test_auth_failure_before_retrieval_can_be_retried(tmp_path: Path) -> None:
    calls: list[str] = []
    ready = {"auth": False}

    def handle(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
        del arguments, timeout
        calls.append(tool)
        if tool == "auth_status":
            return {"authenticated": ready["auth"]}
        if tool == "search_library":
            return {"results": [_hit("doc-1", "2026-10-11T09:00:00+08:00")], "has_more": False}
        return _document("AI revenue grew.")

    transport = _transport(handle)
    first = _prepare(tmp_path, transport)
    assert first["ok"] is False
    assert first["errors"] == ["earnings_failed:RuntimeError"]
    assert calls == ["auth_status"]
    assert not (tmp_path / "batches" / BATCH / ".retrieval").exists()

    ready["auth"] = True
    second = _prepare(tmp_path, transport)
    assert second["ok"] is True
    assert calls.count("get_document") == 1
    assert (tmp_path / "batches" / BATCH / "candidates" / "doc-1.txt").read_text(encoding="utf-8") == "AI revenue grew."


def test_completed_earnings_stage_does_not_retrieve_again(tmp_path: Path) -> None:
    calls: list[str] = []

    def handle(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
        del arguments, timeout
        calls.append(tool)
        if tool == "auth_status":
            return {"authenticated": True}
        if tool == "search_library":
            return {"results": [_hit("doc-1", "2026-10-11T09:00:00+08:00")], "has_more": False}
        return _document("AI revenue grew.")

    transport = _transport(handle)
    first = _prepare(tmp_path, transport)
    assert first["ok"] is True
    assert first["reused"] == []
    saved = list(calls)
    second = _prepare(tmp_path, transport)
    assert second["ok"] is True
    assert second["reused"] == ["github", "earnings"]
    assert calls == saved
    assert calls.count("get_document") == 1


def test_failed_page_persist_is_unresolved_and_not_repeated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    real_write = retrieval_mod._atomic_write

    def fail_confirmed(path: Path, payload: bytes) -> None:
        record = json.loads(payload)
        if record.get("state") == "confirmed" and record.get("tool") == "get_document":
            raise OSError("journal")
        real_write(path, payload)

    monkeypatch.setattr(retrieval_mod, "_atomic_write", fail_confirmed)
    batch = tmp_path / "batches" / BATCH
    batch.mkdir(parents=True)
    journal = open_batch_retrieval(
        batch,
        batch_id=BATCH,
        started_at=FIXED,
        hours=24,
        max_documents=80,
        queries=[QUERY],
    )
    arguments = {"doc_id": "doc-1", "offset": 0, "max_chars": 30000}

    def transport(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
        del tool, arguments, timeout
        calls["n"] += 1
        return _document("AI revenue grew.")

    def invoke() -> dict[str, Any]:
        return transport("get_document", arguments, timeout=150)

    with pytest.raises(UnresolvedRetrieval, match="not durable"):
        journal.call("get_document", arguments, invoke)
    assert calls["n"] == 1
    with pytest.raises(UnresolvedRetrieval, match="no confirmed response"):
        journal.call("get_document", arguments, invoke)
    assert calls["n"] == 1


def test_provider_error_is_replayed_and_not_treated_as_unresolved(tmp_path: Path) -> None:
    calls = {"get_document": 0}

    def handle(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
        del arguments, timeout
        if tool == "auth_status":
            return {"authenticated": True}
        if tool == "search_library":
            return {"results": [_hit("doc-1", "2026-10-11T09:00:00+08:00")], "has_more": False}
        calls["get_document"] += 1
        raise RuntimeError("unavailable")

    batch = tmp_path / "batches" / BATCH
    batch.mkdir(parents=True)
    transport = _transport(handle)
    first = scan(_scan_args(batch, transport))
    second = scan(_scan_args(batch, transport))
    assert calls["get_document"] == 1
    assert first["errors"][0]["status"] == "fetch_error"
    assert first["errors"][0]["error"] == "RuntimeError: unavailable"
    assert second["errors"][0]["error"] == "RuntimeError: unavailable"
    assert "UnresolvedRetrieval" not in second["errors"][0]["error"]


def _scan_args(batch: Path, transport: Any) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        transport=transport,
        now=FIXED,
        candidate_dir=batch / "candidates",
        relative_root=batch,
        hours=24,
        workers=2,
        max_documents=80,
        query=[QUERY],
        retrieval_root=batch,
        batch_id=BATCH,
    )
