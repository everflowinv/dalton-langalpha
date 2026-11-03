"""Workflow driver journal + resume: every dispatch is journaled before and
after, and a resumed run replays a recorded dispatch only while its declared
artifacts still hash the same.

The fakes mirror ``tests/unit/middleware/workflow`` — a recording registry, a
scripted dispatcher, a dict-backed filesystem — so the driver under test is
the real one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from inspect import isawaitable
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from deepagents.backends.protocol import ExecuteResponse
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from ptc_agent.agent.backends.sandbox import SandboxBackend
from ptc_agent.agent.middleware.background_subagent.registry import (
    BackgroundTaskRegistry,
)
from ptc_agent.agent.middleware.background_subagent.workflow.driver import (
    WorkflowDriver,
    WorkflowRunSpec,
)
from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
    compile_check,
)
from src.config.models import WorkflowOrchestrationConfig
from src.dalton.workflows.journal import WorkflowJournal, engine_safe
from tests.unit.middleware.workflow.conftest import FakeBackend, workflow_script


def make_fs_backend(files: dict[str, str] | None = None) -> Any:
    """A spec'd ``SandboxBackend`` serving a dict filesystem whose writes
    read back (the journal's append is read-modify-write) and whose glob
    walks the same store — the two behaviors the journal needs. An absent
    path answers ``FileNotFoundError``, the one read result that says
    missing outright, so the artifact probe's read fallback can see it."""
    backend = create_autospec(SandboxBackend, instance=True)
    store = dict(files or {})

    async def _aread_text(path: str) -> str | None:
        if path not in store:
            raise FileNotFoundError(path)
        return store[path]

    async def _awrite_text(path: str, content: str) -> bool:
        store[path] = content
        return True

    async def _aglob_paths(pattern: str, path: str = ".") -> list[str]:
        return [key for key in store if fnmatch(key, pattern)]

    backend.aread_text.side_effect = _aread_text
    backend.awrite_text.side_effect = _awrite_text
    backend.aglob_paths.side_effect = _aglob_paths
    backend.files = store
    backend.writes = store  # writes ARE the store, so tests read them back
    return backend


def _caps(**overrides: Any) -> WorkflowOrchestrationConfig:
    """The real config object — keys are checked, so a renamed field fails
    the suite instead of becoming an unread attribute."""
    values = WorkflowOrchestrationConfig().model_dump()
    unknown = overrides.keys() - values.keys()
    assert not unknown, f"not WorkflowOrchestrationConfig fields: {sorted(unknown)}"
    values.update(overrides)
    return WorkflowOrchestrationConfig(**values)


@dataclass
class Immediate:
    result: Any
    usage: list[dict[str, Any]] | None = None


def _as_writer_result(result: Any) -> Any:
    inner = result.get("result") if isinstance(result, dict) else None
    if not isinstance(inner, str):
        return result
    message = ToolMessage(content=inner, tool_call_id="child")
    return {**result, "result": Command(update={"messages": [message]})}


class RecordingRegistry(BackgroundTaskRegistry):
    def __init__(self) -> None:
        super().__init__(thread_id="thread-1")
        self.events: list[dict[str, Any]] = []

    async def _spill_records_to_redis(
        self, task: Any, records: list[dict[str, Any]]
    ) -> None:
        self.events.extend(record["data"] for record in records)


@dataclass
class Fail:
    """A child whose writer raises — the dispatch-level failure arm."""

    error: str


def make_dispatcher(
    registry: RecordingRegistry,
    behaviors: list[Immediate | Fail | None] | None = None,
) -> Any:
    """A spec'd ``SubagentDispatcher`` (create_autospec) with the scripted
    child behaviors installed as ``dispatch``'s side effect — the spec keeps
    the call surface honest, the fake keeps the runs hermetic."""
    from ptc_agent.agent.middleware.background_subagent.dispatch import (
        SubagentDispatcher,
    )
    from ptc_agent.agent.middleware.background_subagent.middleware import (
        BackgroundSubagentMiddleware,
    )

    dispatcher = create_autospec(SubagentDispatcher, instance=True)
    middleware = create_autospec(BackgroundSubagentMiddleware, instance=True)
    middleware.registry = registry
    dispatcher.middleware = middleware
    # The real class derives `registry` off middleware; wire it explicitly on
    # the autospec instance, whose property is a plain attribute.
    dispatcher.registry = registry
    dispatcher.subagent_types = ["general-purpose", "dalton-evidence"]
    calls: list[dict[str, Any]] = []
    behaviors = list(behaviors or [])
    registry_ref = registry

    async def _dispatch(**kwargs: Any) -> Any:
        index = len(calls)
        calls.append(kwargs)
        behavior = behaviors[index] if index < len(behaviors) else None
        if behavior is None:
            behavior = Immediate({"success": True, "result": "ok"})
        task = await registry_ref.register(
            tool_call_id=f"child-{index}",
            description=kwargs["description"],
            prompt=kwargs["prompt"],
            subagent_type=kwargs["subagent_type"],
            run_id=kwargs["run_id"],
            owner_task_id=kwargs["owner_task_id"],
        )
        if isinstance(behavior, Fail):
            task.per_call_records = []

            async def _raise() -> Any:
                raise RuntimeError(behavior.error)

            task.asyncio_task = asyncio.create_task(_raise())
            return task
        task.per_call_records = list(behavior.usage or [])

        async def _finish() -> Any:
            return _as_writer_result(behavior.result)

        task.asyncio_task = asyncio.create_task(_finish())
        return task

    dispatcher.dispatch.side_effect = _dispatch
    dispatcher.calls = calls
    return dispatcher


def _script(body: str) -> str:
    return workflow_script(body, name="journal-test", description="journal test")


async def _make_driver(
    backend: FakeBackend | None = None,
    *,
    behaviors: list[Immediate | None] | None = None,
    journal_source: str | None = None,
    progress_path: str | None = None,
) -> tuple[WorkflowDriver, Any, FakeBackend]:
    registry = RecordingRegistry()
    run_task = await registry.register(
        tool_call_id="workflow-run",
        description="journal test",
        prompt="script",
        subagent_type="workflow",
        run_id="launch-1",
    )
    dispatcher = make_dispatcher(registry, behaviors)
    backend = backend or make_fs_backend()
    spec = WorkflowRunSpec(
        run_task=run_task,
        registry=registry,
        dispatcher=dispatcher,
        backend=backend,
        thread_id="thread-1",
        short_thread_id="short-1",
        script=_script("return 1;"),
        meta=compile_check(_script("return 1;")),
        source="inline",
        caps=_caps(),
        journal_source=journal_source,
        progress_path=progress_path,
    )
    driver = WorkflowDriver(spec)
    # The dispatch gate the driver builds in run(); journal tests drive
    # agent() directly, so hand it the semaphore here.
    driver._sem = asyncio.Semaphore(4)
    return driver, dispatcher, backend


def _journal_entry(
    *,
    label: str,
    prompt: str,
    result: Any = None,
    status: str = "ok",
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "label": label,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "status": status,
        "result": result,
        "result_kind": "parsed",
        "artifacts": artifacts or [],
        "replayable": True,
    }


@pytest.mark.asyncio
async def test_workflow_journal_records_pending_then_outcome() -> None:
    """A dispatch writes its intent before the child runs and its outcome —
    with declared artifact digests — after. Extraction is the real
    ``extract_result_content`` (the Command/ToolMessage shape the writer
    produces is exactly what it unwraps)."""
    payload = {"files": ["research/note.md"], "summary": "written"}
    driver, _, backend = await _make_driver(
        make_fs_backend({"research/note.md": "证据" * 100}),
        behaviors=[Immediate({"success": True, "result": json.dumps(payload)})],
    )
    # A schema makes the parsed dict (with `files`) what gets recorded.
    schema = {
        "type": "object",
        "properties": {"files": {"type": "array", "items": {"type": "string"}}},
        "required": ["files"],
    }

    value = await driver.agent("do the work", {"label": "支线", "schema": schema})

    assert value == payload
    journal = json.loads(
        backend.writes[".agents/threads/short-1/workflows/" + driver.wf_task_id + "/journal/支线.json"]
    )
    assert journal["label"] == "支线"
    statuses = [entry["status"] for entry in journal["dispatches"]]
    assert statuses[0] == "pending" and statuses[-1] == "ok"
    outcome = journal["dispatches"][-1]
    assert outcome["result"] == payload
    assert outcome["artifacts"] == [
        {
            "path": "research/note.md",
            "sha256": hashlib.sha256(("证据" * 100).encode("utf-8")).hexdigest(),
        }
    ]


@pytest.mark.asyncio
async def test_workflow_resume_replay_skips_the_dispatch() -> None:
    """A matching journal entry (label + prompt hash + artifact hashes) is
    returned without touching the dispatcher."""
    prompt = "do the work again"
    old_run = "task-old-1"
    backend = make_fs_backend(
        {
            "research/note.md": "证据" * 100,
            f".agents/threads/short-1/workflows/{old_run}/journal/支线.json": json.dumps(
                {
                    "label": "支线",
                    "dispatches": [
                        _journal_entry(
                            label="支线",
                            prompt=prompt,
                            result={"files": ["research/note.md"], "summary": "recorded"},
                            artifacts=[
                                {
                                    "path": "research/note.md",
                                    "sha256": hashlib.sha256(
                                        ("证据" * 100).encode("utf-8")
                                    ).hexdigest(),
                                }
                            ],
                        )
                    ],
                },
                ensure_ascii=False,
            ),
        }
    )
    driver, dispatcher, _ = await _make_driver(
        backend, journal_source=f".agents/threads/short-1/workflows/{old_run}"
    )

    value = await driver.agent(prompt, {"label": "支线"})
    assert value == {"files": ["research/note.md"], "summary": "recorded"}
    assert dispatcher.calls == [], "a replayed dispatch must not reach children"

    # The replay is journaled into the new run, marked with its origin.
    journal = json.loads(
        backend.writes[f".agents/threads/short-1/workflows/{driver.wf_task_id}/journal/支线.json"]
    )
    replay = journal["dispatches"][-1]
    assert replay["status"] == "ok"
    assert replay["resumed_from"] == f".agents/threads/short-1/workflows/{old_run}"


@pytest.mark.asyncio
async def test_workflow_resume_replay_rejects_changed_artifacts() -> None:
    """A file rewritten since the record forces the dispatch again."""
    prompt = "do the work again"
    old_run = "task-old-2"
    stale_digest = hashlib.sha256(b"stale").hexdigest()
    backend = make_fs_backend(
        {
            "research/note.md": "new content that no longer matches",
            f".agents/threads/short-1/workflows/{old_run}/journal/支线.json": json.dumps(
                {
                    "label": "支线",
                    "dispatches": [
                        _journal_entry(
                            label="支线",
                            prompt=prompt,
                            result={"files": ["research/note.md"]},
                            artifacts=[{"path": "research/note.md", "sha256": stale_digest}],
                        )
                    ],
                },
                ensure_ascii=False,
            ),
        }
    )
    driver, dispatcher, _ = await _make_driver(
        backend,
        behaviors=[Immediate({"success": True, "result": "fresh dispatch"})],
        journal_source=f".agents/threads/short-1/workflows/{old_run}",
    )

    value = await driver.agent(prompt, {"label": "支线"})
    assert value == "fresh dispatch"
    assert len(dispatcher.calls) == 1, "a changed artifact must re-dispatch"


@pytest.mark.asyncio
async def test_workflow_resume_replay_ignores_non_ok_entries() -> None:
    """A pending or failed record is never replayed as work."""
    prompt = "never finished"
    old_run = "task-old-3"
    backend = make_fs_backend(
        {
            f".agents/threads/short-1/workflows/{old_run}/journal/支线.json": json.dumps(
                {
                    "label": "支线",
                    "dispatches": [
                        _journal_entry(label="支线", prompt=prompt, status="pending"),
                        _journal_entry(label="支线", prompt=prompt, status="error"),
                    ],
                },
                ensure_ascii=False,
            ),
        }
    )
    driver, dispatcher, _ = await _make_driver(
        backend,
        behaviors=[Immediate({"success": True, "result": "ran for real"})],
        journal_source=f".agents/threads/short-1/workflows/{old_run}",
    )

    value = await driver.agent(prompt, {"label": "支线"})
    assert value == "ran for real"
    assert len(dispatcher.calls) == 1


# ------------------------------------------------- empty-artifact ok answers
# A child whose schema-valid ok answer declares a files/file field but parses
# to no path (the timed-out valuation branch's `{files: [], summary: "未落盘"}`)
# reported writing nothing: the record is never replayable, at write time or
# against a legacy journal, so the branch re-dispatches after the gap is
# filled on disk.

_SCHEMA_FILES = {
    "type": "object",
    "properties": {
        "files": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["files"],
}


@pytest.mark.asyncio
async def test_ok_result_with_empty_files_is_never_replayable() -> None:
    """The journal record for an empty-files ok answer is partial — the child
    reported writing nothing (R3a #5: the branch whose Write the time limit
    refused), so the script gets null and a resume must run the dispatch
    again rather than resurrect the emptiness."""
    empty = {"files": [], "summary": "未落盘，本轮超时"}
    driver, _, backend = await _make_driver(
        make_fs_backend(),
        behaviors=[Immediate({"success": True, "result": json.dumps(empty)})],
    )

    value = await driver.agent("fetch it", {"label": "支线", "schema": _SCHEMA_FILES})

    assert value is None
    journal = json.loads(
        backend.writes[f".agents/threads/short-1/workflows/{driver.wf_task_id}/journal/支线.json"]
    )
    outcome = journal["dispatches"][-1]
    assert outcome["status"] == "partial"
    assert outcome["replayable"] is False
    assert outcome["artifacts"] == []
    assert outcome["missing_artifacts"]


@pytest.mark.asyncio
async def test_legacy_empty_files_entry_re_dispatches_on_resume() -> None:
    """A journal written before the guard (``replayable: true``, no
    artifacts) is still not replayed: the empty claim is judged at read
    time too."""
    prompt = "fetch it again"
    old_run = "task-old-4"
    backend = make_fs_backend(
        {
            f".agents/threads/short-1/workflows/{old_run}/journal/支线.json": json.dumps(
                {
                    "label": "支线",
                    "dispatches": [
                        _journal_entry(
                            label="支线",
                            prompt=prompt,
                            result={"files": [], "summary": "未落盘"},
                            artifacts=[],
                        )
                    ],
                },
                ensure_ascii=False,
            ),
        }
    )
    driver, dispatcher, _ = await _make_driver(
        backend,
        behaviors=[Immediate({"success": True, "result": "ran again"})],
        journal_source=f".agents/threads/short-1/workflows/{old_run}",
    )

    value = await driver.agent(prompt, {"label": "支线"})
    assert value == "ran again"
    assert len(dispatcher.calls) == 1, "an empty-files ok record must re-dispatch"


@pytest.mark.asyncio
async def test_block_fill_resume_chain_reads_the_new_content() -> None:
    """The complete round-2 repair chain, through the real driver, journal
    and ``extract_result_content`` (no substitution):

    1. run 1's branch answers ok with NO files — journaled non-replayable,
       the record a workflow block leaves behind;
    2. the main thread fills the file; a resume re-dispatches the branch and
       the fresh answer pins the NEW content's digest;
    3. later steps read that new content — the bytes on disk, the journal's
       pinned digest and the replayed answer all agree, and content changed
       after the pin refuses the replay.
    """
    prompt = "write the valuation branch"
    schema = _SCHEMA_FILES
    path = "research/valuation.md"
    new_content = "主线程补齐后的估值稿：ln(P1/P0) 分解与可比表 v2"
    filled = {"files": [path], "summary": "written"}

    # Step 1: ok with empty files — a partial record (R3a #5), the block-
    # shaped answer a workflow leaves behind.
    empty = {"files": [], "summary": "未落盘"}
    driver1, dispatcher1, backend1 = await _make_driver(
        make_fs_backend({"research/valuation.md": "占位"}),
        behaviors=[Immediate({"success": True, "result": json.dumps(empty)})],
    )
    value1 = await driver1.agent(prompt, {"label": "估值历史与可比", "schema": schema})
    assert value1 is None
    assert len(dispatcher1.calls) == 1
    journal1_path = (
        f".agents/threads/short-1/workflows/{driver1.wf_task_id}/journal/估值历史与可比.json"
    )
    assert json.loads(backend1.writes[journal1_path])["dispatches"][-1]["replayable"] is False

    # Step 2: the gap is filled; resume re-dispatches and pins the NEW content.
    source1 = f".agents/threads/short-1/workflows/{driver1.wf_task_id}"
    driver2, dispatcher2, backend2 = await _make_driver(
        make_fs_backend(
            {
                path: new_content,
                f"{source1}/journal/估值历史与可比.json": backend1.writes[journal1_path],
            }
        ),
        behaviors=[Immediate({"success": True, "result": json.dumps(filled)})],
        journal_source=source1,
    )
    value2 = await driver2.agent(prompt, {"label": "估值历史与可比", "schema": schema})
    assert value2["files"] == [path]
    assert len(dispatcher2.calls) == 1, "the empty record must not have replayed"
    journal2_path = (
        f".agents/threads/short-1/workflows/{driver2.wf_task_id}/journal/估值历史与可比.json"
    )
    outcome2 = json.loads(backend2.writes[journal2_path])["dispatches"][-1]
    assert outcome2["artifacts"] == [
        {"path": path, "sha256": hashlib.sha256(new_content.encode("utf-8")).hexdigest()}
    ], "the pin is the digest of the NEW content, not just its path"

    # Step 3: later steps read the new content — disk, digest and replay agree.
    assert await backend2.aread_text(path) == new_content
    source2 = f".agents/threads/short-1/workflows/{driver2.wf_task_id}"
    driver3, dispatcher3, backend3 = await _make_driver(
        make_fs_backend(
            {
                path: new_content,
                f"{source2}/journal/估值历史与可比.json": backend2.writes[journal2_path],
            }
        ),
        journal_source=source2,
    )
    value3 = await driver3.agent(prompt, {"label": "估值历史与可比", "schema": schema})
    assert dispatcher3.calls == [], "the pinned, unchanged content replays"
    assert value3 == engine_safe(filled)

    # And the content binding cuts both ways: changed after the pin, the
    # replay is refused and the dispatch runs again.
    mutated = new_content + "\n事后改动"
    driver4, dispatcher4, _ = await _make_driver(
        make_fs_backend(
            {
                path: mutated,
                f"{source2}/journal/估值历史与可比.json": backend2.writes[journal2_path],
            }
        ),
        behaviors=[Immediate({"success": True, "result": json.dumps(filled)})],
        journal_source=source2,
    )
    value4 = await driver4.agent(prompt, {"label": "估值历史与可比", "schema": schema})
    assert len(dispatcher4.calls) == 1, "changed content must re-dispatch"
    assert value4["files"] == [path]


@pytest.mark.asyncio
async def test_workflow_progress_file_tracks_the_run() -> None:
    driver, _, backend = await _make_driver(
        FakeBackend(), progress_path="research/2026-10-23-ic/.progress.json"
    )

    driver.phase("取证")
    await driver._journal.write_progress("running")

    progress = json.loads(backend.writes["research/2026-10-23-ic/.progress.json"])
    assert progress["workflow"] == "journal-test"
    assert progress["run_id"] == driver.wf_task_id
    assert progress["phases"] == ["取证"]
    assert progress["status"] == "running"


def test_workflow_engine_safe_replaces_only_empty_strings() -> None:
    assert engine_safe({"a": "", "b": "x", "c": [0, False, None, "", " "]}) == {
        "a": None,
        "b": "x",
        "c": [0, False, None, None, " "],
    }
    assert engine_safe("") is None
    assert engine_safe("done") == "done"


# --------------------------------------------------------------- tool resume
# The launch-side of resume: the old run must be terminal everywhere the
# ledger can see, and its script snapshot must match what is about to run.


def make_ledger(open_rows: list[dict[str, Any]]) -> Any:
    """A spec'd ``SubagentRunCoordinator`` answering the one query resume
    admission reads (``list_open_workflow_runs``) with real-shaped rows."""
    from src.server.services.subagent_run_coordinator import (
        SubagentRunCoordinator,
    )

    ledger = create_autospec(SubagentRunCoordinator, instance=True)

    async def _rows() -> list[dict[str, Any]]:
        return open_rows

    ledger.list_open_workflow_runs.side_effect = _rows
    return ledger


def make_tool_middleware() -> Any:
    """A spec'd ``BackgroundSubagentMiddleware`` — a ledgered deployment
    always has a checkpointer, which the launch path demands before any
    resume logic runs."""
    from ptc_agent.agent.middleware.background_subagent.middleware import (
        BackgroundSubagentMiddleware,
    )

    middleware = create_autospec(BackgroundSubagentMiddleware, instance=True)
    middleware.registry = RecordingRegistry()
    middleware.checkpointer = object()

    async def _admit_task_run(task: Any, **kwargs: Any) -> str:
        return ""

    middleware.admit_task_run.side_effect = _admit_task_run
    return middleware


def make_tool_dispatcher() -> Any:
    """A spec'd ``SubagentDispatcher`` over the spec'd middleware."""
    from ptc_agent.agent.middleware.background_subagent.dispatch import (
        SubagentDispatcher,
    )

    dispatcher = create_autospec(SubagentDispatcher, instance=True)
    middleware = make_tool_middleware()
    dispatcher.middleware = middleware
    # The real class derives `registry` from middleware; an autospec instance
    # exposes the property as a plain attribute, so wire it explicitly.
    dispatcher.registry = middleware.registry
    dispatcher.subagent_types = ["general-purpose"]
    return dispatcher


class CapturingDriver:
    """Records the spec the launch built and hands back a spec'd driver —
    the launch path only constructs and spawns it, never runs it here."""

    specs: list[Any] = []

    @classmethod
    def capture(cls, spec: Any) -> Any:
        cls.specs.append(spec)
        return create_autospec(WorkflowDriver, instance=True)


def make_prebuilt(workflows: dict[str, str]) -> Any:
    """A spec'd ``PrebuiltWorkflowRegistry`` answering from a dict."""
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        PrebuiltWorkflowRegistry,
    )

    registry = create_autospec(PrebuiltWorkflowRegistry, instance=True)
    registry.get.side_effect = workflows.get
    registry.names.side_effect = lambda: sorted(workflows)
    return registry


def _no_ledger_registry() -> Any:
    """A real registry with no run ledger — the CLI arm of the claim check."""
    registry = RecordingRegistry()
    assert registry.run_ledger is None
    return registry


def _patch_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spec'd pool connection for claim_resume's advisory-lock branch —
    the lock section needs a transaction and a cursor, nothing else."""
    from contextlib import asynccontextmanager

    import psycopg

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)

    async def _execute(*_args: Any, **_kwargs: Any) -> None:
        pass

    cursor.execute.side_effect = _execute
    cursor.__aenter__.return_value = cursor

    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.cursor.return_value = cursor

    @asynccontextmanager
    async def _transaction() -> Any:
        yield conn

    conn.transaction.side_effect = _transaction

    from src.server.database import pool as pool_mod

    @asynccontextmanager
    async def _get_db_connection() -> Any:
        yield conn

    monkeypatch.setattr(pool_mod, "get_db_connection", _get_db_connection)


def _tool_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    CapturingDriver.specs = []
    _patch_pool(monkeypatch)
    tool_module = __import__(
        "ptc_agent.agent.middleware.background_subagent.workflow.tool",
        fromlist=["WorkflowDriver"],
    )
    monkeypatch.setattr(tool_module, "WorkflowDriver", CapturingDriver.capture)

    async def _spawn(mw: Any, task: Any, runner: Any, **kwargs: Any) -> None:
        pass

    monkeypatch.setattr(tool_module, "spawn_task_writer", _spawn)
    monkeypatch.setattr(
        tool_module,
        "get_config",
        lambda: {"metadata": {"run_id": "launch-1"}, "configurable": {"user_id": "user-1"}},
    )
    monkeypatch.setattr(
        "src.config.settings.get_workflow_orchestration_config",
        lambda: WorkflowOrchestrationConfig(),
    )


def _run_dir(task_id: str) -> str:
    return f".agents/threads/short-1/workflows/{task_id}"


@pytest.mark.asyncio
async def test_workflow_resume_refused_while_old_run_still_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two workers must never run the same logical run: an open ledger row
    for the resumed id refuses the launch."""
    _tool_environment(monkeypatch)
    from ptc_agent.agent.middleware.background_subagent.workflow.tool import (
        create_run_workflow_tool,
    )

    script = _script("return 1;")
    backend = FakeBackend({f"{_run_dir('task-old-9')}/workflow.js": script})
    dispatcher = make_tool_dispatcher()
    dispatcher.registry.run_ledger = make_ledger([{"task_id": "task-old-9"}])

    tool = create_run_workflow_tool(
        dispatcher=dispatcher,
        backend=backend,
        thread_id="thread-1",
        short_thread_id="short-1",
        store=None,
        user_id="user-1",
        prebuilt_workflows=make_prebuilt({"journal-test": script}),
    )
    message = await tool.coroutine(workflow="journal-test", params={}, resume="task-old-9")
    assert "still in progress" in message
    assert not CapturingDriver.specs, "a refused resume must start no run"


@pytest.mark.asyncio
async def test_workflow_resume_launches_with_journal_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tool_environment(monkeypatch)
    from ptc_agent.agent.middleware.background_subagent.workflow.tool import (
        create_run_workflow_tool,
    )

    script = _script("return 1;")
    backend = make_fs_backend({f"{_run_dir('task-old-9')}/workflow.js": script})
    dispatcher = make_tool_dispatcher()
    dispatcher.registry.run_ledger = make_ledger([])  # terminal everywhere

    tool = create_run_workflow_tool(
        dispatcher=dispatcher,
        backend=backend,
        thread_id="thread-1",
        short_thread_id="short-1",
        store=None,
        user_id="user-1",
        prebuilt_workflows=make_prebuilt({"journal-test": script}),
    )
    message = await tool.coroutine(
        workflow="journal-test",
        params={"progress_path": "research/2026-10-23-ic/.progress.json"},
        resume="task-old-9",
    )
    assert "started" in message
    spec = CapturingDriver.specs[-1]
    assert spec.journal_source == _run_dir("task-old-9")
    assert spec.progress_path == "research/2026-10-23-ic/.progress.json"


@pytest.mark.asyncio
async def test_workflow_resume_refused_on_script_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old run's launch-time snapshot is the identity of a resume: a
    different script refuses rather than replaying mismatched prompts."""
    _tool_environment(monkeypatch)
    from ptc_agent.agent.middleware.background_subagent.workflow.tool import (
        create_run_workflow_tool,
    )

    script = _script("return 1;")
    backend = make_fs_backend({f"{_run_dir('task-old-9')}/workflow.js": _script("return 2;")})
    dispatcher = make_tool_dispatcher()
    dispatcher.registry.run_ledger = make_ledger([])

    tool = create_run_workflow_tool(
        dispatcher=dispatcher,
        backend=backend,
        thread_id="thread-1",
        short_thread_id="short-1",
        store=None,
        user_id="user-1",
        prebuilt_workflows=make_prebuilt({"journal-test": script}),
    )
    message = await tool.coroutine(workflow="journal-test", params={}, resume="task-old-9")
    assert "workflow.js" in message and "differs" in message
    assert not CapturingDriver.specs


# ------------------------------------------------- round-2 runtime invariants


@pytest.mark.asyncio
async def test_workflow_resume_refused_when_another_run_already_resumes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first resume produced a NEW task id, so "old id still open" cannot
    catch a second resumer: the guard reads each open run's journal for a
    ``resumed_from`` pointing at the same source."""
    _tool_environment(monkeypatch)
    from ptc_agent.agent.middleware.background_subagent.workflow.tool import (
        create_run_workflow_tool,
    )

    script = _script("return 1;")
    old_run = "task-old-9"
    live_resumer = "task-live-1"
    backend = make_fs_backend(
        {
            f"{_run_dir(old_run)}/workflow.js": script,
            f"{_run_dir(live_resumer)}/journal/支线.json": json.dumps(
                {
                    "label": "支线",
                    "dispatches": [
                        {
                            "label": "支线",
                            "status": "ok",
                            "resumed_from": _run_dir(old_run),
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        }
    )
    dispatcher = make_tool_dispatcher()
    dispatcher.registry.run_ledger = make_ledger([{"task_id": live_resumer}])

    tool = create_run_workflow_tool(
        dispatcher=dispatcher,
        backend=backend,
        thread_id="thread-1",
        short_thread_id="short-1",
        store=None,
        user_id="user-1",
        prebuilt_workflows=make_prebuilt({"journal-test": script}),
    )
    message = await tool.coroutine(workflow="journal-test", params={}, resume=old_run)
    assert "already resuming" in message
    assert not CapturingDriver.specs


@pytest.mark.asyncio
async def test_concurrent_dispatches_cannot_exceed_the_cap() -> None:
    """The replay lookup awaits between the admission check and the increment;
    the re-check right before the increment is what holds the cap under
    concurrent agent() calls."""
    driver, dispatcher, _ = await _make_driver(
        make_fs_backend(),
        behaviors=[Immediate({"success": True, "result": "one"})],
    )
    driver.spec.caps.max_dispatches_per_run = 1

    async def _call(tag: str) -> Any:
        try:
            return await driver.agent(f"prompt-{tag}", {"label": f"L-{tag}"})
        except Exception as error:  # WorkflowHostError leaves as an exception here
            return error

    first, second = await asyncio.gather(_call("a"), _call("b"))
    assert not isinstance(first, Exception), first
    assert isinstance(second, Exception), "the second concurrent dispatch must hit the cap"
    assert len(dispatcher.calls) == 1


@pytest.mark.asyncio
async def test_journal_error_is_sanitized_and_clipped() -> None:
    """A failed child's journal entry carries to_record's sanitized, clipped
    error — never the raw upstream exception text (this file is workspace
    data the agent reads)."""
    huge = "x" * 9000
    driver, _, backend = await _make_driver(
        make_fs_backend(), behaviors=[Fail(f"boom {huge}")]
    )
    await driver.agent("explode please", {"label": "支线"})

    journal = json.loads(
        backend.writes[f".agents/threads/short-1/workflows/{driver.wf_task_id}/journal/支线.json"]
    )
    outcome = journal["dispatches"][-1]
    assert outcome["status"] != "ok"
    assert outcome["error"] is not None
    # to_record clips to ~4000 bytes (plus its truncation marker); the raw
    # 9000-char upstream text must not have ridden along.
    assert len(outcome["error"]) < 4100, "the journal error must be the clipped copy"
    assert len(outcome["error"]) < len("boom ") + len(huge)


@pytest.mark.asyncio
async def test_unverifiable_artifact_makes_the_entry_non_replayable() -> None:
    """A child that declares a file that is not on disk is a partial
    dispatch (R3a #5): journaled partial and non-replayable — a later resume
    re-dispatches rather than trusting a pin of nothing."""
    payload = {"files": ["research/gone.md"], "summary": "declared"}
    backend = make_fs_backend()  # no research/gone.md on disk
    driver, dispatcher, _ = await _make_driver(
        backend, behaviors=[Immediate({"success": True, "result": json.dumps(payload)})]
    )
    schema = {
        "type": "object",
        "properties": {"files": {"type": "array", "items": {"type": "string"}}},
        "required": ["files"],
    }

    import ptc_agent.agent.middleware.background_subagent.workflow.driver as driver_mod

    def _extract(result: Any) -> tuple[bool, str]:
        return True, json.dumps(payload)

    original = driver_mod.extract_result_content
    driver_mod.extract_result_content = _extract
    try:
        value = await driver.agent("declare and fail to hash", {"label": "支线", "schema": schema})
    finally:
        driver_mod.extract_result_content = original
    assert value is None

    journal = json.loads(
        backend.writes[f".agents/threads/short-1/workflows/{driver.wf_task_id}/journal/支线.json"]
    )
    outcome = journal["dispatches"][-1]
    assert outcome["status"] == "partial"
    assert outcome["replayable"] is False
    assert outcome["artifacts"] == []

    # And a resume of this run re-dispatches instead of replaying it.
    replay = await driver._journal.find_replayable("支线", "declare and fail to hash")
    assert replay is None


def test_declared_artifact_paths_reads_both_spelling() -> None:
    """Children declare artifacts as plural ``files`` (paths, ``path|sha``
    pairs, objects) or the singular ``file`` of a single-deliverable child —
    the journal reads both, pipe payloads by their path half."""
    from src.dalton.workflows.journal import declared_artifact_paths

    assert declared_artifact_paths(
        {"files": ["a.md|deadbeef", "b.md", {"path": "c.md"}], "summary": "s"}
    ) == ["a.md", "b.md", "c.md"]
    assert declared_artifact_paths({"file": "plan.md"}) == ["plan.md"]
    assert declared_artifact_paths({"file": {"path": "screen.json"}}) == ["screen.json"]
    assert declared_artifact_paths({"summary": "no declaration"}) == []
    assert declared_artifact_paths("plain text") == []


# ------------------------------------------------- round-3 resume invariants


@pytest.mark.asyncio
async def test_second_resume_refused_even_with_zero_replay_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim lands at admission, not at the first replayed dispatch: the
    first resume may not have dispatched anything yet (the driver here never
    runs), and the second resume of the same old run is still refused."""
    _tool_environment(monkeypatch)
    from ptc_agent.agent.middleware.background_subagent.workflow.tool import (
        create_run_workflow_tool,
    )

    script = _script("return 1;")
    old_run = "task-old-9"
    backend = make_fs_backend({f"{_run_dir(old_run)}/workflow.js": script})
    dispatcher = make_tool_dispatcher()
    dispatcher.registry.run_ledger = make_ledger([])

    tool = create_run_workflow_tool(
        dispatcher=dispatcher,
        backend=backend,
        thread_id="thread-1",
        short_thread_id="short-1",
        store=None,
        user_id="user-1",
        prebuilt_workflows=make_prebuilt({"journal-test": script}),
    )
    first = await tool.coroutine(workflow="journal-test", params={}, resume=old_run)
    assert "started" in first
    # Zero journal entries exist for the new run — no dispatch ever replayed.
    markers = [p for p in backend.writes if p.endswith("/resume.json")]
    assert markers, "the claim marker must land at admission"
    assert old_run in backend.writes[markers[0]]

    # The first resume's run is now the one open run in the ledger; a second
    # resume of the same source is refused on the marker alone.
    new_task_id = CapturingDriver.specs[-1].run_task.task_id

    async def _open_rows() -> list[dict[str, Any]]:
        return [{"task_id": new_task_id}]

    dispatcher.registry.run_ledger.list_open_workflow_runs.side_effect = _open_rows
    message = await tool.coroutine(workflow="journal-test", params={}, resume=old_run)
    assert "already resuming" in message, message


@pytest.mark.asyncio
async def test_replayed_entries_stay_replayable_across_two_interruptions() -> None:
    """A replayed dispatch is journaled into the new run with its artifact
    pins and replayable=true, so a second interruption resumes off the second
    run's journal — the chain never degrades."""
    prompt = "the same work"
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    async def _journal_for(source: str | None) -> tuple[WorkflowJournal, FakeBackend]:
        backend = make_fs_backend(
            {"research/note.md": "证据" * 100}
            if source is None
            else {
                "research/note.md": "证据" * 100,
                f"{source}/journal/支线.json": json.dumps(
                    {
                        "label": "支线",
                        "dispatches": [
                            {
                                "label": "支线",
                                "prompt_sha256": digest,
                                "status": "ok",
                                "result": {"files": ["research/note.md"]},
                                "result_kind": "parsed",
                                "artifacts": [
                                    {
                                        "path": "research/note.md",
                                        "sha256": hashlib.sha256(
                                            ("证据" * 100).encode("utf-8")
                                        ).hexdigest(),
                                    }
                                ],
                                "replayable": True,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
            }
        )
        journal = WorkflowJournal(
            backend,
            base_rel=".agents/threads/short-1/workflows/run-current",
            run_id="run-current",
            workflow="w",
            journal_source=source,
        )
        return journal, backend

    # First interruption: run-1 replays from the original run.
    original = ".agents/threads/short-1/workflows/run-original"
    j1, b1 = await _journal_for(original)
    entry = await j1.find_replayable("支线", prompt)
    assert entry is not None
    await j1.record_replay(entry, seq=0, label="支线", phase=None)
    # Run-1's journal now carries the replay with its pins and qualification.
    replay_doc = json.loads(b1.writes[".agents/threads/short-1/workflows/run-current/journal/支线.json"])
    replay = replay_doc["dispatches"][-1]
    assert replay["replayable"] is True
    assert replay["artifacts"], "the replay must carry its artifact pins"

    # Second interruption: run-2 resumes off run-1's journal and replays
    # again — same label+prompt, artifacts unchanged.
    run1_dir = ".agents/threads/short-1/workflows/run-current"
    b1.files[run1_dir + "/journal/支线.json"] = b1.writes[run1_dir + "/journal/支线.json"]
    j2 = WorkflowJournal(
        b1,
        base_rel=".agents/threads/short-1/workflows/run-second",
        run_id="run-second",
        workflow="w",
        journal_source=run1_dir,
    )
    entry2 = await j2.find_replayable("支线", prompt)
    assert entry2 is not None, "the second resume must still replay the dispatch"
    assert entry2["result"] == {"files": ["research/note.md"]}


# ------------------------------------------------- round-4 invariants


@pytest.mark.asyncio
async def test_commands_run_in_the_workspace_folder() -> None:
    """The backend's aexecute runs at the computer root; every command the
    journal issues must carry the cd-into-workspace prefix, or its
    workspace-relative paths resolve against the wrong directory."""
    from src.dalton.workflows.journal import digest_paths

    backend = make_fs_backend({"research/note.md": "x" * 50})
    backend.root_dir = "/computer-root"
    backend.workspace_dir = "workspaces/META"
    await digest_paths(backend, ["research/note.md"])
    command = backend.aexecute.call_args.args[0]
    assert command.startswith("cd workspaces/META && "), command


@pytest.mark.asyncio
async def test_check_done_commands_run_in_the_workspace_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.tools import check_done_tools

    backend = _make_backend_probe()
    _patch_knowledge_ok(monkeypatch)
    _patch_verdicts_ok(monkeypatch)
    await check_done_tools.run_check_done(
        workflow="initial-coverage",
        params={"ticker": "META", "workspace_dir": ".", "as_of": "2026-10-23"},
        backend=backend,
        workspace_id="ws-1",
    )
    commands = [call.args[0] for call in backend.aexecute.call_args_list]
    assert commands
    for command in commands:
        assert command.startswith("cd workspaces/META && "), command


def _make_backend_probe() -> Any:
    """A spec'd backend whose root_dir and workspace_dir differ, serving the
    full initial-coverage artifact set (mirrors test_check_done fixtures)."""
    from tests.unit.dalton.test_check_done import _full_files

    backend = create_autospec(SandboxBackend, instance=True)
    files = _full_files()
    import time as _time

    async def _aread_text(path: str) -> str | None:
        return files.get(path)

    async def _aglob(pattern: str, path: str = ".") -> list[str]:
        from fnmatch import fnmatch as _fn

        return [k for k in files if _fn(k, pattern)]

    async def _aexecute(command: str, *, timeout: int | None = None) -> Any:
        class _R:
            def __init__(self, out: str, code: int) -> None:
                self.output, self.exit_code = out, code

        body = command.removeprefix("cd workspaces/META && ")
        if body.startswith("wc -c"):
            target = files.get(body.removeprefix("wc -c < ").strip())
            return _R("0" if target is None else str(len(target.encode())), 0 if target else 1)
        if body.startswith("stat -c %Y"):
            return _R(str(int(_time.time())), 0)
        return _R("ok", 0)

    backend.aread_text.side_effect = _aread_text
    backend.aglob_paths.side_effect = _aglob
    backend.aexecute.side_effect = _aexecute
    backend.root_dir = "/computer-root"
    backend.workspace_dir = "workspaces/META"
    return backend


def _patch_knowledge_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.dalton.test_check_done import IC_DOCS, _patch_knowledge

    _patch_knowledge(monkeypatch, IC_DOCS)


def _patch_verdicts_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.dalton.test_check_done import _patch_verdicts, _verdict_row

    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])


@pytest.mark.asyncio
async def test_resume_marker_read_failure_refuses_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside the lock, an OPEN run's marker that cannot be read is a
    refusal: the read was attempted, and no new marker was written."""
    from src.dalton.workflows.journal import ResumeRefused, claim_resume

    # One open run whose marker read explodes on a spec'd backend.
    backend = create_autospec(SandboxBackend, instance=True)

    async def _aread_text(path: str) -> str | None:
        if path.endswith("resume.json"):
            raise OSError("transport glitch")
        return None

    backend.aread_text.side_effect = _aread_text

    registry = _no_ledger_registry()
    await registry.register(
        tool_call_id="live-open",
        description="live",
        prompt="p",
        subagent_type="workflow",
        run_id=None,
    )
    # A no-ledger registry exposes pending tasks via get_all_tasks — the
    # registered workflow task above is exactly that.

    _patch_pool(monkeypatch)
    with pytest.raises(ResumeRefused, match="refuse to resume"):
        await claim_resume(
            backend,
            registry,
            source_dir=".agents/threads/short-1/workflows/old",
            resume="old",
            marker_path=".agents/threads/short-1/workflows/new/resume.json",
            run_dir_for=lambda task_id: f".agents/threads/short-1/workflows/{task_id}",
        )
    backend.aread_text.assert_called()
    backend.awrite_text.assert_not_called()


@pytest.mark.asyncio
async def test_resume_after_admission_failure_aborts_the_admitted_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim refusal lands AFTER the ledger row exists — the cleanup must
    settle it through `_abort_admitted_run` (fence released, row terminal),
    never `discard_unstarted`, which refuses admitted runs."""
    _tool_environment(monkeypatch)
    import src.dalton.workflows.journal as journal_mod
    from ptc_agent.agent.middleware.background_subagent.workflow.tool import (
        create_run_workflow_tool,
    )

    script = _script("return 1;")
    backend = make_fs_backend({f"{_run_dir('task-old-9')}/workflow.js": script})
    dispatcher = make_tool_dispatcher()
    dispatcher.registry.run_ledger = make_ledger([])

    async def _refuse(*_args: Any, **_kwargs: Any) -> None:
        raise journal_mod.ResumeRefused("cannot resume: already resuming")

    monkeypatch.setattr(journal_mod, "claim_resume", _refuse)
    tool = create_run_workflow_tool(
        dispatcher=dispatcher,
        backend=backend,
        thread_id="thread-1",
        short_thread_id="short-1",
        store=None,
        user_id="user-1",
        prebuilt_workflows=make_prebuilt({"journal-test": script}),
    )
    message = await tool.coroutine(workflow="journal-test", params={}, resume="task-old-9")
    assert "cannot resume" in message
    dispatcher.middleware._abort_admitted_run.assert_awaited()


# ------------------------------------------------- round-6 invariants


@pytest.mark.asyncio
async def test_marker_existence_decided_by_probe_not_path_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """aglob answers in ABSOLUTE paths while the marker path is relative —
    the membership check could never fire. Existence now comes from a
    workspace probe: aread_text None + exists → refusal."""
    from src.dalton.workflows.journal import ResumeRefused, _read_marker

    backend = create_autospec(SandboxBackend, instance=True)

    async def _aread_text(path: str) -> str | None:
        return None  # empty file / non-UTF-8 on the generic layer

    async def _aglob(pattern: str, path: str = ".") -> list[str]:
        # Production shape: absolute paths, so a naive membership check fails.
        return [f"/computer-root/workspaces/META/{pattern}"]

    class _Response:
        output = ""
        exit_code = 0  # test -e: the marker IS there

    async def _aexecute(command: str, *, timeout: int | None = None) -> Any:
        assert command.startswith("cd "), "the probe must cd into the workspace"
        assert "__EXISTS__" in command and "__ABSENT__" in command

        class _Sentinel:
            output = "__EXISTS__"  # test -e: the marker IS there
            exit_code = 0

        return _Sentinel()

    backend.aread_text.side_effect = _aread_text
    backend.aglob_paths.side_effect = _aglob
    backend.aexecute.side_effect = _aexecute
    backend.workspace_dir = "workspaces/META"

    with pytest.raises(ResumeRefused, match="unreadable"):
        await _read_marker(backend, ".agents/threads/s/workflows/r/resume.json")
    backend.aexecute.assert_called()


@pytest.mark.asyncio
async def test_resume_index_glob_failure_refuses_instead_of_empty() -> None:
    """An unlistable source journal must refuse — an empty index would
    re-dispatch every step the dead run already finished."""
    from src.dalton.workflows.journal import ResumeRefused, WorkflowJournal

    backend = create_autospec(SandboxBackend, instance=True)

    async def _aglob(pattern: str, path: str = ".") -> list[str]:
        raise OSError("listing unavailable")

    backend.aglob_paths.side_effect = _aglob
    journal = WorkflowJournal(
        backend,
        base_rel=".agents/threads/s/workflows/now",
        run_id="now",
        workflow="w",
        journal_source=".agents/threads/s/workflows/old",
    )
    with pytest.raises(ResumeRefused, match="refuse to resume"):
        await journal._load_resume_index()


@pytest.mark.asyncio
async def test_listed_journal_file_read_failure_refuses() -> None:
    """journal_entries lists files but one read fails → None → admission
    refuses (never a silent skip that reads as 'no entries')."""
    from src.dalton.workflows.journal import journal_entries

    backend = create_autospec(SandboxBackend, instance=True)

    async def _aglob(pattern: str, path: str = ".") -> list[str]:
        return [".agents/threads/s/workflows/old/journal/a.json"]

    async def _aread_text(path: str) -> str | None:
        raise OSError("transport glitch")

    backend.aglob_paths.side_effect = _aglob
    backend.aread_text.side_effect = _aread_text
    from src.dalton.workflows.journal import ResumeRefused

    with pytest.raises(ResumeRefused, match="journal file read failed"):
        await journal_entries(backend, ".agents/threads/s/workflows/old")


@pytest.mark.asyncio
async def test_listed_journal_none_readable_returns_none() -> None:
    """Files listed, not one parses as a journal — 'cannot verify', not
    'nothing recorded'."""
    from src.dalton.workflows.journal import journal_entries

    backend = create_autospec(SandboxBackend, instance=True)

    async def _aglob(pattern: str, path: str = ".") -> list[str]:
        return [
            ".agents/threads/s/workflows/old/journal/a.json",
            ".agents/threads/s/workflows/old/journal/b.json",
        ]

    async def _aread_text(path: str) -> str | None:
        return None  # present-but-unreadable shape

    async def _aexecute(command: str, *, timeout: int | None = None) -> Any:
        class _Response:
            output = ""
            exit_code = 1  # test -e: absent after all

        return _Response()

    backend.aglob_paths.side_effect = _aglob
    backend.aread_text.side_effect = _aread_text
    backend.aexecute.side_effect = _aexecute
    from src.dalton.workflows.journal import ResumeRefused

    # Item 3: ONE listed file that cannot be read is a refusal — there is no
    # aggregate "none readable" outcome any more.
    with pytest.raises(ResumeRefused, match="unreadable"):
        await journal_entries(backend, ".agents/threads/s/workflows/old")


# ------------------------------------------------- round-7 invariants


@pytest.mark.asyncio
async def test_probe_nonzero_exit_refuses() -> None:
    """The sentinel probe's command fails (exit_code=1, e.g. a failed cd):
    a read failure, never a pass as 'absent'."""
    from src.dalton.workflows.journal import ResumeRefused, _probe_exists

    backend = create_autospec(SandboxBackend, instance=True)

    async def _aexecute(command: str, *, timeout: int | None = None) -> Any:
        return ExecuteResponse(output="", exit_code=1, truncated=False)

    backend.aexecute.side_effect = _aexecute
    backend.workspace_dir = "workspaces/META"
    with pytest.raises(ResumeRefused, match="exited 1|probe failed"):
        await _probe_exists(backend, ".agents/threads/s/workflows/r/resume.json")


@pytest.mark.asyncio
async def test_probe_missing_sentinel_refuses() -> None:
    """Exit 0 but no sentinel in the output (a mangled echo): refusal."""
    from src.dalton.workflows.journal import ResumeRefused, _probe_exists

    backend = create_autospec(SandboxBackend, instance=True)

    async def _aexecute(command: str, *, timeout: int | None = None) -> Any:
        return ExecuteResponse(output="", exit_code=0, truncated=False)

    backend.aexecute.side_effect = _aexecute
    with pytest.raises(ResumeRefused, match="no sentinel"):
        await _probe_exists(backend, "x/resume.json")


@pytest.mark.asyncio
async def test_one_unreadable_journal_file_among_readable_refuses() -> None:
    """Two listed files, one reads fine and one answers None: the resume
    refuses on the single unreadable file."""
    from src.dalton.workflows.journal import ResumeRefused, journal_entries

    backend = create_autospec(SandboxBackend, instance=True)

    async def _aglob(pattern: str, path: str = ".") -> list[str]:
        return [
            ".agents/threads/s/workflows/old/journal/ok.json",
            ".agents/threads/s/workflows/old/journal/bad.json",
        ]

    good = json.dumps({"label": "支线", "dispatches": [{"label": "支线", "status": "ok"}]})

    async def _aread_text(path: str) -> str | None:
        if path.endswith("bad.json"):
            return None  # present-but-unreadable shape
        return good

    backend.aglob_paths.side_effect = _aglob
    backend.aread_text.side_effect = _aread_text
    with pytest.raises(ResumeRefused, match="bad.json"):
        await journal_entries(backend, ".agents/threads/s/workflows/old")


@pytest.mark.asyncio
async def test_resume_index_single_unreadable_file_refuses() -> None:
    """The replay-index loader applies the same rule: one unreadable listed
    file refuses the resume instead of skipping it."""
    from src.dalton.workflows.journal import ResumeRefused, WorkflowJournal

    backend = create_autospec(SandboxBackend, instance=True)

    async def _aglob(pattern: str, path: str = ".") -> list[str]:
        return [".agents/threads/s/workflows/old/journal/a.json"]

    async def _aread_text(path: str) -> str | None:
        return None  # unreadable

    backend.aglob_paths.side_effect = _aglob
    backend.aread_text.side_effect = _aread_text
    journal = WorkflowJournal(
        backend,
        base_rel=".agents/threads/s/workflows/now",
        run_id="now",
        workflow="w",
        journal_source=".agents/threads/s/workflows/old",
    )
    with pytest.raises(ResumeRefused, match="unreadable"):
        await journal._load_resume_index()


@pytest.mark.asyncio
async def test_state_query_results_are_never_replayable() -> None:
    """The final-model resolver answers {path, sha256} about the disk *now*:
    its journal entry must not be replayed, or a version saved after the
    interruption would be missed on resume."""
    from src.dalton.workflows.journal import artifact_records

    backend = create_autospec(SandboxBackend, instance=True)

    assert await artifact_records(backend, {"path": "model/META-model-v02.xlsx", "sha256": "a" * 64}) is None
    backend.aread_text.assert_not_called()


@pytest.mark.asyncio
async def test_schema_retry_success_is_journaled_under_the_original_prompt() -> None:
    """A child whose first answer fails the schema and whose retry succeeds is
    journaled under the logical call's prompt, so a resume (which looks up by
    that prompt) can replay it instead of dispatching the write again."""
    good = {"files": ["research/note.md"], "summary": "written"}
    driver, dispatcher, backend = await _make_driver(
        make_fs_backend({"research/note.md": "证据" * 100}),
        behaviors=[
            Immediate({"success": True, "result": "not json"}),
            Immediate({"success": True, "result": json.dumps(good)}),
        ],
    )
    schema = {
        "type": "object",
        "properties": {"files": {"type": "array", "items": {"type": "string"}}},
        "required": ["files"],
    }
    answers = iter([(True, "not json"), (True, json.dumps(good))])

    def fake_extract(result: Any) -> tuple[bool, str]:
        return next(answers)

    import ptc_agent.agent.middleware.background_subagent.workflow.driver as driver_mod

    original = driver_mod.extract_result_content
    driver_mod.extract_result_content = fake_extract
    try:
        value = await driver.agent("do the work", {"label": "支线", "schema": schema})
    finally:
        driver_mod.extract_result_content = original

    assert value == good
    assert len(dispatcher.calls) == 2, "the first answer must have been retried"
    journal = json.loads(
        backend.writes[".agents/threads/short-1/workflows/" + driver.wf_task_id + "/journal/支线.json"]
    )
    ok_entries = [entry for entry in journal["dispatches"] if entry["status"] == "ok"]
    assert ok_entries, journal
    # Every record of this call (first pending, failed first answer, retried
    # success) carries the one key resume looks up — the validated logical
    # prompt — never the retry prompt with its schema-error preamble.
    keys = {entry["prompt_sha256"] for entry in journal["dispatches"]}
    assert len(keys) == 1, journal["dispatches"]


# ------------------------------------------- checkpoint replay of a resume call
# The parent turn re-executes the SAME RunWorkflow(resume=...) call from a
# checkpoint (same tool_call_id) after the run it started has claimed the
# resume and replayed dispatches: it reconnects to that run instead of being
# refused as "another resumer".


def _resume_tool(backend: Any, dispatcher: Any, script: str) -> Any:
    from ptc_agent.agent.middleware.background_subagent.workflow.tool import (
        create_run_workflow_tool,
    )

    return create_run_workflow_tool(
        dispatcher=dispatcher,
        backend=backend,
        thread_id="thread-1",
        short_thread_id="short-1",
        store=None,
        user_id="user-1",
        prebuilt_workflows=make_prebuilt({"journal-test": script}),
    )


@pytest.mark.asyncio
async def test_checkpoint_reexecution_of_same_resume_call_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tool_environment(monkeypatch)
    script = _script("return 1;")
    old_run = "task-old-9"
    backend = make_fs_backend({f"{_run_dir(old_run)}/workflow.js": script})
    dispatcher = make_tool_dispatcher()
    dispatcher.registry.run_ledger = make_ledger([])
    tool = _resume_tool(backend, dispatcher, script)

    first = await tool.coroutine(
        workflow="journal-test", params={}, resume=old_run, tool_call_id="call-resume-1"
    )
    first_text = first.content if isinstance(first, ToolMessage) else first
    assert "started" in str(first_text)
    new_task = CapturingDriver.specs[-1].run_task
    marker = json.loads(backend.files[f"{_run_dir(new_task.task_id)}/resume.json"])
    assert marker["tool_call_id"] == "call-resume-1"
    assert marker["task_id"] == new_task.task_id

    # The started run is now open in the ledger and has replayed a dispatch
    # from the old run — exactly what a competing resumer would look like.
    backend.files[f"{_run_dir(new_task.task_id)}/journal/支线.json"] = json.dumps(
        {
            "label": "支线",
            "dispatches": [{"label": "支线", "status": "ok", "resumed_from": _run_dir(old_run)}],
        },
        ensure_ascii=False,
    )

    async def _open_rows() -> list[dict[str, Any]]:
        return [{"task_id": new_task.task_id}]

    dispatcher.registry.run_ledger.list_open_workflow_runs.side_effect = _open_rows

    again = await tool.coroutine(
        workflow="journal-test", params={}, resume=old_run, tool_call_id="call-resume-1"
    )
    again_text = str(again.content if isinstance(again, ToolMessage) else again)
    assert "already active" in again_text, again_text
    assert new_task.display_id in again_text
    assert "already resuming" not in again_text
    assert len(CapturingDriver.specs) == 1, "the re-execution must not start a second run"

    # A DIFFERENT call resuming the same old run is still refused.
    other = await tool.coroutine(
        workflow="journal-test", params={}, resume=old_run, tool_call_id="call-resume-2"
    )
    other_text = str(other.content if isinstance(other, ToolMessage) else other)
    assert "already resuming" in other_text, other_text
    assert len(CapturingDriver.specs) == 1


@pytest.mark.asyncio
async def test_claim_by_same_claimant_is_not_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-worker replay of the same call: the open run's marker names the
    same tool_call_id, so neither the source check nor the claim refuses —
    while a different claimant is refused on the same marker."""
    from src.dalton.workflows.journal import (
        ResumeRefused,
        claim_resume,
        resolve_resume_source,
    )

    script = _script("return 1;")
    old_dir = _run_dir("task-old-9")
    live_dir = _run_dir("task-live-1")
    backend = make_fs_backend(
        {
            f"{old_dir}/workflow.js": script,
            f"{live_dir}/resume.json": json.dumps(
                {"resumed_from": old_dir, "claimed_at": 1.0, "tool_call_id": "call-a", "task_id": "task-live-1"}
            ),
            f"{live_dir}/journal/支线.json": json.dumps(
                {"label": "支线", "dispatches": [{"label": "支线", "status": "ok", "resumed_from": old_dir}]},
                ensure_ascii=False,
            ),
        }
    )
    registry = RecordingRegistry()
    registry.run_ledger = make_ledger([{"task_id": "task-live-1"}])
    _patch_pool(monkeypatch)
    common = {"source_dir": old_dir, "resume": "task-old-9", "run_dir_for": _run_dir}

    assert (
        await resolve_resume_source(backend, registry, script=script, claimant="call-a", **common)
        == old_dir
    )
    await claim_resume(
        backend,
        registry,
        marker_path=f"{_run_dir('task-new-2')}/resume.json",
        claimant="call-a",
        task_id="task-new-2",
        **common,
    )

    with pytest.raises(ResumeRefused, match="already resuming"):
        await resolve_resume_source(backend, registry, script=script, claimant="call-b", **common)
    with pytest.raises(ResumeRefused, match="already resuming"):
        await claim_resume(
            backend,
            registry,
            marker_path=f"{_run_dir('task-new-3')}/resume.json",
            claimant="call-b",
            task_id="task-new-3",
            **common,
        )


# ------------------------------------------------- round 4: the real workflow.js
# The complete repair chain with the shipped script: the engine runs
# workflows/initial-coverage/workflow.js with the real driver as host —
# journal, replay admission and dispatch all live — and a spec'd dispatcher
# answers children BY LABEL (the test_workflows_js.py CapturingHost shape,
# one level down, where the child also writes what it declares).

REPO_ROOT = Path(__file__).resolve().parents[3]

_GENERIC_OUT = {
    "files": ["research/generic.md"],
    "summary": "done",
    "sha256": "a" * 64,
    "underwriting_version": 1,
}

# The default child must be as honest as a real one: it declares
# research/generic.md, so it writes it too (R3a #5 made a declared-but-
# unwritten file a partial dispatch, not an ok answer). Built per dispatch —
# ``Writes`` is defined further down this module.
def _generic_writes(_prompt, _backend):
    files = list(_GENERIC_OUT["files"])
    uw_rel = "./research/2026-10-23-ic/underwriting-summary.md"
    if uw_rel in getattr(_backend, "files", {}):
        files.append(uw_rel)
    return Writes(
        _ok({**_GENERIC_OUT, "files": files}),
        {"research/generic.md": "generic 产物"},
    )


@dataclass
class Writes:
    """A child answer that also writes files into the store first — what a
    real evidence child does before returning its JSON."""

    result: dict[str, Any]
    writes: dict[str, str] = field(default_factory=dict)


def _ok(payload: Any) -> dict[str, Any]:
    return {"success": True, "result": json.dumps(payload, ensure_ascii=False)}


def _probe_answer(prompt: str, backend: Any = None) -> Immediate:
    """The 支线核验 child: every probed file is on disk, one ``path|ok`` per
    ``- path（至少 N 字节）`` prompt line."""
    paths = [
        line[2:].split("（")[0].strip()
        for line in prompt.splitlines()
        if line.startswith("- ") and "|" not in line
    ]
    return Immediate(_ok({"files": [f"{path}|ok" for path in paths]}))


def make_label_dispatcher(
    registry: RecordingRegistry,
    backend: Any,
    by_label: dict[str, Any],
    *,
    default: Any,
) -> Any:
    """A spec'd ``SubagentDispatcher`` answering by the dispatch's label;
    callable behaviors receive ``(prompt, backend)`` — sync or async — and
    ``Writes`` behaviors write into the backend before answering."""
    from ptc_agent.agent.middleware.background_subagent.dispatch import (
        SubagentDispatcher,
    )
    from ptc_agent.agent.middleware.background_subagent.middleware import (
        BackgroundSubagentMiddleware,
    )

    dispatcher = create_autospec(SubagentDispatcher, instance=True)
    middleware = create_autospec(BackgroundSubagentMiddleware, instance=True)
    middleware.registry = registry
    dispatcher.middleware = middleware
    dispatcher.registry = registry
    dispatcher.subagent_types = ["general-purpose", "dalton-evidence", "dalton-reviewer"]
    calls: list[dict[str, Any]] = []

    async def _dispatch(**kwargs: Any) -> Any:
        label = str(kwargs.get("description") or "")
        calls.append(kwargs)
        behavior = by_label.get(label, default)
        if callable(behavior) and not isinstance(behavior, Immediate):
            called = behavior(str(kwargs["prompt"]), backend)
            if isawaitable(called):
                called = await called
            behavior = called
        assert behavior is not None, f"unexpected dispatch label: {label}"
        payload = behavior.result
        if isinstance(behavior, Writes):
            for path, content in behavior.writes.items():
                await backend.awrite_text(path, content)
        task = await registry.register(
            tool_call_id=f"child-{len(calls)}",
            description=label,
            prompt=kwargs["prompt"],
            subagent_type=kwargs["subagent_type"],
            run_id=kwargs["run_id"],
        )
        task.per_call_records = []

        async def _finish() -> Any:
            return _as_writer_result(payload)

        task.asyncio_task = asyncio.create_task(_finish())
        return task

    dispatcher.dispatch.side_effect = _dispatch
    dispatcher.calls = calls
    return dispatcher


async def _drive_workflow(
    store: dict[str, str],
    by_label: dict[str, Any],
    *,
    default: Any,
    journal_source: str | None = None,
) -> tuple[Any, WorkflowDriver, Any, Any]:
    """Run the shipped initial-coverage script through the real engine with
    the real driver as host; children are answered by ``by_label``."""
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
        WorkflowLimits,
        run_workflow_script,
    )

    registry = RecordingRegistry()
    run_task = await registry.register(
        tool_call_id="workflow-run",
        description="chain test",
        prompt="script",
        subagent_type="workflow",
        run_id="launch-1",
    )
    backend = make_fs_backend(store)
    script = (REPO_ROOT / "workflows" / "initial-coverage" / "workflow.js").read_text(
        encoding="utf-8"
    )
    dispatcher = make_label_dispatcher(registry, backend, by_label, default=default)
    spec = WorkflowRunSpec(
        run_task=run_task,
        registry=registry,
        dispatcher=dispatcher,
        backend=backend,
        thread_id="thread-1",
        short_thread_id="short-1",
        script=script,
        meta=compile_check(script),
        source="inline",
        caps=_caps(),
        journal_source=journal_source,
    )
    driver = WorkflowDriver(spec)
    driver._sem = asyncio.Semaphore(8)
    spec_host = create_autospec(WorkflowHost, instance=True)
    spec_host.agent.side_effect = driver.agent
    spec_host.phase.side_effect = driver.phase
    spec_host.log.side_effect = driver.log
    outcome = await run_workflow_script(
        script,
        {
            "ticker": "META",
            "company": "Meta",
            "workspace_dir": ".",
            "as_of": "2026-10-23",
        },
        spec_host,
        WorkflowLimits(memory_limit_mb=64, cpu_budget_s=60.0),
    )
    return outcome, driver, dispatcher, backend


def _journal_entry_of(backend: Any, driver: WorkflowDriver, label: str) -> dict[str, Any]:
    doc = json.loads(
        backend.writes[f".agents/threads/short-1/workflows/{driver.wf_task_id}/journal/{label}.json"]
    )
    return doc["dispatches"][-1]


@pytest.mark.asyncio
async def test_real_script_block_fill_resume_chain() -> None:
    """Four steps against the real workflow.js, journal and driver:

    1. the valuation fetch branch answers ok with EMPTY files → the workflow
       blocks before the draft;
    2. the main thread fills valuation-data.json;
    3. the same params with resume re-dispatch exactly the failed branch —
       its re-run writes NEW content (v2), different from the manual fill;
    4. the write stage downstream reads the RE-DISPATCHED content — it parses
       its input path off the prompt the workflow sends and reads that file
       through the run's backend, quoting what it actually read (a wrong
       path in the prompt fails the chain), never the manual fill.
    """
    store: dict[str, str] = {}
    bg_path = "./drafts/2026-10-23-background.md"
    ind_path = "./research/2026-10-23-ic/industry.md"
    lens_json = "./research/2026-10-23-ic/lenses.json"
    lens_md = "./drafts/2026-10-23-lenses.md"
    data_path = "./research/2026-10-23-ic/valuation-data.json"

    # Step 1: healthy branches write their deliverables; the fetch branch is
    # the timed-out shape — ok with no files.
    outcome1, driver1, dispatcher1, backend1 = await _drive_workflow(
        store,
        {
            "公司背景": Writes(
                _ok({"files": [bg_path], "summary": "背景 v1"}),
                {bg_path: "背景产物 v1"},
            ),
            "行业支线": Writes(
                _ok({"files": [ind_path], "summary": "行业 v1"}),
                {ind_path: "行业产物 v1"},
            ),
            "财务镜头": Writes(
                _ok({"files": [lens_json, lens_md], "summary": "镜头 v1"}),
                {lens_json: json.dumps({"lenses": [1, 2, 3]}), lens_md: "镜头产物 v1"},
            ),
            "估值取数": Immediate(_ok({"files": [], "summary": "未落盘，本轮超时"})),
            "支线核验": _probe_answer,
        },
        default=_generic_writes,
    )
    assert outcome1.status == "completed", outcome1.error
    result1 = outcome1.result
    assert result1["ok"] is False
    assert result1["missing"] == ["val"]
    assert "估值历史与可比" in result1["blocked"]
    dispatched1 = {str(c["description"]) for c in dispatcher1.calls}
    assert "估值历史与可比" not in dispatched1, "no write stage off an empty fetch"
    assert "初筛成稿" not in dispatched1
    fetch1 = _journal_entry_of(backend1, driver1, "估值取数")
    assert fetch1["status"] == "partial" and fetch1["replayable"] is False

    # Step 2: the main thread fills the data file in the workspace (run 1's
    # store — make_fs_backend copies its argument, so the fill goes here).
    backend1.writes[data_path] = "主线程手填的取数稿（等待重派覆盖）"

    # Step 3: same params + resume. The healthy branches replay from run 1's
    # journal; only the failed fetch re-dispatches, writing v2 content.
    source1 = f".agents/threads/short-1/workflows/{driver1.wf_task_id}"
    v2_data = "重派取数稿 v2：ln(P1/P0)=ln(EPS1/EPS0)+ln(PE1/PE0) 分解原始数据"
    valuation_v2 = "估值成稿 v2（基于重派取数）：\n" + "ln(P1/P0) 分解与可比表行\n" * 40
    comps_v2 = "x" * 1200
    store2 = dict(backend1.writes)  # run 1's files + journals, incl. the fill
    store2["./research/2026-10-23-ic/underwriting-summary.md"] = (
        "已保存 thesis 版本 1\n赔率计算摘要（立场：中性）\n"
    )
    store2["./research/2026-10-23-ic/questions.md"] = "12 问初筛底稿\n"
    store2["./drafts/2026-10-23-initial-coverage.md"] = "# 初筛\n\n## 摘要\n摘要。\n"

    async def _write_stage(prompt: str, backend: Any) -> Writes:
        """The write child: take the input path the workflow's PROMPT names
        (“以 <path> 为底”), read that file through the run's own backend, and
        quote what was actually read — a wrong path in the prompt fails here."""
        match = re.search(r"以 (\S+) 为底", prompt)
        assert match, "the write prompt must name its input data file"
        source_file = match.group(1)
        data_text = await backend.aread_text(source_file)
        assert data_text is not None, f"write stage input {source_file!r} not on disk"
        return Writes(
            _ok(
                {
                    "files": [
                        "./research/2026-10-23-ic/valuation.md",
                        "./research/2026-10-23-ic/comps.xlsx",
                    ],
                    "summary": f"写稿基于取数稿：{data_text}",
                }
            ),
            {
                "./research/2026-10-23-ic/valuation.md": valuation_v2,
                "./research/2026-10-23-ic/comps.xlsx": comps_v2,
            },
        )

    outcome2, driver2, dispatcher2, backend2 = await _drive_workflow(
        store2,
        {
            "估值取数": Writes(
                _ok({"files": [data_path], "summary": "重派取数完成 v2"}),
                {data_path: v2_data},
            ),
            "支线核验": _probe_answer,
            "估值历史与可比": _write_stage,
            "草稿探测": Immediate(
                _ok(
                    {
                        "files": ["./drafts/2026-10-23-initial-coverage.md"],
                        "draft_content": "absent",
                    }
                )
            ),
            "赔率摘要核验": Immediate(
                _ok(
                    {
                        "files": ["./research/2026-10-23-ic/underwriting-summary.md"],
                        "exists": True,
                        "first_line": "已保存 thesis 版本 1",
                    }
                )
            ),
            "审稿": Immediate(
                _ok(
                    {
                        "files": [
                            "./drafts/2026-10-23-initial-coverage.md",
                            "./research/2026-10-23-ic/questions.md",
                            "./research/2026-10-23-ic/underwriting-summary.md",
                        ],
                        "verdict": "block",
                        "summary": "测试收尾 block",
                        "must_fix": [],
                        "draft_sha256": "a" * 64,
                        # 审稿契约（提示词第 1 批 #2）：items 逐项覆盖 1–9，
                        # 缺项的结果按审稿失败处理而不是 block。
                        "items": [
                            {"check": n, "result": "revise", "reason": "block 用例"}
                            for n in range(1, 10)
                        ],
                    }
                )
            ),
        },
        default=_generic_writes,
        journal_source=source1,
    )
    assert outcome2.status == "completed", outcome2.error
    assert outcome2.result["ok"] is False
    assert outcome2.result["blocked"] == "测试收尾 block"
    assert any(item.startswith("审稿 block") for item in outcome2.result["incomplete"])

    dispatched2 = [str(c["description"]) for c in dispatcher2.calls]
    assert "估值取数" in dispatched2, "the failed branch re-dispatches"
    for healthy in ("公司背景", "行业支线", "财务镜头"):
        assert healthy not in dispatched2, f"{healthy} must replay, not re-dispatch"
    assert "估值历史与可比" in dispatched2, "the write stage runs off the re-dispatched data"

    # Step 4: the write stage READ the re-dispatched content — its journaled
    # answer quotes v2, never the main thread's manual fill.
    write_entry = _journal_entry_of(backend2, driver2, "估值历史与可比")
    assert "重派取数稿 v2" in write_entry["result"]["summary"]
    assert "手填" not in write_entry["result"]["summary"]
    assert backend2.writes[data_path] == v2_data, "the re-dispatch overwrote the manual fill"
    assert write_entry["artifacts"] == [
        {
            "path": "./research/2026-10-23-ic/valuation.md",
            "sha256": hashlib.sha256(valuation_v2.encode("utf-8")).hexdigest(),
        },
        {
            "path": "./research/2026-10-23-ic/comps.xlsx",
            "sha256": hashlib.sha256(comps_v2.encode("utf-8")).hexdigest(),
        },
    ]
    # And the replayed healthy branches carry their origin, unchanged.
    replayed = _journal_entry_of(backend2, driver2, "公司背景")
    assert replayed["resumed_from"] == source1


@pytest.mark.asyncio
async def test_initial_coverage_underwriting_summary_pause_and_resume_through_real_journal() -> None:
    """Round 4 #2: when underwriting-summary.md is absent on Run 1, 赔率摘要核验
    still declares `files: [underwritingSummaryPath]`, so WorkflowJournal records
    `replayable: False`. After the main agent writes underwriting-summary.md and
    resumes with `journal_source=source1`, 赔率摘要核验 is NOT replayed from the
    stale 'exists: False' entry — it re-dispatches, reads the newly written summary,
    and the workflow proceeds to 初筛成稿 and 审稿 while replaying earlier branches."""
    uw_path = "./research/2026-10-23-ic/underwriting-summary.md"
    q_path = "./research/2026-10-23-ic/questions.md"
    draft_path = "./drafts/2026-10-23-initial-coverage.md"
    proposal_path = "./research/2026-10-23-ic/knowledge-proposal.json"

    def _draft_probe(_prompt: str, backend: Any) -> Immediate:
        content = backend.files.get(draft_path, "absent")
        return Immediate(_ok({"files": [draft_path], "draft_content": content}))

    def _uw_probe(_prompt: str, backend: Any) -> Immediate:
        if uw_path in backend.files:
            first_line = backend.files[uw_path].splitlines()[0]
            return Immediate(
                _ok({"files": [uw_path], "exists": True, "first_line": first_line})
            )
        return Immediate(
            _ok({"files": [uw_path], "exists": False, "first_line": "none"})
        )

    store1: dict[str, str] = {}
    by_label = {
        "支线核验": _probe_answer,
        "草稿探测": _draft_probe,
        "知识层草案": Writes(
            _ok({"files": [proposal_path], "summary": "知识层草案 v1"}),
            {proposal_path: json.dumps({"thesis": [{"op": "create", "value": {}}]})},
        ),
        "赔率摘要核验": _uw_probe,
        "初筛成稿": Writes(
            _ok(
                {
                    "files": [draft_path, uw_path],
                    "summary": "成稿完成",
                    "sha256": "a" * 64,
                    "underwriting_version": 1,
                }
            ),
            {
                draft_path: "# 初筛\n\n## 摘要\n摘要。\n",
                q_path: "# 12 问初筛\n## Q1. 问题\n",
            },
        ),
        "语言审查 1": Writes(
            _ok({"files": [draft_path], "summary": "语言审查 1", "sha256": "a" * 64}),
            {draft_path: "# 初筛\n\n## 摘要\n摘要。\n"},
        ),
        "审稿": Immediate(
            _ok(
                {
                    "files": [draft_path, q_path, uw_path],
                    "verdict": "block",
                    "summary": "进到审稿即止",
                    "must_fix": [],
                    "draft_sha256": "a" * 64,
                    "items": [
                        {"check": n, "result": "revise", "reason": "ok"}
                        for n in range(1, 10)
                    ],
                }
            )
        ),
    }

    # Run 1: underwriting-summary.md does not exist -> pauses before 初筛成稿
    outcome1, driver1, dispatcher1, backend1 = await _drive_workflow(
        store1,
        by_label,
        default=_generic_writes,
    )
    assert outcome1.status == "completed", outcome1.error
    assert outcome1.result["ok"] is False
    assert "已保存 thesis 版本 N" in outcome1.result["blocked"]
    dispatched1 = [str(c["description"]) for c in dispatcher1.calls]
    assert "赔率摘要核验" in dispatched1
    assert "初筛成稿" not in dispatched1

    uw_entry1 = _journal_entry_of(backend1, driver1, "赔率摘要核验")
    assert uw_entry1["result"]["exists"] is False
    assert uw_entry1["replayable"] is False, (
        "a probe of a non-existent file must declare the path in `files` so the journal marks it non-replayable"
    )

    # Main agent formally saves thesis and writes underwriting-summary.md on disk
    backend1.writes[uw_path] = (
        "已保存 thesis 版本 1\n"
        "赔率计算摘要（立场：中性，期限：18 个月）：\n"
        "- 三情景价格区间：悲观 75–85 / 基准 95–105 / 乐观 120–130\n"
    )
    source1 = f".agents/threads/short-1/workflows/{driver1.wf_task_id}"
    store2 = dict(backend1.writes)

    # Run 2: resume with same params and journal_source=source1
    outcome2, driver2, dispatcher2, backend2 = await _drive_workflow(
        store2,
        by_label,
        default=_generic_writes,
        journal_source=source1,
    )
    assert outcome2.status == "completed", outcome2.error
    assert outcome2.result["blocked"] == "进到审稿即止"

    dispatched2 = [str(c["description"]) for c in dispatcher2.calls]
    for replayed_label in ("公司背景", "行业支线", "财务镜头", "估值取数", "估值历史与可比", "bull/bear 快速版", "知识层草案"):
        assert replayed_label not in dispatched2, f"{replayed_label} must replay from Run 1's journal"
    assert "赔率摘要核验" in dispatched2, "赔率摘要核验 must re-dispatch after underwriting-summary.md is created"
    assert "初筛成稿" in dispatched2, "resumed run must proceed past the pause point to 初筛成稿"
    assert "审稿" in dispatched2, "resumed run must reach 审稿"

    uw_entry2 = _journal_entry_of(backend2, driver2, "赔率摘要核验")
    assert uw_entry2["result"]["exists"] is True
    assert uw_entry2["replayable"] is True
    assert uw_entry2["artifacts"] == [
        {
            "path": uw_path,
            "sha256": hashlib.sha256(store2[uw_path].encode("utf-8")).hexdigest(),
        }
    ]


@pytest.mark.asyncio
async def test_initial_coverage_e2e_pause_reunderwrite_resume_and_check_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 6 #1/#2/#3/#4: end-to-end test using the real WorkflowDriver, WorkflowJournal,
    and run_check_done covering:
    首次暂停(100) -> 暂停期间主 agent 正式写入依赖文档与 thesis v1(150) 并按固定格式写摘要 ->
    resume -> 成稿(v1) -> 语言审查 1 改动草稿 -> 审稿 -> 修订 1(声明摘要文件, needs_reunderwrite=true, v1) 立刻停 ->
    主 agent 写入 thesis v2 并更新摘要 -> resume(200) -> 成稿与修订重跑(v2) -> 复审 -> 赔率摘要复核(三者一致=2) ->
    终稿审稿 -> 调用完成检查（沿 resume.json 回溯到 100，按 since 过滤依赖文档通过，三者版本号一致=2）."""
    from tests.unit.dalton.test_check_done import (
        DEFAULT_IC_DRAFT,
        DEFAULT_IC_PROPOSAL,
        DEFAULT_IC_QUESTIONS,
        IC_DOCS,
        PARAMS,
        WS,
        _make_backend,
        _patch_knowledge,
        _patch_verdicts,
        _verdict_row,
        _with_full_language_sidecars,
    )
    from src.dalton.tools.check_done_tools import run_check_done

    bg_path = "./drafts/2026-10-23-background.md"
    ind_path = "./research/2026-10-23-ic/industry.md"
    lens_json = "./research/2026-10-23-ic/lenses.json"
    lens_md = "./drafts/2026-10-23-lenses.md"
    val_data = "./research/2026-10-23-ic/valuation-data.json"
    val_md = "./research/2026-10-23-ic/valuation.md"
    comps_xlsx = "./research/2026-10-23-ic/comps.xlsx"
    bb_path = "./research/2026-10-23-ic/bullbear-quick.md"
    q_path = "./research/2026-10-23-ic/questions.md"
    proposal_path = "./research/2026-10-23-ic/knowledge-proposal.json"
    uw_path = "./research/2026-10-23-ic/underwriting-summary.md"
    draft_path = "./drafts/2026-10-23-initial-coverage.md"

    draft_v1_raw = DEFAULT_IC_DRAFT + "\n乐观情景退出倍数 32x（初稿）。\n"
    draft_v1_raw_sha = hashlib.sha256(draft_v1_raw.encode("utf-8")).hexdigest()
    draft_v1 = DEFAULT_IC_DRAFT + "\n乐观情景退出倍数 32x。\n"
    draft_v1_sha = hashlib.sha256(draft_v1.encode("utf-8")).hexdigest()
    draft_v2_raw = DEFAULT_IC_DRAFT + "\n乐观情景退出倍数 28x（重跑初稿）。\n"
    draft_v2_raw_sha = hashlib.sha256(draft_v2_raw.encode("utf-8")).hexdigest()
    draft_v2 = DEFAULT_IC_DRAFT + "\n乐观情景退出倍数 28x。\n"
    draft_v2_sha = hashlib.sha256(draft_v2.encode("utf-8")).hexdigest()

    def _current_uw_version(backend: Any) -> int:
        first_line = backend.files.get(uw_path, "").splitlines()[0]
        return 2 if "版本 2" in first_line else 1

    def _draft_probe(_prompt: str, backend: Any) -> Immediate:
        content = backend.files.get(draft_path, "absent")
        return Immediate(_ok({"files": [draft_path], "draft_content": content}))

    def _uw_probe(_prompt: str, backend: Any) -> Immediate:
        if uw_path in backend.files:
            first_line = backend.files[uw_path].splitlines()[0]
            return Immediate(
                _ok({"files": [uw_path], "exists": True, "first_line": first_line})
            )
        return Immediate(
            _ok({"files": [uw_path], "exists": False, "first_line": "none"})
        )

    def _uw_recheck(_prompt: str, backend: Any) -> Immediate:
        first_line = backend.files[uw_path].splitlines()[0]
        return Immediate(
            _ok(
                {
                    "files": [uw_path, draft_path],
                    "exists": True,
                    "first_line": first_line,
                }
            )
        )

    def _draft_writer(_prompt: str, backend: Any) -> Writes:
        ver = _current_uw_version(backend)
        if ver == 1:
            return Writes(
                _ok(
                    {
                        "files": [draft_path, uw_path],
                        "summary": "成稿 v1",
                        "sha256": draft_v1_raw_sha,
                        "underwriting_version": 1,
                    }
                ),
                {draft_path: draft_v1_raw, q_path: DEFAULT_IC_QUESTIONS},
            )
        return Writes(
            _ok(
                {
                    "files": [draft_path, uw_path],
                    "summary": "成稿 v2（按新摘要重跑）",
                    "sha256": draft_v2_raw_sha,
                    "underwriting_version": 2,
                }
            ),
            {draft_path: draft_v2_raw, q_path: DEFAULT_IC_QUESTIONS},
        )

    def _lang1_writer(_prompt: str, backend: Any) -> Writes:
        # 语言审查 1 modifies draft_path on disk so that on resume after Pause 2,
        # 初筛成稿's prior journal output no longer matches disk and 初筛成稿 re-runs.
        ver = _current_uw_version(backend)
        body = draft_v1 if ver == 1 else draft_v2
        sha = draft_v1_sha if ver == 1 else draft_v2_sha
        return Writes(
            _ok({"files": [draft_path], "summary": "语言审查 1", "sha256": sha}),
            {draft_path: body},
        )

    def _revision1_writer(_prompt: str, backend: Any) -> Writes:
        ver = _current_uw_version(backend)
        if ver == 1:
            return Writes(
                _ok(
                    {
                        "files": [draft_path, q_path, uw_path],
                        "summary": "发现需要修改乐观情景倍数，等待主智能体重写赔率",
                        "sha256": draft_v1_sha,
                        "needs_reunderwrite": True,
                        "underwriting_version": 1,
                    }
                ),
                {draft_path: draft_v1, q_path: DEFAULT_IC_QUESTIONS},
            )
        return Writes(
            _ok(
                {
                    "files": [draft_path, q_path, uw_path],
                    "summary": "已按版本 2 赔率摘要将乐观情景倍数更新为 28x",
                    "sha256": draft_v2_sha,
                    "needs_reunderwrite": False,
                    "underwriting_version": 2,
                }
            ),
            {draft_path: draft_v2, q_path: DEFAULT_IC_QUESTIONS},
        )

    def _evidence_probe(prompt: str, backend: Any) -> Immediate:
        paths = [
            line[2:].strip()
            for line in prompt.splitlines()
            if line.startswith("- ") and "|" not in line
        ]
        entries = []
        for p in paths:
            content = backend.files.get(p, "")
            sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
            entries.append(f"{p}|{sha}")
        return Immediate(_ok({"files": entries}))

    by_label = {
        "公司背景": Writes(
            _ok({"files": [bg_path], "summary": "公司背景"}),
            {bg_path: "背景" * 200},
        ),
        "行业支线": Writes(
            _ok({"files": [ind_path], "summary": "行业支线"}),
            {ind_path: "行业" * 200},
        ),
        "财务镜头": Writes(
            _ok({"files": [lens_json, lens_md], "summary": "财务镜头"}),
            {
                lens_json: json.dumps({"lenses": ["l" * 20] * 3}),
                lens_md: "镜头" * 200,
            },
        ),
        "估值取数": Writes(
            _ok({"files": [val_data], "summary": "估值取数"}),
            {val_data: json.dumps({"pe": 15})},
        ),
        "支线核验": _probe_answer,
        "估值历史与可比": Writes(
            _ok({"files": [val_md, comps_xlsx], "summary": "估值历史与可比"}),
            {val_md: "估值" * 200, comps_xlsx: "x" * 1200},
        ),
        "bull/bear 快速版": Writes(
            _ok({"files": [bb_path], "summary": "bull/bear"}),
            {bb_path: "多空" * 200},
        ),
        "草稿探测": _draft_probe,
        "知识层草案": Writes(
            _ok({"files": [proposal_path], "summary": "知识层草案"}),
            {proposal_path: DEFAULT_IC_PROPOSAL},
        ),
        "赔率摘要核验": _uw_probe,
        "初筛成稿": _draft_writer,
        "语言审查 1": _lang1_writer,
        "审稿": Immediate(
            _ok(
                {
                    "files": [draft_path, q_path, uw_path],
                    "verdict": "revise",
                    "summary": "乐观情景倍数偏高，需下调至 28x",
                    "must_fix": ["将乐观情景倍数从 32x 下调至 28x"],
                    "items": [
                        {"check": n, "result": "revise" if n == 5 else "pass", "reason": "ok"}
                        for n in range(1, 10)
                    ],
                }
            )
        ),
        "修订 1": _revision1_writer,
        "赔率摘要复核": _uw_recheck,
        "复审 1": Immediate(
            _ok(
                {
                    "files": [draft_path, q_path, uw_path],
                    "verdict": "pass",
                    "summary": "已按版本 2 赔率摘要修正",
                    "must_fix": [],
                    "items": [
                        {"check": n, "result": "pass", "reason": "ok"}
                        for n in range(1, 10)
                    ],
                }
            )
        ),
        "笔记语言审查": Writes(
            _ok(
                {
                    "files": [bg_path, lens_md, ind_path, val_md, bb_path, q_path],
                    "summary": "笔记语言审查通过",
                }
            ),
            {},
        ),
        "语言审查 2": Writes(
            _ok({"files": [draft_path], "summary": "语言审查 2", "sha256": draft_v2_sha}),
            {draft_path: draft_v2},
        ),
        "终稿审稿": Immediate(
            _ok(
                {
                    "files": [draft_path, q_path, uw_path],
                    "verdict": "pass",
                    "summary": "终稿审稿通过",
                    "must_fix": [],
                    "items": [
                        {"check": n, "result": "pass", "reason": "ok"}
                        for n in range(1, 10)
                    ],
                }
            )
        ),
        "产物核验": _evidence_probe,
    }

    def _pin_dispatch_timestamps(store: dict[str, str], run_dir: str, ts: float) -> None:
        prefix = f"{run_dir}/journal/"
        for k, v in list(store.items()):
            clean = k[2:] if k.startswith("./") else k
            if clean.startswith(prefix) and clean.endswith(".json"):
                doc = json.loads(v)
                for entry in doc.get("dispatches") or []:
                    if isinstance(entry, dict):
                        entry["recorded_at"] = ts
                store[k] = json.dumps(doc)

    # 1) Run 1 (started at 100.0): pauses at 赔率摘要核验 before 初筛成稿
    outcome1, driver1, dispatcher1, backend1 = await _drive_workflow(
        {},
        by_label,
        default=_generic_writes,
    )
    assert outcome1.status == "completed", outcome1.error
    assert outcome1.result["ok"] is False
    assert "已保存 thesis 版本 N" in outcome1.result["blocked"]
    assert "初筛成稿" not in [str(c["description"]) for c in dispatcher1.calls]

    source1 = f".agents/threads/short-1/workflows/{driver1.wf_task_id}"
    _pin_dispatch_timestamps(backend1.writes, source1, 100.0)
    backend1.writes[f"{source1}/status.json"] = json.dumps(
        {
            "run_id": driver1.wf_task_id,
            "name": "initial-coverage",
            "status": "completed",
            "updated_at": 110.0,
        }
    )

    # 2) Pause 1 (at 150.0): main agent formally writes agenda/debate/company/drivers/management + thesis v1,
    #    and writes underwriting-summary.md with fixed header "已保存 thesis 版本 1"
    backend1.writes[uw_path] = (
        "已保存 thesis 版本 1\n"
        "赔率计算摘要（立场：中性，期限：18 个月）：\n"
        "- 三情景价格区间：悲观 75–85 / 基准 95–105 / 乐观 125–135\n"
    )
    store2 = dict(backend1.writes)

    # 3) Run 2 (resume=run1, started at 180.0): runs 初筛成稿 -> 语言审查 1 (modifies draft) ->
    #    审稿 (revise) -> 修订 1 (needs_reunderwrite=true) -> pauses immediately before 复审 1
    outcome2, driver2, dispatcher2, backend2 = await _drive_workflow(
        store2,
        by_label,
        default=_generic_writes,
        journal_source=source1,
    )
    assert outcome2.status == "completed", outcome2.error
    assert outcome2.result["ok"] is False
    assert "needs_reunderwrite=true" in outcome2.result["blocked"]
    dispatched2 = [str(c["description"]) for c in dispatcher2.calls]
    assert "初筛成稿" in dispatched2
    assert "语言审查 1" in dispatched2
    assert "审稿" in dispatched2
    assert "修订 1" in dispatched2
    assert "复审 1" not in dispatched2, "must pause before 复审 1 when needs_reunderwrite is true"

    # Record resume.json in Run 2's directory (as create_run_workflow_tool does)
    source2 = f".agents/threads/short-1/workflows/{driver2.wf_task_id}"
    _pin_dispatch_timestamps(backend2.writes, source2, 180.0)
    backend2.writes[f"{source2}/resume.json"] = json.dumps(
        {"resumed_from": source1, "claimed_by": driver2.wf_task_id, "claimed_at": 180.0}
    )
    backend2.writes[f"{source2}/status.json"] = json.dumps(
        {
            "run_id": driver2.wf_task_id,
            "name": "initial-coverage",
            "status": "completed",
            "updated_at": 190.0,
        }
    )

    # 4) Pause 2: main agent formally writes thesis version 2 and updates underwriting-summary.md
    backend2.writes[uw_path] = (
        "已保存 thesis 版本 2\n"
        "赔率计算摘要（立场：中性，期限：18 个月）：\n"
        "- 三情景价格区间：悲观 75–85 / 基准 95–105 / 乐观 115–125\n"
    )
    store3 = dict(backend2.writes)

    # 5) Run 3 (resume=run2, started at 200.0): because 语言审查 1 modified draft_path in Run 2
    #    and uw_path changed in Pause 2, 初筛成稿 and 修订 1 re-run with version 2 ->
    #    复审 1 -> 赔率摘要复核 (verifies version 2 === finalDraftVersion 2) -> 终稿审稿 -> completes
    outcome3, driver3, dispatcher3, backend3 = await _drive_workflow(
        store3,
        by_label,
        default=_generic_writes,
        journal_source=source2,
    )
    assert outcome3.status == "completed", outcome3.error
    assert outcome3.result["ok"] is True, outcome3.result["incomplete"]
    dispatched3 = [str(c["description"]) for c in dispatcher3.calls]
    assert "初筛成稿" in dispatched3
    assert "修订 1" in dispatched3
    assert "复审 1" in dispatched3
    assert "赔率摘要复核" in dispatched3
    assert "终稿审稿" in dispatched3

    source3 = f".agents/threads/short-1/workflows/{driver3.wf_task_id}"
    _pin_dispatch_timestamps(backend3.writes, source3, 200.0)
    backend3.writes[f"{source3}/resume.json"] = json.dumps(
        {"resumed_from": source2, "claimed_by": driver3.wf_task_id, "claimed_at": 200.0}
    )
    backend3.writes[f"{source3}/status.json"] = json.dumps(
        {
            "run_id": driver3.wf_task_id,
            "name": "initial-coverage",
            "status": "completed",
            "updated_at": 210.0,
        }
    )

    # 6) Call run_check_done against the final workspace state and DB version 2.
    #    All dependency docs were written during Pause 1 at 150.0 (between Run 1 at 100.0 and Run 3 at 200.0).
    check_files: dict[str, str] = {}
    for k, v in backend3.writes.items():
        clean_key = k[2:] if k.startswith("./") else k
        check_files[clean_key] = v
    check_files["drafts/2026-10-23-initial-coverage.audit.md"] = (
        "# 数字核验报告\n- **草稿哈希**：`" + draft_v2_sha[:16] + "…`\n"
    )
    _with_full_language_sidecars(check_files)

    _patch_knowledge(
        monkeypatch,
        IC_DOCS,
        start_epochs={driver3.wf_task_id: 200},
        thread_start_epoch=100,
        thesis_version_no=2,
        doc_timestamps={doc: 150.0 for doc in IC_DOCS},
    )
    _patch_verdicts(
        monkeypatch,
        [_verdict_row("NEW_THESIS", run_id=driver3.wf_task_id)],
    )

    done_res = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(check_files),
        workspace_id=WS,
        run_id=driver3.wf_task_id,
        thread_id="short-1",
    )
    assert done_res["done"] is True, done_res["missing"]
    assert done_res["missing"] == []
