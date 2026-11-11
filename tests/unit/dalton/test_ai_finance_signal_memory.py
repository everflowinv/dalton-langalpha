"""Offline SQLite and embedding-reuse checks. No live Gemini call."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import create_autospec
from urllib.request import Request

import pytest

from scripts.dalton.ai_finance import ai_finance_signal_memory as memory


def _record(external_id: str, summary: str, *, record_key: str | None = None) -> dict[str, object]:
    return {
        "record_key": record_key or f"earnings:{external_id}",
        "source_type": "earnings",
        "external_id": external_id,
        "event_time": "2026-10-11T00:00:00Z",
        "summary": summary,
        "signal_direction": "positive",
    }


def _vector(dim: int = 4) -> list[float]:
    return [1.0, *([0.0] * (dim - 1))]


def _row(conn: memory.sqlite3.Connection, record_key: str) -> memory.sqlite3.Row:
    row = conn.execute(
        "SELECT content_hash, embedding, embedding_model, embedding_dim FROM signals WHERE record_key = ?",
        (record_key,),
    ).fetchone()
    assert row is not None
    return row


def test_api_key_comes_from_the_environment_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    config = home / ".openclaw"
    config.mkdir(parents=True)
    (config / ".env").write_text("GEMINI_API_KEY=file-secret\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY") as missing:
        memory.embed_text("zebraquota", "RETRIEVAL_QUERY")
    assert "file-secret" not in str(missing.value)

    monkeypatch.setenv("GEMINI_API_KEY", "env-secret")
    real_urlopen = memory.urlopen
    captured: dict[str, object] = {}

    def opener(url: object, data: object = None, timeout: object = None, *, cafile: object = None, capath: object = None, cadefault: bool = False, context: object = None) -> io.BytesIO:
        del data, cafile, capath, cadefault, context
        assert isinstance(url, Request)
        captured["request"] = url
        captured["timeout"] = timeout
        values = [0.0] * 768
        values[0] = 1.0
        return io.BytesIO(json.dumps({"embedding": {"values": values}}).encode("utf-8"))

    fake_open = create_autospec(real_urlopen, side_effect=opener)
    monkeypatch.setattr(memory, "urlopen", fake_open)
    for task in ("RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"):
        vector = memory.embed_text("zebraquota", task)
        assert len(vector) == 768
        request = captured["request"]
        assert isinstance(request, Request)
        body = json.loads(request.data)
        assert body["taskType"] == task
        assert body["outputDimensionality"] == 768
        assert body["model"] == "models/gemini-embedding-2"
        assert captured["timeout"] == 20
        assert request.get_header("X-goog-api-key") == "env-secret"
        assert "file-secret" not in json.dumps(body)

    def nan_opener(url: object, data: object = None, timeout: object = None, *, cafile: object = None, capath: object = None, cadefault: bool = False, context: object = None) -> io.BytesIO:
        del url, data, timeout, cafile, capath, cadefault, context
        values = [0.0] * 768
        values[0] = float("nan")
        return io.BytesIO(json.dumps({"embedding": {"values": values}}).encode("utf-8"))

    fake_open.side_effect = nan_opener
    with pytest.raises(RuntimeError, match="finite") as bad_vector:
        memory.embed_text("zebraquota", "RETRIEVAL_DOCUMENT")
    assert "env-secret" not in str(bad_vector.value)


def test_empty_history_is_not_fts_and_compare_does_not_insert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = memory.connect_db(tmp_path / "signals.sqlite")
    calls = {"n": 0}
    real_embed = memory.embed_text

    def explode(text: str, task_type: str, *, model: str | None = None, dim: int | None = None, api_key: str | None = None) -> list[float]:
        del text, task_type, model, dim, api_key
        calls["n"] += 1
        raise AssertionError("embed should not run")

    fake_embed = create_autospec(real_embed, side_effect=explode)
    monkeypatch.setattr(memory, "embed_text", fake_embed)
    compared = memory.compare_records(conn, [_record("one", "zebraquota automation")], 5)
    assert compared["search_modes"] == ["empty_history"]
    assert calls["n"] == 0

    def refuse(text: str, task_type: str, *, model: str | None = None, dim: int | None = None, api_key: str | None = None) -> list[float]:
        del text, task_type, model, dim, api_key
        calls["n"] += 1
        raise RuntimeError("GEMINI_API_KEY not available")

    fake_embed.side_effect = refuse
    memory.ingest_records(conn, [_record("one", "zebraquota automation") | {"evidence_status": "actual"}], True)
    assert calls["n"] == 0
    before = memory.stats(conn)["signals"]
    compared = memory.compare_records(conn, [_record("two", "zebraquota staffing")], 5)
    assert compared["search_modes"] == ["fts"]
    assert compared["comparisons"][0]["matches"]
    assert compared["comparisons"][0]["matches"][0]["evidence_status"] == "actual"
    assert calls["n"] == 1
    assert memory.stats(conn)["signals"] == before
    conn.close()


def test_embedding_http_stays_outside_the_write_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = memory.connect_db(tmp_path / "signals.sqlite")

    def embed(text: str, task_type: str, *, model: str | None = None, dim: int | None = None, api_key: str | None = None) -> list[float]:
        del text, task_type, model, dim, api_key
        assert conn.in_transaction is False
        return _vector()

    monkeypatch.setattr(memory, "embed_text", create_autospec(memory.embed_text, side_effect=embed))
    result = memory.ingest_records(conn, [_record("one", "zebraquota")], False, model="model-a", dim=4)
    assert result["inserted"] == 1
    assert result["embedding_errors"] == []
    assert _row(conn, "earnings:one")["embedding"] is not None
    conn.close()


def test_same_body_reuses_vector_and_changed_body_can_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = memory.connect_db(tmp_path / "signals.sqlite")
    calls = {"n": 0}

    def embed(text: str, task_type: str, *, model: str | None = None, dim: int | None = None, api_key: str | None = None) -> list[float]:
        del text, task_type, model, dim, api_key
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("embed down")
        return _vector()

    monkeypatch.setattr(memory, "embed_text", create_autospec(memory.embed_text, side_effect=embed))
    memory.ingest_records(conn, [_record("one", "original body")], False, model="model-a", dim=4)
    original = _row(conn, "earnings:one")
    assert calls["n"] == 1
    memory.ingest_records(conn, [_record("one", "original body")], False, model="model-a", dim=4)
    assert calls["n"] == 1

    failed = memory.ingest_records(conn, [_record("one", "replacement body")], False, model="model-a", dim=4)
    changed = _row(conn, "earnings:one")
    assert failed["embedding_errors"]
    assert changed["content_hash"] != original["content_hash"]
    assert changed["embedding"] is None
    assert changed["embedding_model"] is None
    assert changed["embedding_dim"] is None

    memory.ingest_records(conn, [_record("one", "replacement body")], False, model="model-a", dim=4)
    restored = _row(conn, "earnings:one")
    assert restored["embedding"] is not None
    assert restored["embedding_model"] == "model-a"
    assert restored["embedding_dim"] == 4
    conn.close()


def test_no_embeddings_clears_a_changed_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = memory.connect_db(tmp_path / "signals.sqlite")
    monkeypatch.setattr(memory, "embed_text", create_autospec(memory.embed_text, side_effect=lambda *args, **kwargs: _vector()))
    memory.ingest_records(conn, [_record("one", "original body")], False, model="model-a", dim=4)
    memory.ingest_records(conn, [_record("one", "replacement body")], True, model="model-a", dim=4)
    changed = _row(conn, "earnings:one")
    assert changed["embedding"] is None
    assert changed["embedding_model"] is None
    conn.close()


def test_failed_model_or_dim_change_keeps_the_old_vector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = memory.connect_db(tmp_path / "signals.sqlite")

    def embed(text: str, task_type: str, *, model: str | None = None, dim: int | None = None, api_key: str | None = None) -> list[float]:
        del text, task_type, api_key
        if model == "model-b" or dim == 8:
            raise RuntimeError("embed down")
        width = dim or 4
        return _vector(width)

    monkeypatch.setattr(memory, "embed_text", create_autospec(memory.embed_text, side_effect=embed))
    memory.ingest_records(conn, [_record("one", "same body")], False, model="model-a", dim=4)
    memory.ingest_records(conn, [_record("one", "same body")], False, model="model-b", dim=4)
    kept = _row(conn, "earnings:one")
    assert kept["embedding_model"] == "model-a"
    assert kept["embedding_dim"] == 4
    other = memory.stats(conn, model="model-b", dim=4)
    assert other["embedded_signals"] == 0
    assert other["embedding_status"] == "none"
    assert other["embedded_signals_any_model"] == 1
    assert other["embedding_models"] == {"model-a": 1}
    current = memory.stats(conn, model="model-a", dim=4)
    assert current["embedded_signals"] == 1
    assert current["embedding_status"] == "complete"

    memory.ingest_records(conn, [_record("one", "same body")], False, model="model-a", dim=8)
    assert _row(conn, "earnings:one")["embedding_dim"] == 4
    assert memory.stats(conn, model="model-a", dim=8)["embedded_signals"] == 0
    conn.close()


def test_bad_source_type_is_rejected(tmp_path: Path) -> None:
    conn = memory.connect_db(tmp_path / "signals.sqlite")
    with pytest.raises(ValueError, match="source_type"):
        memory.ingest_records(conn, [_record("one", "body") | {"source_type": "news"}], True)
    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
    conn.close()


def test_fts_failure_rolls_back_the_signal_insert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = memory.connect_db(tmp_path / "signals.sqlite")
    calls = {"n": 0}
    real_update = memory.update_fts

    def flaky(connection: memory.sqlite3.Connection, record: dict[str, object]) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("fts down")
        real_update(connection, record)

    monkeypatch.setattr(memory, "update_fts", flaky)
    with pytest.raises(RuntimeError, match="fts down"):
        memory.ingest_records(
            conn,
            [_record("one", "zebraquota one"), _record("two", "zebraquota two")],
            True,
        )
    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM signals_fts").fetchone()[0] == 0
    conn.close()
