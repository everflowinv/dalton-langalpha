"""Regression: reconcile must not record a direct edit when only the JSON bytes differ.

The live bug: ``content_sha256`` was hashed over the in-memory key order, the file was
written from the jsonb round-trip (another key order), so every post-turn reconcile
recorded an identical ``direct_edit`` version plus a ``rendered_md_edited`` follow-up.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.database import knowledge as db_knowledge
from src.dalton.database import publications as db_publications
from src.dalton.database import verdicts as db_verdicts
from src.dalton.knowledge import reconcile, service
from src.dalton.knowledge.paths import data_path, rendered_path
from src.dalton.knowledge.service import canonical_json
from src.dalton.knowledge.validate import validate_update
from src.server.database import workspace as ws_db

WS = "ws-1"
USER = "user-1"


def _lessons_doc() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "lessons": [
            {
                "id": "L-001",
                "statement": "Capex guides lag the order book by two quarters",
                "applies_when": "a hyperscaler raises capex guidance",
                "origin": {"event": "Q3 call", "at": "2026-10-18"},
                "scope": "company",
                "confidence": "high",
                "status": "active",
                "created_at": "2026-10-18T12:00:00Z",
            }
        ],
    }


def _jsonb_order(value: Any) -> Any:
    """Mimic jsonb's key order on read-back (shorter keys first, then bytewise)."""
    if isinstance(value, dict):
        return {
            k: _jsonb_order(value[k])
            for k in sorted(value, key=lambda k: (len(k.encode()), k.encode()))
        }
    if isinstance(value, list):
        return [_jsonb_order(v) for v in value]
    return value


class _Backend:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    async def awrite_text(self, path: str, text: str) -> None:
        self.files[path] = text

    async def aread_text(self, path: str) -> str | None:
        return self.files.get(path)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    backend = _Backend()
    lock = asyncio.Lock()

    def _latest(doc_key: str, *, valid_only: bool = False, written: bool = False):
        cand = [
            r
            for r in rows
            if r["doc_key"] == doc_key
            and (not valid_only or r["schema_valid"])
            and (not written or r["files_written_at"] is not None)
        ]
        return max(cand, key=lambda r: r["version_no"]) if cand else None

    async def insert_impl(**kw: Any) -> dict[str, Any]:
        # Same contract as the real query: per-doc advisory lock, CAS for tool or
        # enforce_base, content read back in jsonb key order.
        async with lock:
            latest = _latest(kw["doc_key"])
            await asyncio.sleep(0)
            latest_no = latest["version_no"] if latest else None
            if (kw.get("source") == "tool" or kw.get("enforce_base")) and kw[
                "base_version_no"
            ] != latest_no:
                raise db_knowledge.VersionConflict(kw["doc_key"], kw["base_version_no"], latest)
            row = {
                "knowledge_version_id": f"kv-{len(rows) + 1}",
                "workspace_id": kw["workspace_id"],
                "doc_key": kw["doc_key"],
                "version_no": (latest_no or 0) + 1,
                "source": kw.get("source", "tool"),
                "content": _jsonb_order(copy.deepcopy(kw["content"])),
                "content_sha256": kw["content_sha256"],
                "warnings": kw.get("warnings") or [],
                "schema_valid": kw.get("schema_valid", True),
                "edited_path": kw.get("edited_path"),
                "files_written_at": None,
                "created_at": datetime(2026, 10, 23, 15, 0, len(rows), tzinfo=timezone.utc),
            }
            rows.append(row)
            return copy.deepcopy(row)

    async def list_all_latest_impl(workspace_id: str, *, valid_only: bool = True):
        keys = {r["doc_key"] for r in rows}
        out = {k: _latest(k, valid_only=valid_only) for k in keys}
        return {k: copy.deepcopy(v) for k, v in out.items() if v is not None}

    async def written_valid_impl(workspace_id: str, doc_key: str):
        return copy.deepcopy(_latest(doc_key, valid_only=True, written=True))

    async def mark_written_impl(knowledge_version_id: str) -> None:
        for r in rows:
            if r["knowledge_version_id"] == knowledge_version_id:
                r["files_written_at"] = datetime.now(timezone.utc)

    async def meta_impl(workspace_id: str, *, conn=None):
        return {
            p: {"kind": "file", "content_hash": hashlib.sha256(t.encode()).hexdigest()}
            for p, t in backend.files.items()
        }

    async def get_file_impl(workspace_id, file_path, *, include_content=True, conn=None):
        text = backend.files.get(file_path)
        return None if text is None else {"file_path": file_path, "content_text": text}

    async def resolve_impl(file_record: dict, *, user_id: str, context: str):
        return file_record["content_text"]

    insert_mock = create_autospec(db_knowledge.insert_version, side_effect=insert_impl)
    monkeypatch.setattr(db_knowledge, "insert_version", insert_mock)
    monkeypatch.setattr(
        db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(db_knowledge.list_all_latest_for_workspace, side_effect=list_all_latest_impl),
    )
    monkeypatch.setattr(
        db_knowledge,
        "latest_written_valid_version",
        create_autospec(db_knowledge.latest_written_valid_version, side_effect=written_valid_impl),
    )
    monkeypatch.setattr(
        db_knowledge,
        "mark_files_written",
        create_autospec(db_knowledge.mark_files_written, side_effect=mark_written_impl),
    )
    monkeypatch.setattr(
        db_verdicts, "list_verdicts", create_autospec(db_verdicts.list_verdicts, return_value=[])
    )
    monkeypatch.setattr(
        db_publications,
        "list_publication_files",
        create_autospec(db_publications.list_publication_files, return_value=[]),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, return_value={"workspace_id": WS, "user_id": USER}),
    )
    from src.server.database import workspace_file as wf_db
    from src.server.services.persistence import resolve as resolve_mod

    monkeypatch.setattr(
        reconcile,
        "get_file_metadata_for_sync",
        create_autospec(wf_db.get_file_metadata_for_sync, side_effect=meta_impl),
    )
    monkeypatch.setattr(
        reconcile, "db_get_file", create_autospec(wf_db.get_file, side_effect=get_file_impl)
    )
    monkeypatch.setattr(
        reconcile,
        "resolve_file_text_or_none",
        create_autospec(resolve_mod.resolve_file_text_or_none, side_effect=resolve_impl),
    )
    return {"rows": rows, "backend": backend, "insert": insert_mock}


async def _tool_write(env: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    """The apply_update tail: hash the in-memory doc, insert, write files from the row."""
    assert validate_update("lessons", None, doc, reason="seed").ok
    _, sha = canonical_json(doc)
    row = await db_knowledge.insert_version(
        user_id=USER,
        workspace_id=WS,
        doc_key="lessons",
        base_version_no=None,
        content=doc,
        content_sha256=sha,
        ops=None,
        reason="seed",
        source="import",
    )
    await service.write_version_files_to_backend(WS, row, env["backend"])
    return row


def test_canonical_json_ignores_key_order() -> None:
    doc = _lessons_doc()
    assert canonical_json(doc) == canonical_json(_jsonb_order(doc))
    assert list(doc) != list(_jsonb_order(doc))


@pytest.mark.asyncio
async def test_jsonb_key_reorder_is_not_a_direct_edit(env: dict[str, Any]) -> None:
    row = await _tool_write(env, _lessons_doc())
    file_sha = hashlib.sha256(env["backend"].files[data_path("lessons")].encode()).hexdigest()
    assert file_sha == row["content_sha256"]

    out = await reconcile.detect_direct_edits(WS, USER)

    assert out["recorded_versions"] == []
    assert [r["version_no"] for r in env["rows"]] == [1]


@pytest.mark.asyncio
async def test_legacy_hash_mismatch_with_same_content_is_not_a_direct_edit(
    env: dict[str, Any],
) -> None:
    # Rows written before the fix carry a hash of the in-memory order, and the file holds
    # the jsonb order: the hashes differ but the content is the same.
    await _tool_write(env, _lessons_doc())
    env["rows"][0]["content_sha256"] = "0" * 64
    jsonb_text = json.dumps(_jsonb_order(_lessons_doc()), ensure_ascii=False, indent=2) + "\n"
    env["backend"].files[data_path("lessons")] = jsonb_text

    out = await reconcile.detect_direct_edits(WS, USER)

    assert out["recorded_versions"] == []
    assert len(env["rows"]) == 1


@pytest.mark.asyncio
async def test_real_json_edit_recorded_once_without_md_false_positive(
    env: dict[str, Any],
) -> None:
    await _tool_write(env, _lessons_doc())
    edited = _lessons_doc()
    edited["lessons"][0]["statement"] = "Edited by hand"
    env["backend"].files[data_path("lessons")] = json.dumps(edited, indent=4)

    out = await reconcile.detect_direct_edits(WS, USER)

    recorded = out["recorded_versions"]
    assert len(recorded) == 1
    assert recorded[0]["edited_path"] == data_path("lessons")
    assert recorded[0]["content"]["lessons"][0]["statement"] == "Edited by hand"
    # lessons.md still shows v1 until apply_pending re-renders it: not an md edit.
    assert not any(w.get("code") == "rendered_md_edited" for w in recorded[0]["warnings"])

    again = await reconcile.detect_direct_edits(WS, USER)
    assert again["recorded_versions"] == []
    assert len(env["rows"]) == 2


@pytest.mark.asyncio
async def test_real_md_edit_still_detected(env: dict[str, Any]) -> None:
    await _tool_write(env, _lessons_doc())
    md_rel = rendered_path("lessons")
    env["backend"].files[md_rel] += "\nhand-written note\n"

    out = await reconcile.detect_direct_edits(WS, USER)

    assert len(out["recorded_versions"]) == 1
    assert out["recorded_versions"][0]["edited_path"] == md_rel
    assert out["recorded_versions"][0]["warnings"][0]["code"] == "rendered_md_edited"


@pytest.mark.asyncio
async def test_concurrent_reconciles_record_at_most_one_version(
    env: dict[str, Any],
) -> None:
    await _tool_write(env, _lessons_doc())
    edited = _lessons_doc()
    edited["lessons"][0]["statement"] = "Edited by hand"
    env["backend"].files[data_path("lessons")] = json.dumps(edited)

    results = await asyncio.gather(
        reconcile.detect_direct_edits(WS, USER),
        reconcile.detect_direct_edits(WS, USER),
    )

    recorded = [v for r in results for v in r["recorded_versions"]]
    assert len(recorded) == 1
    assert [r["version_no"] for r in env["rows"]] == [1, 2]
    for call in env["insert"].call_args_list[1:]:
        assert call.kwargs["enforce_base"] is True


def test_dedupe_script_lists_only_spurious_tail() -> None:
    from scripts.dalton.dedupe_knowledge_direct_edits import find_spurious

    doc = _lessons_doc()
    edited = copy.deepcopy(doc)
    edited["lessons"][0]["statement"] = "real edit"

    def row(doc_key, no, source, content, edited_path=None, codes=()):
        return {
            "knowledge_version_id": f"{doc_key}-{no}",
            "doc_key": doc_key,
            "version_no": no,
            "source": source,
            "content": content,
            "edited_path": edited_path,
            "warnings": [{"code": c} for c in codes],
        }

    rows = [
        # The incident shape: tool v1, same-content json row, then the md header bump.
        row("lessons", 1, "tool", doc),
        row("lessons", 2, "direct_edit", _jsonb_order(doc), "data/lessons.json"),
        row("lessons", 3, "direct_edit", doc, "lessons.md", ("rendered_md_edited",)),
        # A real edit, and a real md edit after a real version, stay.
        row("debate", 1, "tool", doc),
        row("debate", 2, "direct_edit", edited, "data/debate.json"),
        row("agenda", 1, "tool", doc),
        row("agenda", 2, "direct_edit", doc, "agenda.md", ("rendered_md_edited",)),
        # Spurious rows followed by a real version are not deletable.
        row("thesis", 1, "tool", doc),
        row("thesis", 2, "direct_edit", doc, "data/thesis.json"),
        row("thesis", 3, "tool", edited),
    ]

    deletable, notes = find_spurious(rows)

    assert [r["knowledge_version_id"] for r in deletable] == ["lessons-2", "lessons-3"]
    assert len(notes) == 1 and notes[0].startswith("thesis:")
