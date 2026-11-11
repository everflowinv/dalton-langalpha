"""Host digest output save, archive, and index.

Bindings come from the real prepare path. No test reads a mailbox or calls an
embedding service.
"""

from __future__ import annotations

import array
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import threading
import uuid

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton import market_digest_inputs as inputs
from scripts.dalton import market_digest_outputs as mod
from tests.unit.dalton import test_market_digest_inputs as prep

DATE = prep.DATE
MODEL = "text-embedding-test"
KEEP_BLOB = b"keep-embedding-bytes"
AM_TITLE = "# \U0001f4ca \u5e02\u573a\u6668\u62a5 \u2014 "
PM_TITLE = "# \U0001f4ca \u5e02\u573a\u665a\u62a5 \u2014 "
SOURCE_HEADING = "## \U0001f4dd \u6765\u6e90"

_CHUNKS = """
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    period TEXT,
    section_id TEXT,
    section_label TEXT,
    content TEXT NOT NULL,
    source_emails TEXT,
    embedding BLOB,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
"""
_EMAILS = """
CREATE TABLE email_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    email_id TEXT,
    message_id TEXT,
    sender TEXT,
    subject TEXT,
    chunk_index INTEGER,
    content TEXT NOT NULL,
    embedding BLOB,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
"""
_CHUNK_SQL = """
SELECT id, date, period, section_id, section_label, content, source_emails, embedding, created_at
FROM chunks ORDER BY id
"""
_EMAIL_SQL = """
SELECT id, date, email_id, message_id, sender, subject, chunk_index, content, embedding, created_at
FROM email_chunks ORDER BY id
"""


def _body(period: str, date: str, token: str, *, repeat: int = 40) -> str:
    title = AM_TITLE if period == "AM" else PM_TITLE
    return (
        f"{title}{date}\n"
        "\n"
        "## 美股\n"
        "\n"
        "### 半导体\n"
        "\n"
        f"- {'甲' * repeat}{token} \U0001f680\n"
        "\n"
        "- 末条完整内容保持到行尾\n"
        "\n"
        f"{SOURCE_HEADING}\n"
        "\n"
        f"- Jeffrey Favuzza：{token}\n"
        "- TMTB（AlphaEngine）：示例标题\n"
    )


def _prepare(layout: prep.Layout, period: str, execution: str | None = None) -> tuple[str, dict]:
    runner = prep.RecordingRunner()
    prep._successful_pair(layout, runner, period=period)
    execution_id = execution or str(uuid.uuid4())
    result = inputs.prepare_execution_inputs(
        layout.config(),
        period,
        execution_id,
        now=prep.NOW,
        runner=prep._spec_runner(runner),
        env_builder=prep._spec_env(),
    )
    assert result["preparation_status"] == "ready", result
    assert result["authoritative_date"] == DATE
    assert result["period"] == period
    return execution_id, result


def _init_db(path: Path, *, seed: bool) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(_CHUNKS)
        conn.execute(_EMAILS)
        if seed:
            def add(date: str, period: str | None, content: str) -> None:
                conn.execute(
                    """
                    INSERT INTO chunks (
                        date, period, section_id, section_label, content,
                        source_emails, embedding, created_at
                    ) VALUES (?, ?, 'keep', 'Keep', ?, '[]', ?, '2020-01-01 00:00:00')
                    """,
                    (date, period, content, KEEP_BLOB),
                )

            add(DATE, "AM", "OLD-AM-SUMMARY")
            add(DATE, "PM", "KEEP-PM")
            add(DATE, "FULL", "KEEP-FULL")
            add("2026-11-09", "AM", "KEEP-OTHER-DATE")
            add(DATE, None, "KEEP-NULL-PERIOD")
            conn.execute(
                """
                INSERT INTO email_chunks (
                    date, email_id, message_id, sender, subject, chunk_index,
                    content, embedding, created_at
                ) VALUES (?, 'e1', 'm1', 'Desk', 'Subject', 0, 'KEEP-EMAIL', ?, '2020-01-01 00:00:00')
                """,
                (DATE, KEEP_BLOB),
            )
        conn.commit()
    finally:
        conn.close()


def _query(path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return list(conn.execute(sql))
    finally:
        conn.close()


def _commits(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'dalton_digest_commits'"
        ).fetchone()
        if found is None:
            return []
        return list(
            conn.execute(
                """
                SELECT date, period, body_sha256, input_sha256, embedding_model,
                       chunk_count, chunking_version
                FROM dalton_digest_commits ORDER BY period
                """
            )
        )
    finally:
        conn.close()


class _World:
    def __init__(self, root: Path, *, seed: bool) -> None:
        self.root = root
        self.layout = prep._layout(root / "inputs")
        self.body = root / "bodies"
        self.memory = root / "memory"
        self.db = root / "vectors.db"
        self.body.mkdir()
        self.memory.mkdir()
        _init_db(self.db, seed=seed)
        payload = {
            "input_state_root": str(self.layout.state),
            "body_state_root": str(self.body),
            "market_memory_dir": str(self.memory),
            "index_db_path": str(self.db),
            "embedding_model": MODEL,
        }
        path = root / "output-config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.config = mod.load_output_config(path)

    def execution(self, period: str) -> tuple[str, dict]:
        return _prepare(self.layout, period)


def _safe(payload: dict, world: _World) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    assert world.body.name not in text
    assert world.memory.name not in text
    assert world.db.name not in text


def _embedder(calls: list[list[str]], model: str, *, vectors=None):
    def embed(texts: list[str], requested: str) -> list[list[float]]:
        assert requested == model
        assert isinstance(texts, list)
        assert all(isinstance(item, str) for item in texts)
        calls.append(list(texts))
        if vectors is not None:
            return vectors(texts)
        return [[float(index + 1), 0.25, 0.5, 0.75] for index, _ in enumerate(texts)]

    return embed


def _floats(blob: bytes) -> array.array:
    values = array.array("f")
    values.frombytes(blob)
    return values


def test_signatures_are_keyword_safe() -> None:
    inspect.signature(mod.load_output_config).bind(Path("/tmp/market-digest-output.json"))
    inspect.signature(mod.save_output).bind(object(), "execution", "AM", "markdown")
    inspect.signature(mod.get_output).bind(object(), "execution", "AM", include_body=False)
    digest = "ab" * 32
    inspect.signature(mod.finalize_output).bind(
        object(),
        "execution",
        "AM",
        digest,
        embedder=lambda texts, model: [[1.0]],
    )
    with pytest.raises(TypeError):
        inspect.signature(mod.finalize_output).bind(
            object(),
            "execution",
            "AM",
            digest,
            lambda texts, model: [[1.0]],
        )


def test_config_rejects_relative_secret_and_symlink(tmp_path: Path) -> None:
    database = tmp_path / "vectors.db"
    sqlite3.connect(database).close()
    linked = tmp_path / "linked.db"
    linked.symlink_to(database)
    state = tmp_path / "state"
    body = tmp_path / "bodies"
    memory = tmp_path / "memory"
    for path in (state, body, memory):
        path.mkdir()
    config_path = tmp_path / "output-config.json"

    def write(payload: dict) -> None:
        config_path.write_text(json.dumps(payload), encoding="utf-8")

    write(
        {
            "input_state_root": "relative/state",
            "body_state_root": str(body),
            "market_memory_dir": str(memory),
            "index_db_path": str(database),
            "embedding_model": MODEL,
        }
    )
    with pytest.raises(mod.OutputConfigError) as relative:
        mod.load_output_config(config_path)
    assert relative.value.code == "config_not_absolute"

    write(
        {
            "input_state_root": str(state),
            "body_state_root": str(body),
            "market_memory_dir": str(memory),
            "index_db_path": str(database),
            "embedding_model": MODEL,
            "api_key": "not-a-config-field",
        }
    )
    with pytest.raises(mod.OutputConfigError) as secret:
        mod.load_output_config(config_path)
    assert secret.value.code == "config_secret"

    write(
        {
            "input_state_root": str(state),
            "body_state_root": str(body),
            "market_memory_dir": str(memory),
            "index_db_path": str(linked),
            "embedding_model": MODEL,
        }
    )
    with pytest.raises(mod.OutputConfigError) as symlink:
        mod.load_output_config(config_path)
    assert symlink.value.code == "index_db_invalid"


def test_fresh_save_uses_real_ready_binding_and_recovers_metadata(tmp_path: Path) -> None:
    world = _World(tmp_path, seed=False)
    execution, prepared = world.execution("AM")
    authoritative = prepared["authoritative_date"]
    body = _body("AM", authoritative, "FRESH", repeat=3200)
    period_dir = world.body / "AM"
    assert not period_dir.exists()

    refused = mod.save_output(world.config, execution, "PM", "not-a-digest")
    assert refused == {"ok": False, "code": "period_mismatch"}
    assert not (world.body / "PM").exists()
    assert not period_dir.exists()

    saved = mod.save_output(world.config, execution, "AM", body)
    assert saved["ok"] is True
    assert saved["code"] == "saved"
    assert saved["reused"] is False
    assert saved["date"] == authoritative
    assert saved["period"] == "AM"
    assert saved["report_run_id"] == f"market-digest:AM:{authoritative}"
    assert saved["input_sha256"] == prepared["hashes"]["input_sha256"]
    assert saved["prepared_execution_id"] == prepared["execution_id"]
    assert saved["body_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    _safe(saved, world)
    stored = period_dir / authoritative / "body.md"
    metadata_path = period_dir / authoritative / "metadata.json"
    raw = stored.read_bytes()
    assert raw == body.encode("utf-8")
    metadata_text = metadata_path.read_text(encoding="utf-8")
    assert "FRESH" not in metadata_text
    assert "甲甲" not in metadata_text
    assert world.body.name not in metadata_text

    hidden = mod.get_output(world.config, execution, "AM")
    assert hidden["ok"] is True
    assert "markdown" not in hidden
    assert "FRESH" not in json.dumps(hidden, ensure_ascii=False)
    shown = mod.get_output(world.config, execution, "AM", include_body=True)
    assert shown["markdown"] == body

    reused = mod.save_output(world.config, execution, "AM", body)
    assert reused["ok"] is True
    assert reused["reused"] is True
    assert stored.read_bytes() == raw

    conflict = mod.save_output(world.config, execution, "AM", body.replace("末条完整内容", "末条被替换", 1))
    assert conflict["code"] == "body_conflict"
    assert "markdown" not in conflict
    assert stored.read_bytes() == raw

    metadata_path.unlink()
    assert not metadata_path.exists()
    recovered = mod.save_output(world.config, execution, "AM", body)
    assert recovered["ok"] is True
    assert stored.read_bytes() == raw
    assert metadata_path.is_file()
    assert mod.get_output(world.config, execution, "AM", include_body=True)["markdown"] == body
    assert mod.save_output(world.config, execution, "AM", body)["reused"] is True

    other, second = world.execution("AM")
    assert other != execution
    assert second["execution_id"] == prepared["execution_id"]
    assert second["hashes"]["input_sha256"] == prepared["hashes"]["input_sha256"]
    shared = mod.get_output(world.config, other, "AM", include_body=True)
    assert shared["markdown"] == body
    assert mod.save_output(world.config, other, "AM", body)["reused"] is True
    assert mod.save_output(world.config, other, "AM", body + "\n")["code"] == "body_conflict"
    assert stored.read_bytes() == raw

    document = json.loads(metadata_path.read_text(encoding="utf-8"))
    document["input_sha256"] = "cd" * 32
    metadata_path.write_text(json.dumps(document), encoding="utf-8")
    mismatched = mod.get_output(world.config, execution, "AM", include_body=True)
    assert mismatched == {"ok": False, "code": "input_conflict"}
    assert stored.read_bytes() == raw


def test_concurrent_archive_preserves_both_periods(tmp_path: Path) -> None:
    world = _World(tmp_path, seed=True)
    am_execution, am_prepared = world.execution("AM")
    pm_execution, pm_prepared = world.execution("PM")
    assert am_prepared["period"] == "AM"
    assert pm_prepared["period"] == "PM"
    am_body = _body("AM", DATE, "AM-TOKEN", repeat=80)
    pm_body = _body("PM", DATE, "PM-TOKEN", repeat=80)
    assert mod.save_output(world.config, am_execution, "AM", am_body)["ok"] is True
    assert mod.save_output(world.config, pm_execution, "PM", pm_body)["ok"] is True
    (world.memory / f"{DATE}.md").write_text("KEEP-OTHER\n\n## NOTES\nkeep me\n", encoding="utf-8")
    before_chunks = _query(world.db, _CHUNK_SQL)
    before_email = _query(world.db, _EMAIL_SQL)
    preserved = [row for row in before_chunks if row[2] not in {"AM", "PM"} or row[1] != DATE]
    calls: list[list[str]] = []
    barrier = threading.Barrier(2)
    results: dict[str, dict] = {}

    def run(period: str, execution: str, body_hash: str) -> None:
        def embed(texts: list[str], model: str) -> list[list[float]]:
            assert model == MODEL
            calls.append(list(texts))
            barrier.wait(timeout=10)
            return [[1.0, 2.0, 3.0, 4.0] for _ in texts]

        results[period] = mod.finalize_output(
            world.config,
            execution,
            period,
            body_hash,
            embedder=embed,
        )

    am_hash = hashlib.sha256(am_body.encode("utf-8")).hexdigest()
    pm_hash = hashlib.sha256(pm_body.encode("utf-8")).hexdigest()
    threads = [
        threading.Thread(target=run, args=("AM", am_execution, am_hash)),
        threading.Thread(target=run, args=("PM", pm_execution, pm_hash)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
        assert not thread.is_alive()
    assert results["AM"]["ok"] is True
    assert results["PM"]["ok"] is True
    _safe(results["AM"], world)
    _safe(results["PM"], world)
    archive = (world.memory / f"{DATE}.md").read_text(encoding="utf-8")
    assert "KEEP-OTHER" in archive
    assert "## NOTES\nkeep me\n" in archive
    assert am_body in archive
    assert pm_body in archive
    assert sum(line == "## AM" for line in archive.splitlines()) == 1
    assert sum(line == "## PM" for line in archive.splitlines()) == 1
    after = _query(world.db, _CHUNK_SQL)
    assert [row for row in after if row[2] not in {"AM", "PM"} or row[1] != DATE] == preserved
    assert _query(world.db, _EMAIL_SQL) == before_email
    am_chunks = [row[5] for row in after if row[1] == DATE and row[2] == "AM"]
    pm_chunks = [row[5] for row in after if row[1] == DATE and row[2] == "PM"]
    assert "".join(am_chunks) == am_body
    assert "".join(pm_chunks) == pm_body
    assert "OLD-AM-SUMMARY" not in am_chunks
    assert "KEEP-PM" not in pm_chunks


def test_index_replaces_only_own_period_and_embeds_once(tmp_path: Path) -> None:
    world = _World(tmp_path, seed=True)
    execution, prepared = world.execution("AM")
    body = _body("AM", prepared["authoritative_date"], "INDEX", repeat=3200)
    saved = mod.save_output(world.config, execution, "AM", body)
    assert saved["ok"] is True
    before = _query(world.db, _CHUNK_SQL)
    email = _query(world.db, _EMAIL_SQL)
    preserved = [row for row in before if not (row[1] == DATE and row[2] == "AM")]
    assert any(row[5] == "OLD-AM-SUMMARY" for row in before)
    calls: list[list[str]] = []
    embed = _embedder(calls, MODEL)
    wrong = mod.finalize_output(world.config, execution, "AM", "ab" * 32, embedder=embed)
    assert wrong["code"] == "hash_mismatch"
    assert calls == []
    assert _query(world.db, _CHUNK_SQL) == before

    done = mod.finalize_output(world.config, execution, "AM", saved["body_sha256"], embedder=embed)
    assert done["ok"] is True, done
    assert done["index"]["status"] == "committed"
    assert done["embedding"]["status"] == "embedded"
    _safe(done, world)
    after = _query(world.db, _CHUNK_SQL)
    assert [row for row in after if not (row[1] == DATE and row[2] == "AM")] == preserved
    assert _query(world.db, _EMAIL_SQL) == email
    own = [row for row in after if row[1] == DATE and row[2] == "AM"]
    contents = [row[5] for row in own]
    assert "".join(contents) == body
    assert all(len(content) <= 3000 for content in contents)
    assert "..." not in "".join(contents)
    assert "OLD-AM-SUMMARY" not in contents
    assert any("Jeffrey Favuzza：INDEX" in (row[5] + row[6]) for row in own)
    dims = []
    for row in own:
        values = _floats(row[7])
        assert len(values) > 0
        assert all(math.isfinite(item) for item in values)
        dims.append(len(values))
    assert len(set(dims)) == 1
    commits = _commits(world.db)
    assert commits == [
        (
            DATE,
            "AM",
            saved["body_sha256"],
            saved["input_sha256"],
            MODEL,
            len(own),
            mod.CHUNKING_VERSION,
        )
    ]
    again = mod.finalize_output(world.config, execution, "AM", saved["body_sha256"], embedder=embed)
    assert again["ok"] is True
    assert again["embedding"]["code"] == "already_indexed"
    assert again["index"]["status"] == "reused"
    assert len(calls) == 1
    assert "".join(calls[0]) == body
    assert all(len(piece) <= 3000 for piece in calls[0])
    assert _commits(world.db) == commits
    assert _query(world.db, _EMAIL_SQL) == email


@pytest.mark.parametrize("kind", ["count", "dimension", "nan", "inf"])
def test_invalid_embeddings_do_not_index(tmp_path: Path, kind: str) -> None:
    world = _World(tmp_path, seed=True)
    execution, prepared = world.execution("AM")
    body = _body("AM", prepared["authoritative_date"], kind, repeat=20)
    saved = mod.save_output(world.config, execution, "AM", body)
    before = _query(world.db, _CHUNK_SQL)
    email = _query(world.db, _EMAIL_SQL)
    calls: list[list[str]] = []

    def vectors(texts: list[str]) -> list[list[float]]:
        if kind == "count":
            return [[1.0, 2.0]]
        if kind == "dimension":
            return [[1.0, 2.0] if index == 0 else [1.0] for index, _ in enumerate(texts)]
        bad = float("nan") if kind == "nan" else float("inf")
        return [[bad, 1.0] for _ in texts]

    result = mod.finalize_output(
        world.config,
        execution,
        "AM",
        saved["body_sha256"],
        embedder=_embedder(calls, MODEL, vectors=vectors),
    )
    assert result["ok"] is False
    assert result["code"] == "embedding_invalid"
    assert result["index"]["code"] == "not_started"
    assert len(calls) == 1
    assert _query(world.db, _CHUNK_SQL) == before
    assert _query(world.db, _EMAIL_SQL) == email
    assert _commits(world.db) == []
    _safe(result, world)


def test_cached_retry_after_sqlite_failure(tmp_path: Path) -> None:
    world = _World(tmp_path, seed=True)
    execution, prepared = world.execution("AM")
    body = _body("AM", prepared["authoritative_date"], "RETRY", repeat=80)
    saved = mod.save_output(world.config, execution, "AM", body)
    before = _query(world.db, _CHUNK_SQL)
    email = _query(world.db, _EMAIL_SQL)
    calls: list[list[str]] = []

    def embed(texts: list[str], model: str) -> list[list[float]]:
        assert model == MODEL
        calls.append(list(texts))
        os.chmod(world.db, 0o400)
        return [[0.5, 1.5, 2.5, 3.5] for _ in texts]

    try:
        failed = mod.finalize_output(
            world.config,
            execution,
            "AM",
            saved["body_sha256"],
            embedder=embed,
        )
    finally:
        os.chmod(world.db, 0o600)
    assert failed["ok"] is False
    assert failed["code"] == "index_failed"
    assert failed["archive"]["code"] == "ok"
    assert failed["embedding"]["status"] == "embedded"
    assert _query(world.db, _CHUNK_SQL) == before
    assert _query(world.db, _EMAIL_SQL) == email
    assert _commits(world.db) == []
    assert list((world.body / "cache").glob("*.json"))
    _safe(failed, world)

    done = mod.finalize_output(
        world.config,
        execution,
        "AM",
        saved["body_sha256"],
        embedder=embed,
    )
    assert done["ok"] is True, done
    assert done["embedding"]["status"] == "cached"
    assert done["index"]["status"] == "committed"
    assert len(calls) == 1
    after = _query(world.db, _CHUNK_SQL)
    assert [row for row in after if not (row[1] == DATE and row[2] == "AM")] == [
        row for row in before if not (row[1] == DATE and row[2] == "AM")
    ]
    assert _query(world.db, _EMAIL_SQL) == email
    assert "".join(row[5] for row in after if row[1] == DATE and row[2] == "AM") == body
    assert not any(row[5] == "OLD-AM-SUMMARY" for row in after)
    archive = (world.memory / f"{DATE}.md").read_text(encoding="utf-8")
    assert "## AM\n" in archive
    assert body in archive
    assert "KEEP-PM" in {row[5] for row in after}
