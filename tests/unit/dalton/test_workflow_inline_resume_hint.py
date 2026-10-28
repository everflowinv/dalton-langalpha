"""D6: an interrupted inline workflow run's resume hint must not point at a
workflow name — an inline script has none, and ``workflow=<name>`` answered
``Unknown workflow`` on the first recovery attempt. Every surface that
suggests a resume names both forms (the name for a named run, the script
verbatim for an inline one) and the run's own script snapshot; the resume
interface itself accepts an inline script by verifying it against that
snapshot."""

from __future__ import annotations

from contextlib import asynccontextmanager
from fnmatch import fnmatch
from typing import Any
from unittest.mock import create_autospec

import pytest
from psycopg import AsyncConnection, AsyncCursor

from ptc_agent.agent.backends.sandbox import SandboxBackend
from ptc_agent.agent.middleware.background_subagent.registry import (
    BackgroundTaskRegistry,
)
from ptc_agent.agent.middleware.background_subagent.tools import (
    _compose_missing_reply,
    _settled_text,
)
from ptc_agent.agent.middleware.background_subagent.task import BackgroundTask
from src.dalton.workflows.journal import (
    ResumeRefused,
    resolve_resume_source,
    workflow_resume_call,
    worker_lost_failure,
)
from src.server.models.chat import TaskReport
from src.server.services.report_back.subagent import (
    task_report_message,
    task_report_verdict,
)

THREAD_ID = "64f22fbb-1234-5678-9abc-def012345678"
RUN_DIR = ".agents/threads/64f22fbb/workflows/wf1a2b"
SNAPSHOT = f"{RUN_DIR}/workflow.js"

NAMED_FORM = (
    "RunWorkflow(workflow=<same name>, params=<same as before>, resume='wf1a2b')"
)
INLINE_FORM = (
    "RunWorkflow(script=<the same script verbatim>, params=<same as before>, "
    "resume='wf1a2b')"
)


def _fs_backend(files: dict[str, str]) -> Any:
    backend = create_autospec(SandboxBackend, instance=True)
    store = dict(files)

    async def _aread_text(path: str) -> str | None:
        return store.get(path)

    async def _aglob_paths(pattern: str, path: str = ".") -> list[str]:
        return [key for key in store if fnmatch(key, pattern)]

    backend.aread_text.side_effect = _aread_text
    backend.aglob_paths.side_effect = _aglob_paths
    return backend


def _phrase(thread_id: Any) -> str:
    return workflow_resume_call("wf1a2b", thread_id)


# ------------------------------------------------------------ the shared phrase

def test_the_phrase_names_both_resume_forms_and_the_snapshot():
    phrase = _phrase(THREAD_ID)
    assert NAMED_FORM in phrase
    assert INLINE_FORM in phrase
    assert SNAPSHOT in phrase


def test_the_phrase_omits_the_snapshot_without_a_real_thread_id():
    for thread_id in (None, "", object()):
        phrase = _phrase(thread_id)
        assert NAMED_FORM in phrase
        assert INLINE_FORM in phrase
        assert "workflow.js" not in phrase


# ------------------------------------------------------------- failure + notice

@pytest.mark.asyncio
async def test_worker_lost_failure_tells_inline_runs_to_repass_the_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.server.database.pool as db_pool_mod

    fake_cur = create_autospec(AsyncCursor, instance=True)
    fake_cur.fetchone.return_value = ("workflow",)
    fake_cur.__aenter__.return_value = fake_cur
    fake_cur.__aexit__.return_value = None
    fake_conn = create_autospec(AsyncConnection, instance=True)
    fake_conn.cursor.return_value = fake_cur

    @asynccontextmanager
    async def _fake_conn():
        yield fake_conn

    monkeypatch.setattr(
        db_pool_mod,
        "get_db_connection",
        create_autospec(db_pool_mod.get_db_connection, side_effect=_fake_conn),
    )

    failure = await worker_lost_failure(THREAD_ID, "wf1a2b")
    error = failure["error"]
    assert NAMED_FORM in error
    assert INLINE_FORM in error
    assert SNAPSHOT in error
    assert "MUST NOT manually re-dispatch Task subagents" in error


def test_notification_resume_hint_names_the_inline_form_and_snapshot():
    report = TaskReport(
        kind="task",
        task_id="wf1a2b",
        display_id="Task-wf1a2b",
        description="ad-hoc screen",
        failed=True,
    )
    msg = task_report_message(
        report,
        "workflow",
        failure_error="worker_lost: ... the run was interrupted and is RESUMABLE",
        thread_id=THREAD_ID,
    )
    assert NAMED_FORM in msg
    assert INLINE_FORM in msg
    assert SNAPSHOT in msg
    assert "do NOT manually re-dispatch Task subagents" in msg


@pytest.mark.asyncio
async def test_owed_report_carries_the_thread_id_from_its_job():
    owed = await task_report_verdict(
        {
            "conversation_thread_id": THREAD_ID,
            "payload": {
                "task_id": "wf1a2b",
                "task_run_id": None,
                "display_id": "Task-wf1a2b",
                "subagent_type": "workflow",
                "description": "ad-hoc screen",
                "final_status": "error",
                "failure_error": "worker_lost: ... RESUMABLE",
            },
        }
    )
    assert owed.thread_id == THREAD_ID


# ---------------------------------------------------------- the TaskOutput path

@pytest.mark.asyncio
async def test_settled_and_missing_replies_name_the_inline_form():
    registry = BackgroundTaskRegistry(thread_id=THREAD_ID)
    lost = "worker_lost: ... the run was interrupted and is RESUMABLE"
    task = BackgroundTask(
        task_id="wf1a2b",
        tool_call_id="tc-1",
        description="ad-hoc screen",
        prompt="run",
        subagent_type="workflow",
        terminal_status="error",
        result={"success": False, "error": lost},
    )

    settled = await _settled_text(registry, task)
    assert INLINE_FORM in settled
    assert SNAPSHOT in settled

    missing = await _compose_missing_reply(
        registry,
        "wf1a2b",
        {
            "task_run_id": "run-1",
            "status": "error",
            "subagent_type": "workflow",
            "failure": {"error": lost},
        },
    )
    assert INLINE_FORM in missing
    assert SNAPSHOT in missing


# ------------------------------------------------- the resume interface itself

@pytest.mark.asyncio
async def test_the_resume_interface_accepts_an_inline_script():
    """``RunWorkflow(script=..., resume=...)`` is a supported resume: the
    journal verifies the script against the run's own snapshot, byte for
    byte — which is exactly why the hint can send an inline run back to its
    snapshot instead of a name."""
    script = "const meta = {name: 'ad-hoc'};"
    backend = _fs_backend({SNAPSHOT: script})
    registry = BackgroundTaskRegistry(thread_id="")

    source = await resolve_resume_source(
        backend,
        registry,
        source_dir=RUN_DIR,
        resume="wf1a2b",
        script=script,
    )
    assert source == RUN_DIR

    with pytest.raises(ResumeRefused):
        await resolve_resume_source(
            backend,
            registry,
            source_dir=RUN_DIR,
            resume="wf1a2b",
            script=script + "// drifted",
        )
