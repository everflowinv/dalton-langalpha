"""A dispatch whose declared files are missing is partial, not done — R3a #5.

The 10/30 cases: a knowledge-revision stage journalled ``ok`` with
``knowledge-revision.json`` never written, and branch children that reported
"last Write was refused at the time limit" in a schema-valid ``{files: []}``
answer, collected as successes the main agent then had to redo one by one.
"""

from __future__ import annotations

import json

import pytest

from .test_driver import Immediate, _make_driver, _script

_FILES_SCHEMA = (
    "{ type: 'object', properties: { files: { type: 'array', "
    "items: { type: 'string' } }, summary: { type: 'string' } }, "
    "required: ['files', 'summary'] }"
)


def _child_answer(files: list[str], summary: str = "done") -> Immediate:
    return Immediate(
        {"success": True, "result": json.dumps({"files": files, "summary": summary})}
    )


@pytest.mark.asyncio
async def test_a_declared_file_that_was_never_written_makes_the_dispatch_partial():
    """The GOOGL shape: the child answers ok and declares the deliverable,
    the file is not on disk. The script sees null (as any failed child), the
    journal entry says partial and names the file, so a resume re-dispatches
    instead of replaying the 'success'."""
    driver, _, _, backend = await _make_driver(
        _script(f"return await agent('write it', {{ schema: {_FILES_SCHEMA} }});"),
        behaviors=[_child_answer(["research/knowledge-revision.json"])],
    )

    await driver.run()

    assert json.loads(backend.writes[f"{driver.base_rel}/result.json"]) is None
    record = json.loads(backend.writes[f"{driver.base_rel}/children/000.json"])
    assert record["partial_artifacts"] == [
        "research/knowledge-revision.json（不存在）"
    ]
    journal = json.loads(
        backend.writes[f"{driver.base_rel}/journal/write_it.json"]
    )
    (entry,) = journal["dispatches"][-1:]
    assert entry["status"] == "partial"
    assert entry["replayable"] is False
    assert "research/knowledge-revision.json" in entry["missing_artifacts"][0]


@pytest.mark.asyncio
async def test_an_empty_files_declaration_is_partial_too():
    """The 牧原 shape: the schema-valid "I wrote nothing" answer — the branch
    whose Write was refused — is not a success either."""
    driver, _, _, backend = await _make_driver(
        _script(f"return await agent('branch', {{ schema: {_FILES_SCHEMA} }});"),
        behaviors=[_child_answer([], summary="最后 Write 被时限拦截")],
    )

    await driver.run()

    record = json.loads(backend.writes[f"{driver.base_rel}/children/000.json"])
    assert record["partial_artifacts"] == ["声明了 files/file 但未写出任何文件"]
    journal = json.loads(backend.writes[f"{driver.base_rel}/journal/branch.json"])
    (entry,) = journal["dispatches"][-1:]
    assert entry["status"] == "partial"


@pytest.mark.asyncio
async def test_an_empty_file_on_disk_is_partial():
    """A declared artifact that exists but holds nothing — a truncated write
    — is the same verdict."""
    from .conftest import FakeBackend

    driver, _, _, backend = await _make_driver(
        _script(f"return await agent('write it', {{ schema: {_FILES_SCHEMA} }});"),
        behaviors=[_child_answer(["research/out.json"])],
    )
    # The backend the driver holds is the one the journal digests through.
    driver.spec.backend.files["research/out.json"] = ""
    assert isinstance(backend, FakeBackend)

    await driver.run()

    record = json.loads(backend.writes[f"{driver.base_rel}/children/000.json"])
    assert record["partial_artifacts"] == ["research/out.json（空文件）"]


@pytest.mark.asyncio
async def test_declared_files_that_exist_answer_as_before():
    """The unchanged case: every declared file on disk and non-empty — the
    script gets the parsed answer and the journal entry stays ok/replayable."""
    driver, _, _, backend = await _make_driver(
        _script(f"return await agent('write it', {{ schema: {_FILES_SCHEMA} }});"),
        behaviors=[
            _child_answer(
                ["research/out.json|0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"]
            )
        ],
    )
    driver.spec.backend.files["research/out.json"] = "real content\n"

    await driver.run()

    assert json.loads(backend.writes[f"{driver.base_rel}/result.json"]) == {
        "files": [
            "research/out.json|0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        ],
        "summary": "done",
    }
    record = json.loads(backend.writes[f"{driver.base_rel}/children/000.json"])
    assert record["partial_artifacts"] == []
    journal = json.loads(backend.writes[f"{driver.base_rel}/journal/write_it.json"])
    (entry,) = journal["dispatches"][-1:]
    assert entry["status"] == "ok"
    assert entry["replayable"] is True


@pytest.mark.asyncio
async def test_a_partial_dispatch_is_not_replayed_on_resume():
    """A resume reads the journal: a partial entry never serves as completed
    work, so the child is dispatched again — and this time the file exists,
    so the second run answers from the new dispatch."""
    script = _script(f"return await agent('write it', {{ schema: {_FILES_SCHEMA} }});")
    driver, _, _, backend = await _make_driver(
        script,
        behaviors=[_child_answer(["research/out.json"])],
    )

    await driver.run()  # first pass: file missing → partial → null

    resumed, _, dispatcher2, backend2 = await _make_driver(
        script,
        behaviors=[_child_answer(["research/out.json"])],
    )
    resumed.spec.journal_source = driver.base_rel
    # The resume reads the first run's journal through its own backend.
    backend2.files.update(backend.writes)
    backend2.files["research/out.json"] = "written on the retry\n"

    await resumed.run()

    # The partial entry was not replayable: the child was dispatched again.
    assert len(dispatcher2.calls) == 1
    result = json.loads(backend2.writes[f"{resumed.base_rel}/result.json"])
    assert result == {"files": ["research/out.json"], "summary": "done"}


@pytest.mark.asyncio
async def test_a_partial_child_is_error_on_the_wire_and_names_its_files():
    """The user-visible status matches the journal: child_done carries an
    existing non-success status (``error`` — not a timeout, not a schema
    problem) and the error text names the missing deliverables."""
    driver, registry, _, backend = await _make_driver(
        _script(f"return await agent('write it', {{ schema: {_FILES_SCHEMA} }});"),
        behaviors=[_child_answer(["research/knowledge-revision.json"])],
    )

    await driver.run()

    done = [e for e in registry.events if e["phase"] == "child_done"]
    assert done, "a child_done frame must have been emitted"
    assert done[-1]["status"] == "error"
    assert "research/knowledge-revision.json" in str(done[-1]["error"])

    record = json.loads(backend.writes[f"{driver.base_rel}/children/000.json"])
    assert record["status"] == "error"
    assert "research/knowledge-revision.json" in record["error"]


@pytest.mark.asyncio
async def test_a_probe_dispatch_opting_out_of_file_checking_is_healthy():
    """``checkFiles: false`` is the probe contract: a read-only verification
    child that reports files which legitimately do not exist (the branch
    probe's ``path|missing`` lines, the deliverables check's bare paths) is
    an ok dispatch with an ok journal entry, and its answer reaches the
    script whole."""
    driver, _, _, backend = await _make_driver(
        _script(
            "return await agent('verify', { schema: { type: 'object', "
            "properties: { files: { type: 'array', items: { type: 'string' } } }, "
            "required: ['files'] }, checkFiles: false });"
        ),
        behaviors=[
            _child_answer(
                [
                    "./drafts/ok.md|0123456789abcdef",
                    "./research/absent.json",  # reported missing, by design
                ]
            )
        ],
    )

    await driver.run()

    result = json.loads(backend.writes[f"{driver.base_rel}/result.json"])
    assert result is not None and len(result["files"]) == 2
    journal = json.loads(backend.writes[f"{driver.base_rel}/journal/verify.json"])
    (entry,) = journal["dispatches"][-1:]
    assert entry["status"] == "ok"
