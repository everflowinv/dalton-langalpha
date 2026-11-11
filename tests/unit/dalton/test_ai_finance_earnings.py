"""Offline checks for the AlphaEngine scanner. RPC is a test double."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

import pytest

from scripts.dalton.ai_finance.alphaengine_earnings_ai_scan import (
    EARNINGS_TYPE_ID,
    TransportUnconfigured,
    discovery_score,
    fetch_full_document,
    rpc_from_transport,
    scan,
    search_transcripts,
)

HKT = ZoneInfo("Asia/Hong_Kong")
NOW = datetime(2026, 10, 11, 20, 0, tzinfo=HKT)
QUERY = "artificial intelligence"


def rpc_template(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
    """Real callback signature used by create_autospec."""
    del tool, arguments, timeout
    raise AssertionError("rpc template is not a transport")


def _args(tmp_path: Path, transport: Any, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "transport": transport,
        "now": NOW,
        "candidate_dir": tmp_path / "candidates",
        "relative_root": tmp_path,
        "hours": 24,
        "workers": 1,
        "max_documents": 80,
        "query": [QUERY],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _document(text: str, *, sha: str | None = None, next_offset: int | None = None, chars: int | None = None) -> dict[str, Any]:
    return {
        "text": text,
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if sha is None else sha,
        "content_chars": len(text) if chars is None else chars,
        "next_offset": next_offset,
        "metadata": {},
    }


def test_missing_transport_fails_before_any_call(tmp_path: Path) -> None:
    with pytest.raises(TransportUnconfigured):
        scan(_args(tmp_path, None))


def test_transport_url_rejects_credentials_and_non_http() -> None:
    with pytest.raises(TransportUnconfigured):
        rpc_from_transport("http://user:secret@127.0.0.1/rpc")
    with pytest.raises(TransportUnconfigured):
        rpc_from_transport("ftp://127.0.0.1/rpc")


def test_unauthenticated_status_does_not_search(tmp_path: Path) -> None:
    called: list[str] = []

    def bare(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        del arguments
        called.append(tool)
        return {"authenticated": False}

    with pytest.raises(RuntimeError, match="not authenticated"):
        scan(_args(tmp_path, bare))
    assert called == ["auth_status"]


def test_search_follows_the_next_cursor() -> None:
    pages = [
        {"results": [{"doc_id": "page-1", "title": "one"}], "has_more": True, "cursor": "next"},
        {"results": [{"doc_id": "page-2", "title": "two"}], "has_more": False},
    ]

    def handle(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
        del timeout
        assert tool == "search_library"
        assert arguments["type_ids"] == [EARNINGS_TYPE_ID]
        assert arguments["limit"] == 50
        assert arguments["sort"] == "relevance"
        assert arguments["include_snippets"] is True
        page = pages[0 if "cursor" not in arguments else 1]
        if "cursor" in arguments:
            assert arguments["cursor"] == "next"
        return page

    rows, errors, _stats = search_transcripts([QUERY], "2026-10-10", "2026-10-11", create_autospec(rpc_template, side_effect=handle))
    assert errors == []
    assert {row["doc_id"] for row in rows} == {"page-1", "page-2"}


def test_full_document_joins_pages_and_stops_when_sha_changes() -> None:
    body = "AI revenue grew"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    parts = [body[:5], body[5:]]
    calls = {"n": 0}

    def handle(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
        del tool, arguments, timeout
        calls["n"] += 1
        if calls["n"] == 1:
            return _document(parts[0], sha=digest, chars=len(body), next_offset=len(parts[0]))
        return _document(parts[1], sha=digest, chars=len(body), next_offset=None)

    fetched = fetch_full_document("doc-1", create_autospec(rpc_template, side_effect=handle))
    assert calls["n"] == 2
    assert fetched["text"] == body
    assert fetched["content_chars"] == len(body)

    changed = {"n": 0}

    def change(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
        del tool, arguments, timeout
        changed["n"] += 1
        if changed["n"] == 1:
            return {"text": "AI", "content_sha256": "a" * 64, "content_chars": 4, "next_offset": 2, "metadata": {}}
        if changed["n"] == 2:
            return {"text": "xx", "content_sha256": "b" * 64, "content_chars": 4, "next_offset": 4, "metadata": {}}
        raise AssertionError("third get_document")

    with pytest.raises(RuntimeError, match="content changed"):
        fetch_full_document("doc-1", create_autospec(rpc_template, side_effect=change))
    assert changed["n"] == 2


def test_get_document_failure_is_not_retried(tmp_path: Path) -> None:
    calls: list[str] = []

    def handle(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
        del timeout
        calls.append(tool)
        if tool == "auth_status":
            return {"authenticated": True}
        if tool == "search_library":
            return {
                "results": [{"doc_id": "doc-1", "title": "AI update", "rank_date": "2026-10-11T09:00:00+08:00"}],
                "has_more": False,
            }
        assert tool == "get_document"
        raise RuntimeError("unavailable")

    payload = scan(_args(tmp_path, create_autospec(rpc_template, side_effect=handle)))
    assert calls.count("get_document") == 1
    assert payload["errors"][0]["status"] == "fetch_error"
    assert list((tmp_path / "candidates").glob("*.txt")) == []


def test_fixed_now_keeps_the_window_and_rank_filter(tmp_path: Path) -> None:
    searches: list[dict[str, Any]] = []

    def handle(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
        del timeout
        if tool == "auth_status":
            return {"authenticated": True}
        if tool == "search_library":
            searches.append(dict(arguments))
            return {
                "results": [
                    {"doc_id": "early", "title": "AI early", "rank_date": "2026-10-10T10:00:00+08:00"},
                    {"doc_id": "inside", "title": "AI inside", "rank_date": "2026-10-10T21:00:00+08:00"},
                ],
                "has_more": False,
            }
        assert arguments["doc_id"] == "inside"
        text = "The AI assistant grew revenue."
        return _document(text)

    rpc = create_autospec(rpc_template, side_effect=handle)
    first = scan(_args(tmp_path, rpc))
    second = scan(_args(tmp_path / "other", rpc, candidate_dir=tmp_path / "other" / "candidates", relative_root=tmp_path / "other"))
    assert searches[0]["date_from"] == "2026-10-10"
    assert searches[0]["date_to"] == "2026-10-11"
    assert searches[1]["date_from"] == searches[0]["date_from"]
    assert searches[1]["date_to"] == searches[0]["date_to"]
    assert [row["doc_id"] for row in first["selected_documents"]] == ["inside"]
    assert first["window"]["end"] == second["window"]["end"]
    assert first["candidates"][0]["full_text_path"] == "candidates/inside.txt"


def test_only_top_ranked_documents_are_fetched(tmp_path: Path) -> None:
    rows = [
        {"doc_id": "low", "title": "operations update", "snippet": "steady quarter", "rank_date": "2026-10-11T09:00:00+08:00"},
        {"doc_id": "mid", "title": "AI pilot", "snippet": "one team", "rank_date": "2026-10-11T10:00:00+08:00"},
        {
            "doc_id": "high",
            "title": "generative AI revenue declined",
            "snippet": "analyst: $12 million and 15% margin pressure",
            "rank_date": "2026-10-11T11:00:00+08:00",
        },
    ]
    scored = [
        (discovery_score({**item, "matched_queries": [QUERY]}), str(item["doc_id"]))
        for item in rows
    ]
    scored.sort()
    fetched: list[str] = []

    def handle(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
        del timeout
        if tool == "auth_status":
            return {"authenticated": True}
        if tool == "search_library":
            return {"results": rows, "has_more": False}
        fetched.append(arguments["doc_id"])
        return _document("AI revenue grew.")

    scan(_args(tmp_path, create_autospec(rpc_template, side_effect=handle), max_documents=2))
    assert scored[0] == (min(score for score, _doc_id in scored), "low")
    assert scored[0][0] < scored[1][0]
    assert set(fetched) == {doc_id for _score, doc_id in scored[1:]}
    assert len(fetched) == 2


def test_only_ai_hits_are_saved_and_negative_qa_sorts_first(tmp_path: Path) -> None:
    texts = {
        "plain": "AI revenue grew.",
        "nq": "AI revenue declined. analyst: why the loss?",
        "other": "Revenue grew in the quarter.",
    }

    def handle(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
        del timeout
        if tool == "auth_status":
            return {"authenticated": True}
        if tool == "search_library":
            return {
                "results": [
                    {"doc_id": doc_id, "title": "update", "rank_date": "2026-10-11T09:00:00+08:00"}
                    for doc_id in texts
                ],
                "has_more": False,
            }
        return _document(texts[arguments["doc_id"]])

    payload = scan(_args(tmp_path, create_autospec(rpc_template, side_effect=handle)))
    names = sorted(path.name for path in (tmp_path / "candidates").glob("*.txt"))
    assert names == ["nq.txt", "plain.txt"]
    assert [item["doc_id"] for item in payload["candidates"]] == ["nq", "plain"]
    assert payload["candidates"][0]["negative_qa_priority"] is True
    assert payload["candidates"][1]["negative_qa_priority"] is False
    assert payload["candidates"][0]["full_text_path"] == "candidates/nq.txt"
