"""`dalton_check_done`: each gap class comes back as its own missing item, and
a satisfied contract reports done.

The filesystem seam is a spec'd ``SandboxBackend`` and the database seams are
spec'd ``AsyncConnection``/``AsyncCursor`` (create_autospec), so a renamed
backend method or a changed query shape fails these tests rather than
silently passing. Verdict rows carry the REAL ``dalton_verdicts`` columns
(``word``, ``trigger_kind``, ...) exactly as ``_serialize_row`` emits them.
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from fnmatch import fnmatch
from typing import Any
from unittest.mock import create_autospec

import psycopg
import pytest

from ptc_agent.agent.backends.sandbox import SandboxBackend
from src.dalton.database.publications import list_pending as _REAL_LIST_PENDING
from src.dalton.database.verdicts import (
    get_verdict_for_execution as _REAL_GET_VERDICT_FOR_EXECUTION,
    list_verdicts as _REAL_LIST_VERDICTS,
)
from src.dalton.tools import check_done_tools
from src.dalton.tools.check_done_tools import run_check_done

WS = "ws-1"
PARAMS = {"ticker": "META", "workspace_dir": ".", "as_of": "2026-10-23"}

DRAFT = "drafts/2026-10-23-initial-coverage.md"
# A journal carrying a reviewer `pass` — the evidence the review stage reads.
JOURNAL_PASS = ".agents/threads/short-1/workflows/task-run-1/journal/审稿.json"


def _draft_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _with_full_language_sidecars(files: dict[str, str]) -> dict[str, str]:
    """Give every reviewable .md in a healthy-run fixture its two-round sidecar
    — the deterministic workspace scan checks them all, not just the draft."""
    from src.dalton.publish.language import is_reviewable_deliverable

    for path, text in list(files.items()):
        if is_reviewable_deliverable(path) and f"{path}.language.json" not in files:
            files[f"{path}.language.json"] = _language_json(text)
    return files


def _language_json(draft_text: str, *, round2_sha: str | None = None) -> str:
    """A healthy two-round check_language sidecar for ``draft_text``: round 2
    reviewed the current text (round 1 an older one)."""
    return json.dumps(
        {
            "draft": "drafts/x.md",
            "rounds": [
                {
                    "round": 1,
                    "draft_sha256": _draft_hash("初稿\n" + draft_text[:20]),
                    "model": "test-review-model",
                    "generated_at": "2026-10-23T00:00:00Z",
                    "issues": [],
                    "dropped_quotes": 0,
                },
                {
                    "round": 2,
                    "draft_sha256": round2_sha or _draft_hash(draft_text),
                    "model": "test-review-model",
                    "generated_at": "2026-10-23T01:00:00Z",
                    "issues": [],
                    "dropped_quotes": 0,
                },
            ],
        },
        ensure_ascii=False,
    )


def _make_backend(
    files: dict[str, str],
    *,
    validator_exit: int = 0,
    mtimes: dict[str, int] | None = None,
    read_error: bool = False,
    execute_error: bool = False,
    read_none_paths: frozenset[str] | set[str] = frozenset(),
    scan_rc: int = 0,
) -> Any:
    """A spec'd sandbox backend serving ``files`` and scripted commands.

    ``wc -c`` answers sizes from the file table, ``stat -c %Y`` mtimes from
    ``mtimes`` (defaulting to now), the existence probe prints the tool's
    PRESENT/ABSENT marker; every other command carries the validator
    verdict, so a contract command's exit code is the one knob a test needs.
    ``read_error``/``execute_error`` model a dead sandbox: reads raise and
    commands fail without the probe marker (the real backend converts both
    to exit code 1 with an error string, never a marker). Reads answer None
    for paths in ``read_none_paths`` and for EMPTY files, exactly as the
    real read layer does (empty file and UTF-8 decode failure both read as
    None, not as an exception).
    """
    backend = create_autospec(SandboxBackend, instance=True)
    now = int(time.time())
    mtimes = mtimes or {}

    async def _aread_text(path: str) -> str | None:
        if read_error:
            raise RuntimeError("sandbox unreachable")
        if path in read_none_paths:
            return None  # decode failure: the real read layer answers None
        return files.get(path) or None

    async def _aglob_paths(pattern: str, path: str = ".") -> list[str]:
        return [key for key in files if fnmatch(key, pattern)]

    async def _aexecute(command: str, *, timeout: int | None = None) -> Any:
        class _Response:
            def __init__(self, output: str, exit_code: int) -> None:
                self.output = output
                self.exit_code = exit_code

        if execute_error:
            raise RuntimeError("sandbox unreachable")
        # Real commands may carry the cd-into-workspace prefix; a fake that
        # ignored it would mis-answer every probe (the round-4 lesson).
        body = command
        marker = " && "
        if body.startswith("cd ") and marker in body:
            body = body.split(marker, 1)[1]
        if body.startswith("wc -c"):
            path = body.removeprefix("wc -c < ").strip().strip("'\"")
            content = files.get(path)
            if content is None:
                return _Response("", 1)
            return _Response(str(len(content.encode("utf-8"))), 0)
        if body.startswith("stat -c %Y"):
            path = body.removeprefix("stat -c %Y ").strip().strip("'\"")
            if path not in files:
                return _Response("", 1)
            return _Response(str(mtimes.get(path, now)), 0)
        if body.startswith("{ test -d "):
            # The scan-root existence probe: one of two markers whenever the
            # shell ran, so a dead executor prints neither.
            root = body.split("test -d ", 1)[1].split(" ", 1)[0]
            present = any(p == root or p.startswith(root + "/") for p in files)
            marker = f"__DALTON_DIR_{root}" if present else "__DALTON_DIR_NONE"
            return _Response(marker, 0)
        if body.startswith("{ find "):
            # The language-review manifest scan over the roots the probe found
            # (rc override simulates an unreadable directory).
            roots = body.removeprefix("{ find ").split(" -type", 1)[0].split()
            listed = sorted(
                p
                for p in files
                if p.split("/")[0] in roots and p.endswith(".md")
            )
            return _Response(
                "\n".join(listed) + f"\n__DALTON_SCAN_RC__={scan_rc}", 0
            )
        if "test -e " in body:
            path = body.split("test -e ", 1)[1].split(" &&", 1)[0].strip().strip("'\"")
            found = check_done_tools._PROBE_PRESENT if path in files else check_done_tools._PROBE_ABSENT
            return _Response(found, 0)
        return _Response("stub validator output", validator_exit)

    backend.aread_text.side_effect = _aread_text
    backend.aglob_paths.side_effect = _aglob_paths
    backend.aexecute.side_effect = _aexecute
    return backend


DEFAULT_START_EPOCH = int(datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc).timestamp())


def _patch_knowledge(
    monkeypatch: pytest.MonkeyPatch,
    docs: list[str],
    *,
    start_epochs: dict[str, int | None] | None = None,
    thread_start_epoch: int | None = DEFAULT_START_EPOCH,
) -> None:
    """Patch the version query: ``docs`` are the ones with a tool version.

    The connection and cursor are spec'd against psycopg's real async types;
    the rows are (doc_key,) tuples, the shape the query fetches. Single-row
    start-epoch lookups answer strictly by the bound ``run_id`` / ``thread_id``
    parameter value (never by SQL text)."""
    epochs_by_id: dict[str, int | None] = {"short-1": thread_start_epoch}
    if start_epochs is not None:
        epochs_by_id.update(start_epochs)

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    last_id: dict[str, str | None] = {"value": None}

    async def _execute(_query: Any = "", params: Any = None, **_kwargs: Any) -> None:
        if (
            isinstance(params, (tuple, list))
            and len(params) == 1
            and isinstance(params[0], str)
        ):
            last_id["value"] = params[0]
        else:
            last_id["value"] = None

    async def _fetchall() -> list[tuple[str]]:
        return [(doc,) for doc in docs]

    async def _fetchone() -> tuple[int] | None:
        key = last_id["value"]
        if key is None:
            return None
        epoch = epochs_by_id.get(key)
        return (epoch,) if epoch is not None else None

    cursor.execute.side_effect = _execute
    cursor.fetchall.side_effect = _fetchall
    cursor.fetchone.side_effect = _fetchone

    # The tool enters the cursor as an async context manager, exactly as it
    # would a real one; the autospec instance carries the dunder mocks.
    cursor.__aenter__.return_value = cursor

    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.cursor.return_value = cursor

    @asynccontextmanager
    async def _get_db_connection() -> Any:
        yield conn

    monkeypatch.setattr(check_done_tools, "get_db_connection", _get_db_connection)


def _patch_knowledge_error(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def _get_db_connection() -> Any:
        raise RuntimeError("pool exhausted")
        yield  # pragma: no cover

    monkeypatch.setattr(check_done_tools, "get_db_connection", _get_db_connection)


def _patch_knowledge_capture(
    monkeypatch: pytest.MonkeyPatch, docs: list[str]
) -> dict[str, Any]:
    """``_patch_knowledge`` that records the args the version query ran with —
    ``(workspace_id, docs, since)`` — so a test can pin the freshness window
    itself (audit #20's whole point)."""
    captured: dict[str, Any] = {}
    cursor = create_autospec(psycopg.AsyncCursor, instance=True)

    async def _execute(_query: Any, args: Any = None, **_kwargs: Any) -> None:
        captured["args"] = args

    async def _fetchall() -> list[tuple[str]]:
        return [(doc,) for doc in docs]

    cursor.execute.side_effect = _execute
    cursor.fetchall.side_effect = _fetchall
    cursor.__aenter__.return_value = cursor
    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.cursor.return_value = cursor

    @asynccontextmanager
    async def _get_db_connection() -> Any:
        yield conn

    monkeypatch.setattr(check_done_tools, "get_db_connection", _get_db_connection)
    return captured


def _verdict_row(
    word: str,
    trigger_kind: str = "manual",
    *,
    run_id: str | None = None,
    conversation_thread_id: str | None = None,
) -> dict[str, Any]:
    """One row exactly as ``verdicts._serialize_row`` emits it (real columns)."""
    return {
        "verdict_id": "VD-20261023-01",
        "user_id": "user-1",
        "workspace_id": WS,
        "verdict_at": "2026-10-23T10:00:00+00:00",
        "word": word,
        "trigger_kind": trigger_kind,
        "trigger_ref": None,
        "progress": None,
        "next_due": None,
        "payload": {},
        "conversation_thread_id": conversation_thread_id,
        "run_id": run_id,
        "automation_execution_id": None,
        "origin_type": None,
        "created_at": "2026-10-23T10:00:00+00:00",
    }


def _patch_verdicts(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, Any]] | None = None,
    error: Exception | None = None,
) -> None:
    from src.dalton.database import verdicts as verdicts_db

    # A spec'd stand-in for the real query function, its side effect carrying
    # the real row shape (or the error arm). The spec source was captured at
    # test-module import — a second patch in one test must not spec the first.
    listed = create_autospec(_REAL_LIST_VERDICTS)

    async def _rows(workspace_id: str, limit: int = 60, since: Any = None):
        if error is not None:
            raise error
        return rows or []

    listed.side_effect = _rows
    monkeypatch.setattr(verdicts_db, "list_verdicts", listed)

    by_execution = create_autospec(_REAL_GET_VERDICT_FOR_EXECUTION)

    async def _for_execution(
        *,
        automation_execution_id: str | None = None,
        thread_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any] | None:
        if error is not None:
            raise error
        if not run_id:
            return None
        for row in rows or []:
            if row.get("run_id") == run_id:
                return row
        return None

    by_execution.side_effect = _for_execution
    monkeypatch.setattr(verdicts_db, "get_verdict_for_execution", by_execution)


IC_DOCS = ["company", "thesis", "debate", "drivers", "management", "agenda"]


def _full_files(
    *,
    draft: str = "# 初筛\n" + "内容" * 400,
    audit_hash: str | None = None,
    with_review_pass: bool = True,
) -> dict[str, str]:
    """Every artifact initial-coverage's contract declares, in healthy shape."""
    files = {
        "drafts/2026-10-23-background.md": "背景" * 200,
        "research/2026-10-23-ic/industry.md": "行业" * 200,
        "research/2026-10-23-ic/lenses.json": json.dumps({"lenses": ["l" * 20] * 3}),
        "drafts/2026-10-23-lenses.md": "镜头" * 200,
        "research/2026-10-23-ic/valuation.md": "估值" * 200,
        "research/2026-10-23-ic/comps.xlsx": "x" * 1200,
        "research/2026-10-23-ic/bullbear-quick.md": "多空" * 200,
        "research/2026-10-23-ic/knowledge-proposal.json": json.dumps(
            {"company": [{"op": "append", "value": "x" * 40}]}
        ),
        DRAFT: draft,
    }
    digest = audit_hash or _draft_hash(draft)
    files["drafts/2026-10-23-initial-coverage.audit.md"] = (
        "# 数字核验报告\n- **草稿哈希**：`" + digest[:16] + "…`\n"
    )
    files[f"{DRAFT}.language.json"] = _language_json(draft)
    _with_full_language_sidecars(files)
    if with_review_pass:
        # Run-scoped review evidence: the run's status.json names the
        # workflow, and its journal's LAST review record is a pass whose
        # draft_sha256 matches the draft above (hash computed by the tool,
        # the record echoes the writer's).
        files[".agents/threads/short-1/workflows/task-run-1/status.json"] = json.dumps(
            {"run_id": "task-run-1", "name": "initial-coverage", "status": "completed", "updated_at": 1.0}
        )
        files[JOURNAL_PASS] = json.dumps(
            {
                "label": "审稿",
                "dispatches": [
                    {
                        "label": "审稿",
                        "status": "ok",
                        "seq": 7,
                        "recorded_at": 2.0,
                        "result": {
                            "verdict": "pass",
                            "summary": "通过",
                            "draft_sha256": _draft_hash(draft),
                        },
                    }
                ],
            },
            ensure_ascii=False,
        )
    return files


@pytest.mark.asyncio
async def test_all_satisfied_reports_done(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(_full_files()),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert result["done"], result
    assert result["missing"] == []


@pytest.mark.asyncio
async def test_missing_artifact_is_reported_with_next_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _full_files()
    del files["research/2026-10-23-ic/comps.xlsx"]
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
    )
    assert not result["done"]
    assert any("comps.xlsx" in item and "不存在" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_failing_validator_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _full_files(with_review_pass=False)
    files.update(
        {
            "research/2026-10-23-fc/facts.md": "<!-- FACTS-LEDGER v1 -->\n" + "底表" * 200,
            "research/2026-10-23-fc/questions.md": "问题" * 200,
            "research/2026-10-23-fc/ea.md": "A " * 200 + "\n<!-- 完成 -->",
            "research/2026-10-23-fc/eb.md": "B " * 200 + "\n<!-- 完成 -->",
            "research/2026-10-23-fc/ec.md": "C " * 200 + "\n<!-- 完成 -->",
            "research/2026-10-23-fc/ed.md": "D " * 200 + "\n<!-- 完成 -->",
            "model/META-model-v01.xlsx": "x" * 1200,
            "drafts/2026-10-23-memo.md": "# 备忘录\n" + "内容" * 400,
            "research/2026-10-23-fc/model-check.md": "审模型" * 100,
            "research/2026-10-23-fc/knowledge-revision.json": json.dumps(
                {"thesis": {"ops": [{"op": "set", "value": "x" * 40}]}}
            ),
            "drafts/2026-10-23-memo.audit.md": (
                "# 数字核验报告\n- **草稿哈希**：`"
                + _draft_hash("# 备忘录\n" + "内容" * 400)[:16]
                + "…`\n"
            ),
        }
    )
    _patch_knowledge(
        monkeypatch, ["company", "thesis", "debate", "drivers", "management", "models"]
    )
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="full-coverage",
        params=PARAMS,
        backend=_make_backend(files, validator_exit=1),
        workspace_id=WS,
    )
    assert not result["done"]
    assert any("校验未通过" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_missing_knowledge_version_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_knowledge(monkeypatch, ["company", "thesis"])
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(_full_files()),
        workspace_id=WS,
    )
    assert not result["done"]
    assert any("debate" in item and "source=tool" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_knowledge_query_failure_is_missing_not_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_knowledge_error(monkeypatch)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(_full_files()),
        workspace_id=WS,
    )
    assert not result["done"]
    assert any("知识层版本无法核对" in item and "请重试" in item for item in result["missing"])


# ------------------------------------------------- audit #20: the closing window
# The freshness window opens at the run's start (or the as_of day's start in
# America/New_York on the manual path) — never at UTC midnight, which dropped
# every version written before an Asia-morning UTC crossing.


@pytest.mark.asyncio
async def test_manual_as_of_window_is_eastern_day_and_keeps_evening_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """as_of=2026-10-23, checked at 08:30 UTC+8 (00:30 UTC on 10-24, so UTC
    "today" is already 10-24): the window opens at 2026-10-23 00:00
    America/New_York (04:00 UTC), and a version written on 10-23 evening
    (22:00 UTC+8 = 14:00 UTC) is still inside it."""
    captured = _patch_knowledge_capture(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(_full_files()),
        workspace_id=WS,
    )
    since = captured["args"][2]
    assert since == datetime(2026, 10, 23, 4, 0, tzinfo=timezone.utc)
    # The old floor — UTC midnight of the check's own day — would have been
    # 2026-10-24T00:00Z and dropped this version.
    evening_version = datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc)
    assert since <= evening_version


# ------------------------------------------------- as_of inherited from the run
# The launch path resolves as_of to a concrete trading day before the script
# runs; the closing check inherits that date from the run's args.json so the
# caller's original params (omitted as_of, or "latest") still find the run's
# artifacts.


def _with_run_args(files: dict[str, str], as_of: str = "2026-10-23") -> dict[str, str]:
    files[f".agents/threads/short-1/workflows/task-run-1/args.json"] = json.dumps(
        {"ticker": "META", "as_of": as_of}
    )
    return files


@pytest.mark.asyncio
async def test_check_done_inherits_the_runs_effective_as_of(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitted as_of at closing time: the run's args.json supplies the date
    the artifacts are keyed on — by run_id here, by the thread's latest
    same-name run without one."""
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    params = {"ticker": "META", "workspace_dir": "."}
    result = await run_check_done(
        workflow="initial-coverage",
        params=params,
        backend=_make_backend(_with_run_args(_full_files())),
        workspace_id=WS,
        run_id="task-run-1",
        thread_id="short-1",
    )
    assert not any("缺少必需参数" in item for item in result["missing"]), result["missing"]
    assert not any("产物文件不存在" in item for item in result["missing"]), result["missing"]
    assert result["done"], result["missing"]

    # Same inheritance through the thread's latest run when run_id is absent.
    result = await run_check_done(
        workflow="initial-coverage",
        params=params,
        backend=_make_backend(_with_run_args(_full_files())),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert result["done"], result["missing"]


@pytest.mark.asyncio
async def test_check_done_replaces_latest_with_the_runs_effective_as_of(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """params say as_of="latest" while the run ran on a concrete date: the
    check must look where the artifacts are, not under latest/."""
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params={"ticker": "META", "workspace_dir": ".", "as_of": "latest"},
        backend=_make_backend(_with_run_args(_full_files())),
        workspace_id=WS,
        run_id="task-run-1",
        thread_id="short-1",
    )
    assert result["done"], result["missing"]
    assert not any("latest" in item for item in result["missing"]), result["missing"]


@pytest.mark.asyncio
async def test_check_done_does_not_invent_a_date_without_a_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No run, no args.json: the missing-parameter report stays honest — the
    inheritance never recomputes a date, it only reads the run's record."""
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params={"ticker": "META", "workspace_dir": "."},
        backend=_make_backend(_full_files()),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("缺少必需参数 'as_of'" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_closing_window_starts_at_the_run_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run on the thread anchors the window at its first dispatch (the
    earliest journal recorded_at): versions written mid-run, after a
    UTC-midnight crossing, stay valid."""
    captured = _patch_knowledge_capture(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])
    files = _full_files()
    first_dispatch = datetime(2026, 10, 24, 2, 35, tzinfo=timezone.utc).timestamp()
    files[JOURNAL_PASS] = json.dumps(
        {
            "label": "审稿",
            "dispatches": [
                {
                    "label": "公司背景",
                    "status": "ok",
                    "seq": 1,
                    "recorded_at": first_dispatch,
                    "result": {"files": ["drafts/2026-10-23-background.md"]},
                },
                {
                    "label": "审稿",
                    "status": "ok",
                    "seq": 7,
                    "recorded_at": first_dispatch + 3600,
                    "result": {
                        "verdict": "pass",
                        "summary": "通过",
                        "draft_sha256": _draft_hash(files[DRAFT]),
                    },
                },
            ],
        },
        ensure_ascii=False,
    )

    await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )
    since = captured["args"][2]
    assert since == datetime(2026, 10, 24, 2, 35, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_verdict_query_failure_is_missing_not_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, error=RuntimeError("db down"))

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(_full_files()),
        workspace_id=WS,
    )
    assert not result["done"]
    assert any("verdict 无法核对" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_verdict_matched_on_real_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The earnings-deep contract wants word in the five with trigger_kind=
    earnings — rows are matched on the real ``word``/``trigger_kind`` columns,
    so a row with the right word but the wrong trigger does not satisfy it."""
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="manual")])
    files = {"drafts/2026-10-28-deep-FY26Q3.md": "深度" * 300}

    result = await run_check_done(
        workflow="earnings-deep-update",
        params={
            "ticker": "META",
            "quarter": "FY26Q3",
            "workspace_dir": ".",
            "release_date": "2026-10-28",
        },
        backend=_make_backend(files),
        workspace_id=WS,
    )
    assert not result["done"]
    assert any("verdict" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_missing_review_pass_is_missing_not_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(_full_files(with_review_pass=False)),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("审稿" in item and ("run 目录" in item or "审稿/复审记录" in item) for item in result["missing"])
    assert all("审稿" not in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_stale_check_draft_hash_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """full-coverage's check_draft is required: a sidecar holding the
    pre-revision hash is a missing item, not a warning."""
    draft = "# 备忘录（修订后）\n" + "内容" * 400
    files = {
        "research/2026-10-23-fc/facts.md": "<!-- FACTS-LEDGER v1 -->\n" + "底表" * 200,
        "research/2026-10-23-fc/questions.md": "问题" * 200,
        "research/2026-10-23-fc/ea.md": "A " * 200 + "\n<!-- 完成 -->",
        "research/2026-10-23-fc/eb.md": "B " * 200 + "\n<!-- 完成 -->",
        "research/2026-10-23-fc/ec.md": "C " * 200 + "\n<!-- 完成 -->",
        "research/2026-10-23-fc/ed.md": "D " * 200 + "\n<!-- 完成 -->",
        "model/META-model-v01.xlsx": "x" * 1200,
        "drafts/2026-10-23-memo.md": draft,
        "research/2026-10-23-fc/model-check.md": "审模型" * 100,
        "research/2026-10-23-fc/knowledge-revision.json": json.dumps(
            {"thesis": {"ops": [{"op": "set", "value": "x" * 40}]}}
        ),
        "drafts/2026-10-23-memo.audit.md": (
            "# 数字核验报告\n- **草稿哈希**：`"
            + _draft_hash("# 备忘录\n" + "内容" * 400)[:16]
            + "…`\n"
        ),
    }
    _patch_knowledge(
        monkeypatch, ["company", "thesis", "debate", "drivers", "management", "models"]
    )
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="full-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
    )
    assert not result["done"]
    assert any("旧草稿" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_latest_anchor_uses_today_and_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``as_of="latest"`` (a topic run without a date) must not raise: paths
    render to the ``latest`` directory and the since-floor is the manual
    window's (no run, no thread start readable → the last 24 hours)."""
    files = {
        "research/latest-topic-ai-capex/plan.md": "规划" * 100,
        "research/latest-topic-ai-capex/primary.md": "一手" * 100,
        "research/latest-topic-ai-capex/cluster-c1.md": "簇" * 100,
        "research/latest-topic-ai-capex/cluster-c2.md": "簇" * 100,
        "research/latest-topic-ai-capex/cluster-c3.md": "簇" * 100,
        "research/latest-topic-ai-capex/internal-memory.md": "记忆" * 50,
        "research/latest-topic-ai-capex/guidepoint.md": "专家" * 50,
        "research/latest-topic-ai-capex/cross-check.md": "交叉验证 盲区检查完成",
        "drafts/latest-topic-ai-capex.md": "专题" * 300,
        "research/latest-topic-ai-capex/number-audit.md": "核验" * 50,
    }
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS", trigger_kind="agenda")])

    result = await run_check_done(
        workflow="topic-research",
        params={"topic": "AI capex", "slug": "ai-capex", "workspace_dir": ".", "as_of": "latest"},
        backend=_make_backend(files),
        workspace_id=None,
    )
    assert result["done"] is False  # knowledge unverified (no workspace) but no crash
    # The 24h fallback window starts exactly a day back, so its UTC date is
    # today's or yesterday's — never older.
    since_date = date.fromisoformat(result["checked"]["since"])
    today = date(*time.gmtime()[:3])
    assert today - timedelta(days=1) <= since_date <= today


@pytest.mark.asyncio
async def test_validator_params_are_shell_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A param carrying ``$(touch x)`` must arrive as one quoted shell word —
    the validator re-runs in the sandbox, so an unquoted param executes."""
    from src.dalton.workflows.contracts import render_command

    rendered = render_command(
        "python tool.py --ticker {ticker}", {"ticker": "META$(touch x)"}
    )
    assert "'META$(touch x)'" in rendered

    # And the backend never sees an unquoted substitution when the tool runs.
    files = {"research/2026-10-23-flash-FY26Q3.md": "快讯" * 200}
    backend = _make_backend(files)
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="earnings")])
    await run_check_done(
        workflow="dalton-earnings-flash",
        params={"ticker": "META", "quarter": "FY26Q3", "as_of": "2026-10-23"},
        backend=backend,
        workspace_id=WS,
    )
    for call in backend.aexecute.call_args_list:
        assert "$(touch" not in call.args[0]


@pytest.mark.asyncio
async def test_weekly_core_files_derive_from_screen_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-ticker core deliverables come off the screening result: a
    ticker in screen.json without its core file is a missing item."""
    files = {
        "dalton/research/weekly-2026-10-23/screen.json": json.dumps(
            {"core": [{"ticker": "META"}, {"ticker": "NFLX"}], "flagged": []}
        ),
        "dalton/research/weekly-2026-10-23/attribution.json": json.dumps({"rows": []}),
        "dalton/research/weekly-2026-10-23/core-META.md": " META " * 100,
        "dalton/drafts/2026-10-23-weekly.md": "周会" * 300,
    }

    result = await run_check_done(
        workflow="weekly-coverage-review",
        params={"home_dir": "dalton", "end": "2026-10-23", "coverage_path": "dalton/coverage.yaml"},
        backend=_make_backend(files),
        workspace_id=None,
    )
    assert not result["done"]
    assert any("core-NFLX.md" in item for item in result["missing"])
    assert not any("core-META.md" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_internal_error_returns_structured_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever breaks inside, the tool answers {done:false, missing:[...]} —
    never an exception the model has to catch."""
    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("contract loader exploded")

    monkeypatch.setattr(check_done_tools, "load_contract", _explode)
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(_full_files()),
        workspace_id=WS,
    )
    assert result["done"] is False
    assert any("无法核对" in item and "请重试" in item for item in result["missing"])


# ------------------------------------------------- round-3 invariants


@pytest.mark.asyncio
async def test_review_pass_from_another_run_does_not_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass recorded under a DIFFERENT workflow's run directory is not this
    run's review — the newest same-workflow run is the scope."""
    files = _full_files()
    # Rename the run's workflow marker so the pass sits in another workflow's
    # directory as far as discovery is concerned.
    files[".agents/threads/short-1/workflows/task-run-1/status.json"] = json.dumps(
        {"run_id": "task-run-1", "name": "some-other-workflow", "status": "completed", "updated_at": 1.0}
    )
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("run 目录" in item and "审稿" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_review_pass_hash_must_match_current_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded pass whose draft_sha256 is the PRE-revision draft is a
    missing item: the review covered a draft that is no longer there."""
    files = _full_files(draft="# 初筛（修订后）\n" + "内容" * 400)
    # The journal still records the pre-revision draft's hash.
    files[JOURNAL_PASS] = json.dumps(
        {
            "label": "审稿",
            "dispatches": [
                {
                    "label": "审稿",
                    "status": "ok",
                    "seq": 7,
                    "recorded_at": 2.0,
                    "result": {
                        "verdict": "pass",
                        "summary": "通过",
                        "draft_sha256": _draft_hash("# 初筛\n" + "内容" * 400),
                    },
                }
            ],
        },
        ensure_ascii=False,
    )
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("旧草稿" in item and "重新审稿" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_run_id_scopes_the_review_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """With run_id given, only that run's journal is consulted — an older
    run's pass does not cover this one."""
    files = _full_files()
    # A second, newer run whose review is a revise.
    files[".agents/threads/short-1/workflows/task-run-2/status.json"] = json.dumps(
        {"run_id": "task-run-2", "name": "initial-coverage", "status": "completed", "updated_at": 9.0}
    )
    files[".agents/threads/short-1/workflows/task-run-2/journal/审稿.json"] = json.dumps(
        {
            "label": "审稿",
            "dispatches": [
                {
                    "label": "审稿",
                    "status": "ok",
                    "seq": 1,
                    "recorded_at": 8.0,
                    "result": {
                        "verdict": "revise",
                        "summary": "P0",
                        "draft_sha256": "f" * 64,
                    },
                }
            ],
        },
        ensure_ascii=False,
    )
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        run_id="task-run-2",
    )
    assert not result["done"]
    assert any("不是 pass" in item for item in result["missing"])




# ------------------------------------------------- round-4 invariants


@pytest.mark.asyncio
async def test_unreadable_review_journal_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A journal file that exists but cannot be read (or parsed) is 无法核对
    — never a silent skip that reads as 'no review'."""
    files = _full_files()
    files[JOURNAL_PASS] = "{broken json"

    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        run_id="task-run-1",
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("不可解析" in item and "请重试" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_last_review_record_judged_even_when_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LAST review record is selected before any filtering: an earlier
    pass followed by a failed re-review dispatch is NOT a pass."""
    files = _full_files()
    files[JOURNAL_PASS] = json.dumps(
        {
            "label": "审稿",
            "dispatches": [
                {
                    "label": "审稿",
                    "status": "ok",
                    "seq": 5,
                    "recorded_at": 1.0,
                    "result": {"verdict": "pass", "summary": "ok", "draft_sha256": "a" * 64},
                },
                {
                    "label": "复审 1",
                    "status": "timeout",
                    "seq": 6,
                    "recorded_at": 2.0,
                    "error": "timed out",
                },
            ],
        },
        ensure_ascii=False,
    )
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        run_id="task-run-1",
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("最后一次审稿派发未成功" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_review_scan_stays_inside_the_calling_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without run_id, the newest-run discovery globs only this thread's
    directories — a newer run in ANOTHER thread cannot stand in."""
    files = _full_files()
    # Another thread's run, newer than ours.
    files[".agents/threads/other01/workflows/task-run-9/status.json"] = json.dumps(
        {"run_id": "task-run-9", "name": "initial-coverage", "status": "completed", "updated_at": 99.0}
    )
    files[".agents/threads/other01/workflows/task-run-9/journal/审稿.json"] = json.dumps(
        {
            "label": "审稿",
            "dispatches": [
                {
                    "label": "审稿",
                    "status": "ok",
                    "seq": 1,
                    "recorded_at": 90.0,
                    "result": {"verdict": "pass", "summary": "ok", "draft_sha256": "a" * 64},
                }
            ],
        },
        ensure_ascii=False,
    )
    # Our thread's pass carries a MISMATCHED hash → missing.
    files[JOURNAL_PASS] = json.dumps(
        {
            "label": "审稿",
            "dispatches": [
                {
                    "label": "审稿",
                    "status": "ok",
                    "seq": 7,
                    "recorded_at": 2.0,
                    "result": {"verdict": "pass", "summary": "ok", "draft_sha256": "f" * 64},
                }
            ],
        },
        ensure_ascii=False,
    )
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("旧草稿" in item or "重新审稿" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_unknown_name_reports_undefined_contract() -> None:
    result = await run_check_done(
        workflow="dalton-agenda",
        params={"ticker": "META", "as_of": "2026-10-23"},
        backend=_make_backend({}),
        workspace_id=WS,
    )
    assert result == {
        "done": False,
        "missing": ["dalton-agenda 尚未定义完成契约"],
        "warnings": [],
        "checked": {
            "workflow": "dalton-agenda",
            "since": result["checked"]["since"],
            "run_id": None,
        },
    }


def _freeze_execution_day(monkeypatch: pytest.MonkeyPatch, day: str) -> None:
    """Pin the tool's execution clock so mtime math cannot drift across a
    real midnight; mtimes then state exactly where they sit between the
    report day and the execution day."""
    from datetime import datetime as _dt

    frozen = _dt.fromisoformat(f"{day}T00:00:00+00:00")

    def _today() -> Any:
        return frozen

    monkeypatch.setattr(check_done_tools, "_today_start_utc", _today)


# ------------------------------------------------- round-5 invariants


@pytest.mark.asyncio
async def test_modified_on_today_ignores_report_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``modified_on: "today"`` floors at the EXECUTION day: a model written
    on the release date — between release_date and today — is not fresh,
    even though release_date sits in the params."""
    release_day = "2026-10-20"
    params = {
        "ticker": "META",
        "quarter": "FY26Q3",
        "workspace_dir": ".",
        "release_date": release_day,
        "model_path": "model/META-model-v01.xlsx",
    }
    files = {
        "drafts/2026-10-20-deep-FY26Q3.md": "深度" * 300,
        "research/2026-10-20-deep-FY26Q3/reconcile.md": "对账" * 50,
        "research/2026-10-20-deep-FY26Q3/reconcile.json": '{"wrong": 0, "partial": 0}',
        "research/2026-10-20-deep-FY26Q3/thesis-calibration.md": "校准" * 100,
        "research/2026-10-20-deep-FY26Q3/management-delta.md": "指引" * 50,
        "research/2026-10-20-deep-FY26Q3/knowledge-revision.json": '{"thesis": {}}',
        "research/2026-10-20-deep-FY26Q3/mapping.md": "映射" * 50,
        "research/2026-10-20-deep-FY26Q3/model-delta.md": "模型增量" * 50,
        "model/META-model-v01.xlsx": "x" * 1200,
    }
    _patch_knowledge(monkeypatch, [])
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="earnings")])
    _freeze_execution_day(monkeypatch, "2026-10-23")

    from datetime import datetime as _dt
    from datetime import timezone as _tz

    # Explicitly between the release day (10-20) and the execution day (10-23).
    stale = int(_dt(2026, 10, 20, 12, tzinfo=_tz.utc).timestamp())
    execution_day_start = int(_dt(2026, 10, 23, tzinfo=_tz.utc).timestamp())
    mtimes = {"model/META-model-v01.xlsx": stale}
    result = await run_check_done(
        workflow="earnings-deep-update",
        params=params,
        backend=_make_backend(files, mtimes=mtimes),
        workspace_id=WS,
    )
    assert any("不是本轮写的" in item and "model-v01" in item for item in result["missing"])

    # Written ON the execution day (any time of it) is fresh.
    mtimes["model/META-model-v01.xlsx"] = execution_day_start
    result = await run_check_done(
        workflow="earnings-deep-update",
        params=params,
        backend=_make_backend(files, mtimes=mtimes),
        workspace_id=WS,
    )
    assert not any("不是本轮写的" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_one_final_model_version_across_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A higher version on disk is THE final model everywhere: mtime and
    size speak only about it — an old-mtime v01 does not fail freshness,
    and a stale v02 does."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    params = {
        "ticker": "META",
        "quarter": "FY26Q3",
        "workspace_dir": ".",
        "release_date": "2026-10-20",
        "model_path": "model/META-model-v01.xlsx",
    }
    files = {
        "drafts/2026-10-20-deep-FY26Q3.md": "深度" * 300,
        "research/2026-10-20-deep-FY26Q3/reconcile.md": "对账" * 50,
        "research/2026-10-20-deep-FY26Q3/reconcile.json": '{"wrong": 0, "partial": 0}',
        "research/2026-10-20-deep-FY26Q3/thesis-calibration.md": "校准" * 100,
        "research/2026-10-20-deep-FY26Q3/management-delta.md": "指引" * 50,
        "research/2026-10-20-deep-FY26Q3/knowledge-revision.json": '{"thesis": {}}',
        "research/2026-10-20-deep-FY26Q3/mapping.md": "映射" * 50,
        "research/2026-10-20-deep-FY26Q3/model-delta.md": "模型增量" * 50,
        "model/META-model-v01.xlsx": "x" * 1200,
        "model/META-model-v02.xlsx": "y" * 1200,
    }
    _patch_knowledge(monkeypatch, [])
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="earnings")])
    _freeze_execution_day(monkeypatch, "2026-10-23")

    old = int(_dt(2026, 10, 20, 12, tzinfo=_tz.utc).timestamp())
    fresh = int(_dt(2026, 10, 23, tzinfo=_tz.utc).timestamp())
    # v01 old, v02 fresh: only v02 is checked — v01's stale mtime is history.
    result = await run_check_done(
        workflow="earnings-deep-update",
        params=params,
        backend=_make_backend(
            files,
            mtimes={"model/META-model-v01.xlsx": old, "model/META-model-v02.xlsx": fresh},
        ),
        workspace_id=WS,
    )
    assert not any("不是本轮写的" in item for item in result["missing"])

    # v02 stale → the ONE final file fails freshness.
    result = await run_check_done(
        workflow="earnings-deep-update",
        params=params,
        backend=_make_backend(
            files,
            mtimes={"model/META-model-v01.xlsx": old, "model/META-model-v02.xlsx": old},
        ),
        workspace_id=WS,
    )
    flagged = [item for item in result["missing"] if "不是本轮写的" in item]
    assert flagged and all("model-v02" in item for item in flagged), flagged


@pytest.mark.asyncio
async def test_no_thread_identity_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without run_id AND without thread_id the run cannot be located — a
    missing entry, never a widened cross-thread glob."""
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(_full_files()),
        workspace_id=WS,
        run_id=None,
        thread_id=None,
    )
    assert not result["done"]
    assert any("缺少线程身份" in item for item in result["missing"])


# ------------------------------------------------- manual fallback path
# workflow.enabled=false: no run journal exists, so `manual: true` lets the
# reviewer JSON the analyst saved at the contract's manual_sidecar stand in —
# checked the same way (pass + draft_sha256 of the CURRENT draft).

MANUAL_SIDECAR = "research/2026-10-23-ic/review.json"
MANUAL_PARAMS = {**PARAMS, "manual": True}


def _manual_files(draft_sha: str | None = None, verdict: str = "pass") -> dict[str, str]:
    files = _full_files(with_review_pass=False)
    files[MANUAL_SIDECAR] = json.dumps(
        {
            "verdict": verdict,
            "summary": "ok",
            "must_fix": [],
            "draft_sha256": draft_sha or _draft_hash(files[DRAFT]),
        },
        ensure_ascii=False,
    )
    return files


async def _manual_check(monkeypatch: pytest.MonkeyPatch, files: dict[str, str], params: dict[str, Any]) -> dict[str, Any]:
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])
    return await run_check_done(
        workflow="initial-coverage",
        params=params,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )


@pytest.mark.asyncio
async def test_manual_matching_pass_sidecar_satisfies_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _manual_check(monkeypatch, _manual_files(), MANUAL_PARAMS)
    assert result["done"], result
    assert result["missing"] == []


@pytest.mark.asyncio
async def test_manual_stale_sidecar_hash_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await _manual_check(monkeypatch, _manual_files(draft_sha="f" * 64), MANUAL_PARAMS)
    assert not result["done"]
    assert any("旧草稿" in item and "重新审稿" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_manual_sidecar_last_verdict_must_be_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _manual_files()
    passed = json.loads(files[MANUAL_SIDECAR])
    files[MANUAL_SIDECAR] = json.dumps([passed, {**passed, "verdict": "revise"}])
    result = await _manual_check(monkeypatch, files, MANUAL_PARAMS)
    assert not result["done"]
    assert any("不是 pass" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_manual_without_sidecar_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _manual_files()
    del files[MANUAL_SIDECAR]
    result = await _manual_check(monkeypatch, files, MANUAL_PARAMS)
    assert not result["done"]
    assert any(MANUAL_SIDECAR in item for item in result["missing"])


@pytest.mark.asyncio
async def test_without_manual_flag_sidecar_does_not_count(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await _manual_check(monkeypatch, _manual_files(), PARAMS)
    assert not result["done"]
    assert any("run 目录" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_manual_with_existing_run_dir_still_uses_the_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow run exists for this thread: its journal (here a revise)
    decides, and a passing manual sidecar cannot override it."""
    files = _full_files()
    files[JOURNAL_PASS] = json.dumps(
        {
            "label": "审稿",
            "dispatches": [
                {
                    "label": "审稿",
                    "status": "ok",
                    "seq": 7,
                    "recorded_at": 2.0,
                    "result": {"verdict": "revise", "summary": "P0", "draft_sha256": "f" * 64},
                }
            ],
        },
        ensure_ascii=False,
    )
    files[MANUAL_SIDECAR] = _manual_files()[MANUAL_SIDECAR]
    result = await _manual_check(monkeypatch, files, MANUAL_PARAMS)
    assert not result["done"]
    assert any("不是 pass" in item and "本次 run" in item for item in result["missing"])


# ------------------------------------------------- chief_of_staff binding


def _tool_call(workflow: str) -> dict[str, Any]:
    return {
        "name": "dalton_check_done",
        "args": {
            "workflow": workflow,
            "params": {
                "home_dir": ".",
                "end": "2026-10-23",
                "as_of": "2026-10-23",
                "coverage_path": "c.json",
            },
        },
        "id": "call-1",
        "type": "tool_call",
    }


@pytest.mark.asyncio
async def test_chief_of_staff_check_done_only_accepts_weekly_review() -> None:
    from src.dalton.tools import build_dalton_tools

    tools = {t.name: t for t in build_dalton_tools("chief_of_staff", enabled=True)}
    assert "dalton_check_done" in tools
    assert "check_language" in tools
    assert "publish_report" not in tools
    assert "check_draft" not in tools

    analyst_tools = {t.name: t for t in build_dalton_tools("analyst", enabled=True)}
    assert {"check_draft", "check_language", "publish_report"}.issubset(analyst_tools)

    tool = tools["dalton_check_done"]

    for company_wf in ("initial-coverage", "dalton-earnings-flash", "dalton-stock-move"):
        refused = json.loads((await tool.ainvoke(_tool_call(company_wf))).content)
        assert refused["done"] is False
        assert refused["missing"] == [
            "该角色只能核对 dalton-earnings-calendar、weekly-coverage-review"
        ]

    # weekly-coverage-review and dalton-earnings-calendar pass the role gate and
    # reach the real check (no backend bound here → the no-backend missing, not the role refusal).
    for home_wf in ("weekly-coverage-review", "dalton-earnings-calendar"):
        allowed_res = json.loads((await tool.ainvoke(_tool_call(home_wf))).content)
        assert allowed_res["done"] is False
        assert allowed_res["missing"] == ["当前没有可用的文件后端，无法核对工作区产物。"]


@pytest.mark.asyncio
async def test_analyst_check_done_has_no_role_restriction() -> None:
    tool = check_done_tools.create_check_done_tools(None)[0]
    result = json.loads((await tool.ainvoke(_tool_call("initial-coverage"))).content)
    assert all("该角色" not in item for item in result["missing"])


@pytest.mark.asyncio
async def test_manual_sidecar_refused_while_an_unfinished_run_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-flight or crashed run has workflow.js and a journal but no
    status.json yet: that is still a run, so a passing manual review.json
    must not stand in for the journal's revise."""
    files = _manual_files()
    files[".agents/threads/short-1/workflows/task-run-3/workflow.js"] = "return 1;"
    files[".agents/threads/short-1/workflows/task-run-3/journal/审稿.json"] = json.dumps(
        {
            "label": "审稿",
            "dispatches": [
                {"label": "审稿", "status": "ok", "seq": 1, "recorded_at": 1.0,
                 "result": {"verdict": "revise", "summary": "P0", "draft_sha256": "f" * 64}}
            ],
        },
        ensure_ascii=False,
    )
    result = await _manual_check(monkeypatch, files, MANUAL_PARAMS)
    assert not result["done"], result


@pytest.mark.asyncio
async def test_unfinished_newer_run_blocks_falling_back_to_an_older_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older finished run passed review; a newer run on the same thread is
    still going (no status.json) and its review said revise. Without run_id the
    check must not fall back to the older pass."""
    files = _full_files()  # includes the finished task-run-1 with a pass
    files[".agents/threads/short-1/workflows/task-run-4/workflow.js"] = "return 1;"
    files[".agents/threads/short-1/workflows/task-run-4/journal/审稿.json"] = json.dumps(
        {"label": "审稿", "dispatches": [{"label": "审稿", "status": "ok", "seq": 1, "recorded_at": 9.0,
          "result": {"verdict": "revise", "summary": "P0", "draft_sha256": "f" * 64}}]},
        ensure_ascii=False,
    )
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])
    result = await run_check_done(
        workflow="initial-coverage", params=PARAMS, backend=_make_backend(files), workspace_id=WS, thread_id="short-1"
    )
    assert not result["done"]
    assert any("未结束" in item for item in result["missing"]), result["missing"]


@pytest.mark.asyncio
async def test_corrupt_newer_status_blocks_falling_back_to_an_older_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer run's truncated status.json cannot be told apart from the
    newest run of this workflow, so the older pass must not stand in."""
    files = _full_files()
    files[".agents/threads/short-1/workflows/task-run-6/workflow.js"] = "return 1;"
    files[".agents/threads/short-1/workflows/task-run-6/status.json"] = '{"name": "initial-cov'
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])
    result = await run_check_done(
        workflow="initial-coverage", params=PARAMS, backend=_make_backend(files), workspace_id=WS, thread_id="short-1"
    )
    assert not result["done"]
    assert any("损坏" in item for item in result["missing"]), result["missing"]

    # A readable status.json with no name: same undeterminable situation.
    files[".agents/threads/short-1/workflows/task-run-6/status.json"] = '{"updated_at": 9.0}'
    result = await run_check_done(
        workflow="initial-coverage", params=PARAMS, backend=_make_backend(files), workspace_id=WS, thread_id="short-1"
    )
    assert not result["done"]
    assert any("缺少 name" in item for item in result["missing"]), result["missing"]


@pytest.mark.asyncio
async def test_newer_status_of_another_workflow_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer run's status.json that parses and names a DIFFERENT workflow is
    safely skipped; the older same-name run decides."""
    files = _full_files()
    files[".agents/threads/short-1/workflows/task-run-6/workflow.js"] = "return 1;"
    files[".agents/threads/short-1/workflows/task-run-6/status.json"] = json.dumps(
        {"run_id": "task-run-6", "name": "full-coverage", "status": "completed", "updated_at": 9.0}
    )
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])
    result = await run_check_done(
        workflow="initial-coverage", params=PARAMS, backend=_make_backend(files), workspace_id=WS, thread_id="short-1"
    )
    assert result["done"], result["missing"]


@pytest.mark.asyncio
async def test_manual_sidecar_refused_when_only_a_journal_marks_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launch snapshot (workflow.js) failed to write, but the run's journal
    exists: that is a run, so the manual review.json does not count."""
    files = _manual_files()
    files[".agents/threads/short-1/workflows/task-run-5/journal/审稿.json"] = json.dumps(
        {"label": "审稿", "dispatches": [{"label": "审稿", "status": "ok", "seq": 1, "recorded_at": 1.0,
          "result": {"verdict": "revise", "summary": "P0", "draft_sha256": "f" * 64}}]},
        ensure_ascii=False,
    )
    result = await _manual_check(monkeypatch, files, MANUAL_PARAMS)
    assert not result["done"], result


# ------------------------------------------------- multi-step skill contracts


def _sidecar_for(draft_text: str) -> str:
    return (
        "# 数字核验报告\n- **草稿哈希**：`" + _draft_hash(draft_text)[:16] + "…`\n"
    )


PREVIEW_PARAMS = {"ticker": "META", "quarter": "FY26Q3", "as_of": "2026-10-23"}
PREVIEW_DRAFT = "drafts/2026-10-23-preview-FY26Q3.md"
PREVIEW_PUSH = "research/push/2026-10-23-earnings-preview-FY26Q3.md"
PREVIEW_PRED = "research/2026-10-23-preview-FY26Q3/predictions.json"


def _preview_files(draft_text: str = "# 前瞻\n" + "判断" * 300) -> dict[str, str]:
    push_text = "## 结论\n维持。\n"
    return {
        PREVIEW_DRAFT: draft_text,
        "drafts/2026-10-23-preview-FY26Q3.audit.md": _sidecar_for(draft_text),
        f"{PREVIEW_DRAFT}.language.json": _language_json(draft_text),
        PREVIEW_PUSH: push_text,
        f"{PREVIEW_PUSH}.language.json": _language_json(push_text),
        PREVIEW_PRED: json.dumps({"predictions": [{"id": "P1", "metric": "revenue"}] * 3}),
    }


@pytest.mark.asyncio
async def test_earnings_preview_complete_reports_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_knowledge(monkeypatch, ["drivers", "debate"])
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="earnings")])
    backend = _make_backend(_preview_files())
    result = await run_check_done(
        workflow="dalton-earnings-preview",
        params=PREVIEW_PARAMS,
        backend=backend,
        workspace_id=WS,
        thread_id="short-1",
    )
    assert result["done"] is True, result["missing"]
    # The strict predictions validator really re-ran.
    assert any(
        "predictions.py validate" in call.args[0] and "--strict" in call.args[0]
        for call in backend.aexecute.call_args_list
    )


@pytest.mark.asyncio
async def test_earnings_preview_reports_each_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed strict validation, a sidecar of an older draft, a missing debate
    version and a verdict on the wrong trigger are four separate gaps."""
    files = _preview_files()
    files[PREVIEW_DRAFT] = files[PREVIEW_DRAFT] + "\n改稿后新增一段。"
    _patch_knowledge(monkeypatch, ["drivers"])
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="manual")])
    result = await run_check_done(
        workflow="dalton-earnings-preview",
        params=PREVIEW_PARAMS,
        backend=_make_backend(files, validator_exit=1),
        workspace_id=WS,
        thread_id="short-1",
    )
    missing = result["missing"]
    assert result["done"] is False
    assert any("校验未通过" in m and "predictions.py" in m for m in missing)
    assert any("旧草稿" in m for m in missing)
    assert any("知识层 debate" in m for m in missing)
    assert any("trigger_kind=earnings" in m for m in missing)


FLASH_PUSH = "research/push/2026-10-23-earnings-flash-FY26Q3.md"
MOVE_PUSH = "research/push/2026-10-23-stock-move.md"


def _round1_only_language_json(draft_text: str) -> str:
    doc = json.loads(_language_json(draft_text))
    doc["rounds"] = [doc["rounds"][0]]
    return json.dumps(doc, ensure_ascii=False)


@pytest.mark.asyncio
async def test_earnings_flash_needs_research_note_and_earnings_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    params = {"ticker": "META", "quarter": "FY26Q3", "as_of": "2026-10-23"}
    push_text = "## 结论\n强化。\n"
    note = {
        "research/2026-10-23-flash-FY26Q3.md": "快讯" * 200,
        FLASH_PUSH: push_text,
        f"{FLASH_PUSH}.language.json": _language_json(push_text),
    }
    _patch_knowledge(monkeypatch, [])
    _patch_verdicts(monkeypatch, [_verdict_row("THESIS_STRENGTHENED", trigger_kind="earnings")])
    done = await run_check_done(
        workflow="dalton-earnings-flash", params=params,
        backend=_make_backend(note), workspace_id=WS, thread_id="short-1",
    )
    assert done["done"] is True, done["missing"]

    round1_only = dict(note)
    round1_only[f"{FLASH_PUSH}.language.json"] = _round1_only_language_json(push_text)
    missing_r2 = await run_check_done(
        workflow="dalton-earnings-flash", params=params,
        backend=_make_backend(round1_only), workspace_id=WS, thread_id="short-1",
    )
    assert missing_r2["done"] is False
    assert any("缺少第 2 轮" in m and FLASH_PUSH in m for m in missing_r2["missing"])

    # Stale push draft (mtime earlier than this run's start) fails.
    stale_res = await run_check_done(
        workflow="dalton-earnings-flash",
        params=params,
        backend=_make_backend(note, mtimes={FLASH_PUSH: 1_700_000_000}),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert stale_res["done"] is False
    assert any("早于本轮运行开始时间" in m and FLASH_PUSH in m for m in stale_res["missing"])

    gap = await run_check_done(
        workflow="dalton-earnings-flash", params=params,
        backend=_make_backend({}), workspace_id=WS, thread_id="short-1",
    )
    assert any("research/2026-10-23-flash-FY26Q3.md" in m for m in gap["missing"])
    assert any("缺少推送稿" in m and FLASH_PUSH in m for m in gap["missing"])


MOVE_PARAMS = {"ticker": "META", "as_of": "2026-10-23"}
MOVE_FILES = {"research/2026-10-23-stock-move.md": "归因" * 200}


@pytest.mark.asyncio
async def test_stock_move_no_change_needs_no_knowledge_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every call in this test names a conversation run; give it a readable
    # start so the window anchors (a named run without one is its own case).
    monkeypatch.setattr(
        check_done_tools,
        "_run_start_epoch",
        create_autospec(
            check_done_tools._run_start_epoch,
            return_value=int(time.time()) - 3600,
        ),
    )
    _patch_knowledge(monkeypatch, [])
    _patch_verdicts(
        monkeypatch,
        [_verdict_row("NO_CHANGE", trigger_kind="price_move", run_id="run-nc")],
    )
    result = await run_check_done(
        workflow="dalton-stock-move",
        params=MOVE_PARAMS,
        backend=_make_backend(MOVE_FILES),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="run-nc",
    )
    assert result["done"] is True, result["missing"]

    # An older push draft sitting at MOVE_PUSH from a prior run is also skipped when NO_CHANGE.
    old_push = "## 结论\n旧推送稿。\n"
    with_stale_push = {
        **MOVE_FILES,
        MOVE_PUSH: old_push,
        f"{MOVE_PUSH}.language.json": _language_json(old_push),
    }
    stale_no_change = await run_check_done(
        workflow="dalton-stock-move",
        params=MOVE_PARAMS,
        backend=_make_backend(with_stale_push, mtimes={MOVE_PUSH: 1_700_000_000}),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="run-nc",
    )
    assert stale_no_change["done"] is True, stale_no_change["missing"]

    # NO_CHANGE with a fresh push draft written in this round and reviewed for 2 rounds also passes.
    fresh_no_change = await run_check_done(
        workflow="dalton-stock-move",
        params=MOVE_PARAMS,
        backend=_make_backend(with_stale_push),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="run-nc",
    )
    assert fresh_no_change["done"] is True, fresh_no_change["missing"]

    # Same-day morning run had THESIS_STRENGTHENED (and updated debate), while the
    # current afternoon run has NO_CHANGE and no push draft of its own -> passes.
    _patch_knowledge(monkeypatch, ["debate"])
    _patch_verdicts(
        monkeypatch,
        [
            _verdict_row("THESIS_STRENGTHENED", trigger_kind="price_move", run_id="run-morning"),
            _verdict_row("NO_CHANGE", trigger_kind="price_move", run_id="run-afternoon"),
        ],
    )
    afternoon_no_change = await run_check_done(
        workflow="dalton-stock-move",
        params=MOVE_PARAMS,
        backend=_make_backend(MOVE_FILES),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="run-afternoon",
    )
    assert afternoon_no_change["done"] is True, afternoon_no_change["missing"]

    # When the current run's verdict cannot be fetched (run_id has no verdict row)
    # and there is no push draft -> does NOT skip, reports missing push draft.
    missing_exec_verdict = await run_check_done(
        workflow="dalton-stock-move",
        params=MOVE_PARAMS,
        backend=_make_backend(MOVE_FILES),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="run-unrecorded",
    )
    assert missing_exec_verdict["done"] is False
    assert any("缺少推送稿" in m and MOVE_PUSH in m for m in missing_exec_verdict["missing"])


@pytest.mark.asyncio
async def test_stock_move_changed_verdict_requires_a_knowledge_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    push_up_text = "## 结论\n单日大涨 6%，论点强化。\n"
    _patch_knowledge(monkeypatch, [])
    _patch_verdicts(
        monkeypatch,
        [_verdict_row("THESIS_WEAKENED", trigger_kind="price_move", run_id="conv-run-14pm")],
    )
    result = await run_check_done(
        workflow="dalton-stock-move", params=MOVE_PARAMS,
        backend=_make_backend(MOVE_FILES), workspace_id=WS, thread_id="short-1",
    )
    assert result["done"] is False
    assert any("THESIS_WEAKENED" in m and "非 NO_CHANGE" in m for m in result["missing"])
    assert any("缺少推送稿" in m and MOVE_PUSH in m for m in result["missing"])

    _patch_knowledge(monkeypatch, ["debate"])
    move_round1 = {
        **MOVE_FILES,
        MOVE_PUSH: push_up_text,
        f"{MOVE_PUSH}.language.json": _round1_only_language_json(push_up_text),
    }
    r1_res = await run_check_done(
        workflow="dalton-stock-move", params=MOVE_PARAMS,
        backend=_make_backend(move_round1), workspace_id=WS, thread_id="short-1",
    )
    assert r1_res["done"] is False
    assert any("缺少第 2 轮" in m and MOVE_PUSH in m for m in r1_res["missing"])

    move_up_done = {
        **MOVE_FILES,
        MOVE_PUSH: push_up_text,
        f"{MOVE_PUSH}.language.json": _language_json(push_up_text),
    }
    result = await run_check_done(
        workflow="dalton-stock-move", params=MOVE_PARAMS,
        backend=_make_backend(move_up_done), workspace_id=WS, thread_id="short-1",
    )
    assert result["done"] is True, result["missing"]

    # Same-thread two-round regression THROUGH THE TOOL ENTRYPOINT (`dalton_check_done`
    # tool itself, `config` carries `run_id="conv-run-14pm"`, tool args omit `run_id`):
    # Thread created at 09:00 UTC; morning run wrote MOVE_PUSH at 10:00 UTC and
    # completed 2 rounds; afternoon run created at 14:00 UTC without rewriting -> fails.
    morning_9am = int(datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc).timestamp())
    morning_10am = int(datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc).timestamp())
    afternoon_14pm = int(datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc).timestamp())
    _patch_knowledge(
        monkeypatch,
        ["debate"],
        start_epochs={
            "short-1": morning_9am,
            "conv-run-14pm": afternoon_14pm,
        },
    )
    tool_cfg = {
        "configurable": {
            "user_id": "user-1",
            "workspace_id": WS,
            "thread_id": "short-1",
            "run_id": "conv-run-14pm",
        }
    }
    stale_tool = check_done_tools.create_check_done_tools(
        _make_backend(move_up_done, mtimes={MOVE_PUSH: morning_10am})
    )[0]
    stale_msg = await stale_tool.ainvoke(
        {
            "name": "dalton_check_done",
            "args": {"workflow": "dalton-stock-move", "params": MOVE_PARAMS},
            "id": "call-move-stale",
            "type": "tool_call",
        },
        config=tool_cfg,
    )
    stale_same_day = json.loads(stale_msg.content)
    assert stale_same_day["done"] is False
    assert any("早于本轮运行开始时间" in m and MOVE_PUSH in m for m in stale_same_day["missing"])

    # Once the afternoon run rewrites MOVE_PUSH at/after 14:00 UTC and reviews 2 rounds,
    # calling `dalton_check_done` through the tool entrypoint passes.
    push_down_text = "## 结论\n盘中回落下跌 5%，核心支柱削弱。\n"
    move_down_done = {
        **MOVE_FILES,
        MOVE_PUSH: push_down_text,
        f"{MOVE_PUSH}.language.json": _language_json(push_down_text),
    }
    fresh_tool = check_done_tools.create_check_done_tools(
        _make_backend(move_down_done, mtimes={MOVE_PUSH: afternoon_14pm})
    )[0]
    fresh_msg = await fresh_tool.ainvoke(
        {
            "name": "dalton_check_done",
            "args": {"workflow": "dalton-stock-move", "params": MOVE_PARAMS},
            "id": "call-move-fresh",
            "type": "tool_call",
        },
        config=tool_cfg,
    )
    down_ok = json.loads(fresh_msg.content)
    assert down_ok["done"] is True, down_ok["missing"]


@pytest.mark.asyncio
async def test_preview_and_deep_require_push_draft_even_when_main_draft_reviewed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    push_text = "## 结论\n维持。\n"
    _patch_knowledge(monkeypatch, ["drivers", "debate"])
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="earnings")])

    # 1. dalton-earnings-preview: main draft has 2 rounds of language review, but push draft is missing.
    preview_no_push = _preview_files()
    preview_no_push.pop(PREVIEW_PUSH)
    preview_no_push.pop(f"{PREVIEW_PUSH}.language.json")
    missing_push = await run_check_done(
        workflow="dalton-earnings-preview",
        params=PREVIEW_PARAMS,
        backend=_make_backend(preview_no_push),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert missing_push["done"] is False
    assert any("缺少推送稿" in m and PREVIEW_PUSH in m for m in missing_push["missing"])

    # Push draft with round 1 only -> reports missing round 2.
    preview_r1 = dict(preview_no_push)
    preview_r1[PREVIEW_PUSH] = push_text
    preview_r1[f"{PREVIEW_PUSH}.language.json"] = _round1_only_language_json(push_text)
    r1_res = await run_check_done(
        workflow="dalton-earnings-preview",
        params=PREVIEW_PARAMS,
        backend=_make_backend(preview_r1),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert r1_res["done"] is False
    assert any("缺少第 2 轮" in m and PREVIEW_PUSH in m for m in r1_res["missing"])

    # Push draft written in this round + 2 rounds complete -> passes.
    preview_ok = dict(preview_no_push)
    preview_ok[PREVIEW_PUSH] = push_text
    preview_ok[f"{PREVIEW_PUSH}.language.json"] = _language_json(push_text)
    ok_res = await run_check_done(
        workflow="dalton-earnings-preview",
        params=PREVIEW_PARAMS,
        backend=_make_backend(preview_ok),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert ok_res["done"] is True, ok_res["missing"]

    # 2. earnings-deep-update: main draft has 2 rounds of language review, but push draft is missing.
    deep_params = {
        "ticker": "META",
        "quarter": "FY26Q3",
        "workspace_dir": ".",
        "release_date": "2026-10-23",
        "manual": True,
    }
    deep_draft = "drafts/2026-10-23-deep-FY26Q3.md"
    deep_text = "# 深度点评\n" + "内容" * 300
    deep_push = "research/push/2026-10-23-earnings-deep-FY26Q3.md"
    deep_files = _with_full_language_sidecars(
        {
            deep_draft: deep_text,
            "drafts/2026-10-23-deep-FY26Q3.audit.md": _sidecar_for(deep_text),
            "research/2026-10-23-deep-FY26Q3/thesis-calibration.md": "校准" * 100,
            "research/2026-10-23-deep-FY26Q3/management-delta.md": "指引" * 50,
            "research/2026-10-23-deep-FY26Q3/reconcile.md": "对账" * 50,
            "research/2026-10-23-deep-FY26Q3/reconcile.json": '{"wrong": 0, "partial": 0}',
            "research/2026-10-23-deep-FY26Q3/review.json": json.dumps(
                {
                    "verdict": "pass",
                    "summary": "ok",
                    "must_fix": [],
                    "draft_sha256": _draft_hash(deep_text),
                }
            ),
            "research/2026-10-23-deep-FY26Q3/knowledge-revision.json": json.dumps(
                {"thesis": {"ops": [{"op": "set", "value": "x" * 40}]}}
            ),
        }
    )
    _patch_knowledge(monkeypatch, ["thesis", "debate", "drivers", "management"])

    deep_missing_push = await run_check_done(
        workflow="earnings-deep-update",
        params=deep_params,
        backend=_make_backend(deep_files),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert deep_missing_push["done"] is False
    assert any("缺少推送稿" in m and deep_push in m for m in deep_missing_push["missing"])

    deep_r1 = dict(deep_files)
    deep_r1[deep_push] = push_text
    deep_r1[f"{deep_push}.language.json"] = _round1_only_language_json(push_text)
    deep_r1_res = await run_check_done(
        workflow="earnings-deep-update",
        params=deep_params,
        backend=_make_backend(deep_r1),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert deep_r1_res["done"] is False
    assert any("缺少第 2 轮" in m and deep_push in m for m in deep_r1_res["missing"])

    deep_files[deep_push] = push_text
    deep_files[f"{deep_push}.language.json"] = _language_json(push_text)
    deep_reviewed = await run_check_done(
        workflow="earnings-deep-update",
        params=deep_params,
        backend=_make_backend(deep_files),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert deep_reviewed["done"] is True, deep_reviewed["missing"]


@pytest.mark.asyncio
async def test_weekly_and_earnings_calendar_push_draft_and_unreadable_start_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_knowledge(monkeypatch, [])
    push_text = "## 结论\n本周确认 3 家。\n"

    # 1. weekly-coverage-review
    weekly_params = {
        "home_dir": "dalton",
        "end": "2026-10-23",
        "coverage_path": "dalton/coverage.yaml",
    }
    weekly_push = "research/push/2026-10-23-director-weekly.md"
    weekly_base = {
        "dalton/research/weekly-2026-10-23/screen.json": json.dumps(
            {"core": [{"ticker": "META", "workspace": "Meta"}], "flagged": []}
        ),
        "dalton/research/weekly-2026-10-23/attribution.json": json.dumps({"rows": ["x" * 60]}),
        "dalton/research/weekly-2026-10-23/core-META.md": "META " * 50,
        "dalton/drafts/2026-10-23-weekly.md": "周会" * 300,
    }
    weekly_missing = await run_check_done(
        workflow="weekly-coverage-review",
        params=weekly_params,
        backend=_make_backend(weekly_base),
        workspace_id=None,
        thread_id="short-1",
    )
    assert weekly_missing["done"] is False
    assert any("缺少推送稿" in m and weekly_push in m for m in weekly_missing["missing"])

    weekly_r1 = {
        **weekly_base,
        weekly_push: push_text,
        f"{weekly_push}.language.json": _round1_only_language_json(push_text),
    }
    weekly_r1_res = await run_check_done(
        workflow="weekly-coverage-review",
        params=weekly_params,
        backend=_make_backend(weekly_r1),
        workspace_id=None,
        thread_id="short-1",
    )
    assert weekly_r1_res["done"] is False
    assert any("缺少第 2 轮" in m and weekly_push in m for m in weekly_r1_res["missing"])

    weekly_ok = {
        **weekly_base,
        weekly_push: push_text,
        f"{weekly_push}.language.json": _language_json(push_text),
    }
    weekly_ok_res = await run_check_done(
        workflow="weekly-coverage-review",
        params=weekly_params,
        backend=_make_backend(weekly_ok),
        workspace_id=None,
        thread_id="short-1",
    )
    assert weekly_ok_res["done"] is True, weekly_ok_res["missing"]

    # 2. dalton-earnings-calendar
    cal_path = "research/push/2026-10-23-earnings-calendar.md"
    cal_params = {"as_of": "2026-10-23"}

    cal_missing = await run_check_done(
        workflow="dalton-earnings-calendar",
        params=cal_params,
        backend=_make_backend({}),
        workspace_id=None,
        thread_id="short-1",
    )
    assert cal_missing["done"] is False
    assert any("缺少推送稿" in m and cal_path in m for m in cal_missing["missing"])

    cal_r1 = await run_check_done(
        workflow="dalton-earnings-calendar",
        params=cal_params,
        backend=_make_backend(
            {
                cal_path: push_text,
                f"{cal_path}.language.json": _round1_only_language_json(push_text),
            }
        ),
        workspace_id=None,
        thread_id="short-1",
    )
    assert cal_r1["done"] is False
    assert any("缺少第 2 轮" in m and cal_path in m for m in cal_r1["missing"])

    cal_ok_files = {
        cal_path: push_text,
        f"{cal_path}.language.json": _language_json(push_text),
    }
    cal_ok = await run_check_done(
        workflow="dalton-earnings-calendar",
        params=cal_params,
        backend=_make_backend(cal_ok_files),
        workspace_id=None,
        thread_id="short-1",
    )
    assert cal_ok["done"] is True, cal_ok["missing"]

    # 3. Run start readable: the 5s tolerance holds — mtime = start - 3s
    # counts as current round, while mtime = start - 10s is rejected.
    # (A run id whose start is unreadable now reports the dedicated gap; its
    # own test covers that.)
    _patch_knowledge(
        monkeypatch,
        [],
        start_epochs={"conv-run-unknown": DEFAULT_START_EPOCH, "short-1": DEFAULT_START_EPOCH},
    )
    within_tolerance = await run_check_done(
        workflow="dalton-earnings-calendar",
        params=cal_params,
        backend=_make_backend(cal_ok_files, mtimes={cal_path: DEFAULT_START_EPOCH - 3}),
        workspace_id=None,
        thread_id="short-1",
        conversation_run_id="conv-run-unknown",
    )
    assert within_tolerance["done"] is True, within_tolerance["missing"]

    outside_tolerance = await run_check_done(
        workflow="dalton-earnings-calendar",
        params=cal_params,
        backend=_make_backend(cal_ok_files, mtimes={cal_path: DEFAULT_START_EPOCH - 10}),
        workspace_id=None,
        thread_id="short-1",
        conversation_run_id="conv-run-unknown",
    )
    assert outside_tolerance["done"] is False
    assert any(
        "早于本轮运行开始时间" in m and cal_path in m
        for m in outside_tolerance["missing"]
    )

    # 4. Both run and thread start times unreadable -> fail-closed (reports missing).
    _patch_knowledge(
        monkeypatch,
        [],
        start_epochs={"conv-run-unknown": None, "short-1": None},
    )
    unreadable_start = await run_check_done(
        workflow="dalton-earnings-calendar",
        params=cal_params,
        backend=_make_backend(cal_ok_files),
        workspace_id=None,
        thread_id="short-1",
        conversation_run_id="conv-run-unknown",
    )
    assert unreadable_start["done"] is False
    assert any(
        "无法读取本轮运行开始时间" in m and cal_path in m
        for m in unreadable_start["missing"]
    )


@pytest.mark.asyncio
async def test_stock_move_conditional_knowledge_query_failure_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_knowledge_error(monkeypatch)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS", trigger_kind="price_move")])
    result = await run_check_done(
        workflow="dalton-stock-move", params=MOVE_PARAMS,
        backend=_make_backend(MOVE_FILES), workspace_id=WS,
    )
    assert result["done"] is False
    assert any("条件知识更新无法核对" in m for m in result["missing"])


@pytest.mark.asyncio
async def test_filing_review_accepts_filing_or_manual_trigger_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    params = {"ticker": "META", "as_of": "2026-10-23", "form": "8-K", "accession": "123456"}
    files = {"research/2026-10-23-filing-8-K-123456.md": "审阅" * 200}
    for trigger, expect_done in (("filing", True), ("manual", True), ("earnings", False)):
        _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind=trigger)])
        result = await run_check_done(
            workflow="dalton-filing-review", params=params,
            backend=_make_backend(files), workspace_id=WS,
        )
        assert result["done"] is expect_done, (trigger, result["missing"])


DELTA_PARAMS = {"ticker": "META", "as_of": "2026-10-23"}
LAST_RUN = "research/tracker/last_run.json"


def _delta_files(material: str, *, with_note: bool) -> dict[str, str]:
    files = {
        LAST_RUN: json.dumps({"run_id": "20261023-2000", "material": material}),
        "research/tracker/updates.log": "2026-10-23 20:00 | 20261023-2000 | material=no | 无新增\n",
    }
    if with_note:
        files["research/2026-10-23-delta.md"] = "增量" * 100
    return files


@pytest.mark.asyncio
async def test_thesis_delta_material_no_without_note_is_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_knowledge(monkeypatch, [])
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="indicator")])
    result = await run_check_done(
        workflow="dalton-thesis-delta", params=DELTA_PARAMS,
        backend=_make_backend(_delta_files("no", with_note=False)), workspace_id=WS,
    )
    assert result["done"] is True, result["missing"]


@pytest.mark.asyncio
async def test_thesis_delta_material_yes_needs_note_and_knowledge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_knowledge(monkeypatch, [])
    _patch_verdicts(monkeypatch, [_verdict_row("THESIS_STRENGTHENED", trigger_kind="filing")])
    result = await run_check_done(
        workflow="dalton-thesis-delta", params=DELTA_PARAMS,
        backend=_make_backend(_delta_files("yes", with_note=False)), workspace_id=WS,
    )
    missing = result["missing"]
    assert result["done"] is False
    assert any("research/2026-10-23-delta.md" in m for m in missing)
    assert any("至少需要其一" in m for m in missing)

    _patch_knowledge(monkeypatch, ["drivers"])
    result = await run_check_done(
        workflow="dalton-thesis-delta", params=DELTA_PARAMS,
        backend=_make_backend(_delta_files("yes", with_note=True)), workspace_id=WS,
    )
    assert result["done"] is True, result["missing"]


@pytest.mark.asyncio
async def test_thesis_delta_stale_or_unreadable_last_run_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_execution_day(monkeypatch, "2026-10-23")
    _patch_knowledge(monkeypatch, [])
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE")])
    from datetime import datetime as _dt

    yesterday = int(_dt.fromisoformat("2026-10-22T12:00:00+00:00").timestamp())
    stale = await run_check_done(
        workflow="dalton-thesis-delta", params=DELTA_PARAMS,
        backend=_make_backend(
            _delta_files("no", with_note=False), mtimes={LAST_RUN: yesterday}
        ),
        workspace_id=WS,
    )
    assert any("不是本轮写的" in m and LAST_RUN in m for m in stale["missing"])

    files = _delta_files("no", with_note=False)
    files[LAST_RUN] = "{not json"
    broken = await run_check_done(
        workflow="dalton-thesis-delta", params=DELTA_PARAMS,
        backend=_make_backend(files), workspace_id=WS,
    )
    assert broken["done"] is False
    assert any("条件文件不可解析" in m for m in broken["missing"])


@pytest.mark.asyncio
async def test_thesis_delta_missing_material_field_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-day last_run.json WITHOUT the declared material field is 无法
    核对: reading it as "condition not met" would skip the delta note and the
    knowledge check while everything else passes."""
    _patch_knowledge(monkeypatch, [])
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="indicator")])
    files = _delta_files("no", with_note=False)
    files[LAST_RUN] = json.dumps({"run_id": "20261023-2000"})
    result = await run_check_done(
        workflow="dalton-thesis-delta", params=DELTA_PARAMS,
        backend=_make_backend(files), workspace_id=WS,
    )
    missing = result["missing"]
    assert result["done"] is False
    assert any("缺少字符串字段 material" in m for m in missing)
    assert any(m.startswith("条件文件缺少字符串字段 material") for m in missing)
    assert sum("缺少字符串字段 material" in m for m in missing) == 2  # artifact + knowledge


@pytest.mark.asyncio
async def test_thesis_delta_material_out_of_domain_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """material must be yes or no — any other value is unverifiable, not a
    no-requirements round; yes/no keep their two existing paths."""
    _patch_knowledge(monkeypatch, [])
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="indicator")])
    files = _delta_files("maybe", with_note=False)
    result = await run_check_done(
        workflow="dalton-thesis-delta", params=DELTA_PARAMS,
        backend=_make_backend(files), workspace_id=WS,
    )
    assert result["done"] is False
    assert sum("不在允许值" in m and "material" in m for m in result["missing"]) == 2


CALL_PARAMS = {"ticker": "META", "as_of": "2026-10-23"}
CALL_DRAFT = "drafts/2026-10-23-call-META.md"


@pytest.mark.asyncio
async def test_call_gate_passed_branch_needs_current_check_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = "# Conviction Call\n" + "论证" * 300
    # The gate-passed branch never consults verdicts (they come after PM approval).
    _patch_verdicts(monkeypatch, error=RuntimeError("must not be queried"))
    files = {
        CALL_DRAFT: draft,
        "drafts/2026-10-23-call-META.audit.md": _sidecar_for(draft),
        f"{CALL_DRAFT}.language.json": _language_json(draft),
    }
    result = await run_check_done(
        workflow="dalton-call", params=CALL_PARAMS,
        backend=_make_backend(files), workspace_id=WS,
    )
    assert result["done"] is True, result["missing"]

    files["drafts/2026-10-23-call-META.audit.md"] = _sidecar_for("旧稿")
    stale = await run_check_done(
        workflow="dalton-call", params=CALL_PARAMS,
        backend=_make_backend(files), workspace_id=WS,
    )
    assert any("旧草稿" in m for m in stale["missing"])


@pytest.mark.asyncio
async def test_call_gate_failed_branch_needs_no_change_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE")])
    done = await run_check_done(
        workflow="dalton-call", params=CALL_PARAMS,
        backend=_make_backend({}), workspace_id=WS,
    )
    assert done["done"] is True, done["missing"]

    # Neither signature: no draft and no NO_CHANGE word this round.
    _patch_verdicts(monkeypatch, [_verdict_row("THESIS_WEAKENED")])
    undecided = await run_check_done(
        workflow="dalton-call", params=CALL_PARAMS,
        backend=_make_backend({}), workspace_id=WS,
    )
    assert undecided["done"] is False
    assert any("无法判定门禁结果" in m for m in undecided["missing"])

    _patch_verdicts(monkeypatch, error=RuntimeError("db down"))
    dead = await run_check_done(
        workflow="dalton-call", params=CALL_PARAMS,
        backend=_make_backend({}), workspace_id=WS,
    )
    assert dead["done"] is False
    assert any("数据库查询失败" in m for m in dead["missing"])


@pytest.mark.asyncio
async def test_call_probe_failure_is_not_confirmed_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead sandbox (reads raise, commands fail with no probe marker) must
    not read as "no draft": the gate's absent branch would then clear on a
    same-day NO_CHANGE verdict and report done."""
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE")])
    result = await run_check_done(
        workflow="dalton-call", params=CALL_PARAMS,
        backend=_make_backend({}, read_error=True, execute_error=True),
        workspace_id=WS,
    )
    assert result["done"] is False
    assert any("正式草稿读取出错" in m and "无法判定门禁结果" in m for m in result["missing"])


@pytest.mark.asyncio
async def test_call_empty_file_with_dead_probe_is_not_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An EMPTY draft reads as None without raising (the read layer answers
    None for empty files, files.py): a None read is not proof of absence,
    so with the probe dead (exit 1, no marker) the gate cannot be decided
    and a same-day NO_CHANGE must not clear it."""
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE")])
    result = await run_check_done(
        workflow="dalton-call", params=CALL_PARAMS,
        backend=_make_backend({CALL_DRAFT: ""}, execute_error=True),
        workspace_id=WS,
    )
    assert result["done"] is False
    assert any("正式草稿读取出错" in m and "无法判定门禁结果" in m for m in result["missing"])


@pytest.mark.asyncio
async def test_call_decode_failure_with_dead_probe_is_not_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A UTF-8 decode failure reads as None the same way — existence stays
    unknown, never the absent branch."""
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE")])
    backend = _make_backend(
        {CALL_DRAFT: "# Conviction Call\n" + "论证" * 300},
        execute_error=True,
        read_none_paths={CALL_DRAFT},
    )
    result = await run_check_done(
        workflow="dalton-call", params=CALL_PARAMS,
        backend=backend,
        workspace_id=WS,
    )
    assert result["done"] is False
    assert any("正式草稿读取出错" in m and "无法判定门禁结果" in m for m in result["missing"])


# dalton-call's real publish naming: the DRAFT carries the date, the slug
# does not — the contract's draft_path (not the slug) names draft + sidecars,
# and the slug only builds the archive path.
PUB_PARAMS = {
    "draft_path": "drafts/2026-10-23-call-META.md",
    "slug": "call-META",
    "as_of": "2026-10-23",
}
PUB_DRAFT = PUB_PARAMS["draft_path"]
PUB_ANNOTATED = "drafts/2026-10-23-call-META.annotated.md"


def _publish_files() -> dict[str, str]:
    draft = "# Conviction Call\n" + "论证" * 200
    files = {
        PUB_DRAFT: draft,
        "drafts/2026-10-23-call-META.audit.md": _sidecar_for(draft),
        f"{PUB_DRAFT}.language.json": _language_json(draft),
        PUB_ANNOTATED: "标注" * 50,
    }
    return _with_full_language_sidecars(files)


def test_same_workspace_path_requires_full_match() -> None:
    """The awaiting_pm draft match joins both spellings onto the workspace
    root FIRST and normpath's after, so /root/../root/drafts/x.md is the
    same file as drafts/x.md — while a path that merely ends the same, or
    escapes the workspace, stays a different file (the old suffix compare
    let archive/drafts/x.md stand in for drafts/x.md)."""
    same = check_done_tools._same_workspace_path
    root = "/workspaces/META"
    # Every spelling of THE file matches…
    assert same("drafts/x.md", "drafts/x.md", root)
    assert same("./drafts/x.md", "drafts/x.md", root)
    assert same(f"{root}/drafts/x.md", "drafts/x.md", root)
    assert same(f"{root}/../META/drafts/x.md", "drafts/x.md", root)
    assert same("./drafts/x.md", f"{root}/drafts/x.md", root)
    assert same("drafts/../drafts/x.md", "drafts/x.md", root)
    # …and a path that merely ends the same, or escapes the workspace, does not.
    assert not same("archive/drafts/x.md", "drafts/x.md", root)
    assert not same("../Other/drafts/x.md", "drafts/x.md", root)
    assert not same("/elsewhere/drafts/x.md", "drafts/x.md", root)
    assert not same(None, "drafts/x.md", root)


@pytest.mark.asyncio
async def test_publish_done_when_published_or_awaiting_pm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _publish_files()
    # A real publish_report archive: the draft with the publication banner
    # prepended (different hash), never edited again. It carries no language
    # sidecar of its own — its review is the draft's.
    from src.dalton.publish.files import prepend_publication_banner

    files["published/2026-10-23-call-META.md"] = prepend_publication_banner(
        files[PUB_DRAFT],
        title="Conviction Call",
        kind="conviction_call",
        published_date="2026-10-23",
        approver_user_id="user-1",
        approved_at="2026-10-23T00:00:00Z",
        reviewer_model="gpt-6.1-sol-oauth",
        reviewer_family="openai",
        publication_id="pr-1",
    )
    _patch_pending(monkeypatch, error=RuntimeError("must not be queried"))
    published = await run_check_done(
        workflow="dalton-publish", params=PUB_PARAMS,
        backend=_make_backend(files), workspace_id=WS, user_id="user-1",
    )
    assert published["done"] is True, published["missing"]

    _patch_pending(monkeypatch, [_pending_row("./drafts/2026-10-23-call-META.md")])
    awaiting = await run_check_done(
        workflow="dalton-publish", params=PUB_PARAMS,
        backend=_make_backend(_publish_files()), workspace_id=WS, user_id="user-1",
    )
    assert awaiting["done"] is True, awaiting["missing"]

    # The same request spelled absolutely inside the workspace still matches.
    backend = _make_backend(_publish_files())
    backend.workspace_dir = "/workspaces/ws-1"
    _patch_pending(monkeypatch, [_pending_row("/workspaces/ws-1/drafts/2026-10-23-call-META.md")])
    absolute = await run_check_done(
        workflow="dalton-publish", params=PUB_PARAMS,
        backend=backend, workspace_id=WS, user_id="user-1",
    )
    assert absolute["done"] is True, absolute["missing"]


@pytest.mark.asyncio
async def test_publish_gaps_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Another workspace's request for the same path does not count.
    _patch_pending(monkeypatch, [_pending_row(PUB_DRAFT, workspace_id="ws-other")])
    foreign = await run_check_done(
        workflow="dalton-publish", params=PUB_PARAMS,
        backend=_make_backend(_publish_files()), workspace_id=WS, user_id="user-1",
    )
    assert any("awaiting_pm 发布请求" in m for m in foreign["missing"])

    # A request whose draft_path merely ENDS the same (the old suffix match)
    # is a different file.
    _patch_pending(monkeypatch, [_pending_row("archive/drafts/2026-10-23-call-META.md")])
    suffixed = await run_check_done(
        workflow="dalton-publish", params=PUB_PARAMS,
        backend=_make_backend(_publish_files()), workspace_id=WS, user_id="user-1",
    )
    assert any("awaiting_pm 发布请求" in m for m in suffixed["missing"])

    _patch_pending(monkeypatch, error=RuntimeError("db down"))
    dead = await run_check_done(
        workflow="dalton-publish", params=PUB_PARAMS,
        backend=_make_backend(_publish_files()), workspace_id=WS, user_id="user-1",
    )
    assert any("awaiting_pm 查询失败" in m for m in dead["missing"])

    files = _publish_files()
    del files[PUB_ANNOTATED]
    _patch_pending(monkeypatch, [_pending_row(PUB_DRAFT)])
    no_annotated = await run_check_done(
        workflow="dalton-publish", params=PUB_PARAMS,
        backend=_make_backend(files), workspace_id=WS, user_id="user-1",
    )
    assert any("2026-10-23-call-META.annotated.md" in m for m in no_annotated["missing"])


@pytest.mark.asyncio
async def test_publish_without_draft_path_param_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """slug no longer names the draft: a caller passing only slug+as_of has
    not said which draft it published — a required param gap, fail-closed."""
    _patch_pending(monkeypatch, error=RuntimeError("must not be queried"))
    result = await run_check_done(
        workflow="dalton-publish",
        params={"slug": "call-META", "as_of": "2026-10-23"},
        backend=_make_backend(_publish_files()), workspace_id=WS, user_id="user-1",
    )
    assert result["done"] is False
    assert any("缺少必需参数 'draft_path'" in m for m in result["missing"])


def _patch_pending(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, Any]] | None = None,
    error: Exception | None = None,
) -> None:
    from src.dalton.database import publications as publications_db

    listed = create_autospec(_REAL_LIST_PENDING)

    async def _rows(user_id: str, status: str = "awaiting_pm") -> list[dict[str, Any]]:
        if error is not None:
            raise error
        assert status == "awaiting_pm"
        return rows or []

    listed.side_effect = _rows
    monkeypatch.setattr(publications_db, "list_pending", listed)


def _pending_row(draft_path: str, workspace_id: str = WS) -> dict[str, Any]:
    return {
        "publish_request_id": "pr-1",
        "user_id": "user-1",
        "workspace_id": workspace_id,
        "slug": "call-META",
        "draft_path": draft_path,
        "status": "awaiting_pm",
    }


# ------------------------------------------------- language_review closing rule


@pytest.mark.asyncio
async def test_language_review_missing_round2_is_a_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _full_files()
    doc = json.loads(files[f"{DRAFT}.language.json"])
    doc["rounds"] = [doc["rounds"][0]]  # round 1 only
    files[f"{DRAFT}.language.json"] = json.dumps(doc, ensure_ascii=False)
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("缺少第 2 轮" in m and "check_language" in m for m in result["missing"])


@pytest.mark.asyncio
async def test_language_review_stale_round2_hash_is_a_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _full_files()
    doc = json.loads(files[f"{DRAFT}.language.json"])
    # Round 2 reviewed an older draft: neither the current draft's hash nor
    # the review-pass hash (which equals the current draft's here).
    doc["rounds"][1]["draft_sha256"] = _draft_hash("旧得多的稿子")
    files[f"{DRAFT}.language.json"] = json.dumps(doc, ensure_ascii=False)
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("第 2 轮语言审查针对的是旧草稿" in m for m in result["missing"])


@pytest.mark.asyncio
async def test_language_review_passes_when_round2_matches_the_review_pass_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first disjunct: round 2 recorded the hash the review passed on —
    here the file was edited after that pass, so the review check itself
    fails, but the language review must not add its own gap."""
    files = _full_files()
    draft_text = files[DRAFT]
    review_sha = _draft_hash(draft_text)
    doc = json.loads(files[f"{DRAFT}.language.json"])
    doc["rounds"][1]["draft_sha256"] = review_sha
    files[f"{DRAFT}.language.json"] = json.dumps(doc, ensure_ascii=False)
    # The draft changed after the pass: current hash differs from review_sha.
    files[DRAFT] = draft_text + "\n事后改动。\n"
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("审稿针对的是旧草稿" in m for m in result["missing"])
    assert not any("语言审查" in m for m in result["missing"])


@pytest.mark.asyncio
async def test_language_review_unreadable_sidecar_is_missing_not_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sidecar the read layer answers None for (decode failure) while the
    shell probe sees it present: 无法核对, never a pass."""
    files = _full_files()
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files, read_none_paths={f"{DRAFT}.language.json"}),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("无法核对" in m and "language.json" in m for m in result["missing"])


@pytest.mark.asyncio
async def test_language_review_enumerates_the_runs_reviewable_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Files the run's dispatches reported writing are checked through the
    scope rule: research notes and drafts owe their own two rounds; json/xlsx
    and scraped-data paths don't."""
    files = _full_files()
    # A writer dispatch in the same run's journal reporting its outputs.
    files[".agents/threads/short-1/workflows/task-run-1/journal/取证.json"] = json.dumps(
        {
            "label": "行业支线",
            "dispatches": [
                {
                    "label": "行业支线",
                    "status": "ok",
                    "seq": 3,
                    "recorded_at": 1.0,
                    "result": {
                        "files": [
                            "research/2026-10-23-ic/industry.md",
                            "./research/2026-10-23-ic/lenses.json",
                            "research/2026-10-23-ic/data/raw.md",
                            "drafts/2026-10-23-background.md",
                        ],
                        "summary": "done",
                    },
                }
            ],
        },
        ensure_ascii=False,
    )
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    # industry.md has no language record at all; background.md's is stale.
    files.pop("research/2026-10-23-ic/industry.md.language.json", None)
    files["drafts/2026-10-23-background.md"] = "背景" * 200
    doc = json.loads(_language_json("旧背景" * 200))
    files["drafts/2026-10-23-background.md.language.json"] = json.dumps(doc, ensure_ascii=False)

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )
    missing = result["missing"]
    assert not result["done"]
    assert any(
        "industry.md.language.json" in m and "缺少语言审查记录" in m for m in missing
    )
    assert any(
        "2026-10-23-background.md" in m and "旧草稿" in m for m in missing
    )
    # The machine files reported by the same dispatch are not checked.
    assert not any("lenses.json" in m or "data/raw.md" in m for m in missing)


@pytest.mark.asyncio
async def test_language_review_enumerated_run_passes_when_every_file_carries_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _full_files()
    files[".agents/threads/short-1/workflows/task-run-1/journal/取证.json"] = json.dumps(
        {
            "label": "行业支线",
            "dispatches": [
                {
                    "label": "行业支线",
                    "status": "ok",
                    "seq": 3,
                    "recorded_at": 1.0,
                    "result": {
                        "files": ["research/2026-10-23-ic/industry.md|" + "e" * 64],
                        "summary": "done",
                    },
                }
            ],
        },
        ensure_ascii=False,
    )
    files["research/2026-10-23-ic/industry.md.language.json"] = _language_json(
        files["research/2026-10-23-ic/industry.md"]
    )
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert result["done"], result["missing"]


# ------------------------------- round-4: same-name run, scan, glob counting


@pytest.mark.asyncio
async def test_unrelated_crashed_run_does_not_block_manual_language_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leftover run directory from another (crashed, status-less) workflow
    on the thread must not fail the language check of a skill closing without
    run_id: with no run of THIS workflow, the manual path decides. Preview
    carries no review stage, so the language check is what could block."""
    files = _preview_files()
    files[".agents/threads/short-1/workflows/task-crashed/workflow.js"] = "// other workflow"
    _patch_knowledge(monkeypatch, ["drivers", "debate"])
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="earnings")])

    result = await run_check_done(
        workflow="dalton-earnings-preview",
        params=PREVIEW_PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert result["done"], result["missing"]
    assert not any("未结束的 workflow run" in m for m in result["missing"])


@pytest.mark.asyncio
async def test_same_name_run_is_enumerated_despite_a_crashed_neighbour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a finished same-name run present, its journal drives enumeration
    even when a crashed neighbour directory also sits on the thread."""
    files = _full_files()
    files[".agents/threads/short-1/workflows/task-crashed/workflow.js"] = "// other workflow"
    files[".agents/threads/short-1/workflows/task-run-1/journal/取证.json"] = json.dumps(
        {
            "label": "行业支线",
            "dispatches": [
                {
                    "label": "行业支线",
                    "status": "ok",
                    "seq": 3,
                    "recorded_at": 1.0,
                    "result": {"files": ["research/2026-10-23-ic/industry.md"], "summary": "d"},
                }
            ],
        },
        ensure_ascii=False,
    )
    files.pop("research/2026-10-23-ic/industry.md.language.json", None)
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    # The journal layer ran (the gap names the unreviewed note); the crashed
    # neighbour may still cost the REVIEW stage its own missing entry, which
    # is that check's rule, not the language check's.
    assert any("industry.md.language.json" in m for m in result["missing"])
    assert any("industry.md" in m and "缺少语言审查记录" in m for m in result["missing"])


@pytest.mark.asyncio
async def test_window_scan_catches_files_the_dispatches_never_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hand-written note the manual flow never echoed anywhere is still
    caught by the deterministic mtime scan — and an out-of-window file is not."""
    files = _manual_files()
    fresh = "research/2026-10-23-ic/hand-note.md"
    stale = "research/2026-10-23-ic/old-note.md"
    files[fresh] = "手写笔记" * 50
    files[stale] = "上周的笔记" * 50
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    # Manual window: the as_of day (2026-10-23) at UTC midnight. The fresh
    # note keeps the backend's default mtime (now); the stale one is a day
    # older than the window start.
    result = await run_check_done(
        workflow="initial-coverage",
        params=MANUAL_PARAMS,
        backend=_make_backend(files, mtimes={stale: 1774000000}),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    assert any(f"{fresh}.language.json" in m and "缺少语言审查记录" in m for m in result["missing"])
    assert not any(stale in m for m in result["missing"])


@pytest.mark.asyncio
async def test_window_scan_failure_is_missing_not_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _full_files()
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files, execute_error=True),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("无法扫描工作区的正式产物" in m for m in result["missing"])


@pytest.mark.asyncio
async def test_glob_counting_excludes_language_sidecars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two real cluster notes plus a sidecar must not satisfy a ≥3 glob: the
    sidecar matches ``cluster-*.md`` but never counts."""
    params = {"topic": "AI capex", "slug": "ai-capex", "workspace_dir": ".", "as_of": "2026-10-23"}
    base = "research/2026-10-23-topic-ai-capex"
    files = {
        f"{base}/cluster-gpu.md": "GPU " * 100,
        f"{base}/cluster-power.md": "电力 " * 100,
        # The sidecar that used to pad the count.
        f"{base}/cluster-gpu.md.language.md": "# 语言审查清单\n" + "条目" * 30,
    }
    result = await run_check_done(
        workflow="topic-research",
        params=params,
        backend=_make_backend(files),
        workspace_id=WS,
    )
    assert not result["done"]
    assert any(
        "cluster-*.md" in m and "需要 ≥3 个匹配，实际 2" in m for m in result["missing"]
    )


# ------------------------------------------ round-5: declared paths, window, scan


@pytest.mark.asyncio
async def test_published_archive_needs_no_language_record_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An earlier same-day archive (banner-prefixed, different hash, no
    sidecar) must not block an unrelated manual close either."""
    files = _preview_files()
    files["published/2026-10-23-older-call-AAPL.md"] = "# Dalton 正式发布\n……\n" + "旧归档" * 80
    _patch_knowledge(monkeypatch, ["drivers", "debate"])
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="earnings")])

    result = await run_check_done(
        workflow="dalton-earnings-preview",
        params=PREVIEW_PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert result["done"], result["missing"]


@pytest.mark.asyncio
async def test_contract_declared_note_is_checked_regardless_of_mtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """industry.md is a declared artifact: outside the run's window it still
    owes its two rounds — and a reused note with same-hash records passes
    without any re-review."""
    files = _full_files()
    files.pop("research/2026-10-23-ic/industry.md.language.json", None)
    ancient = 1_600_000_000  # well before the journal's recorded_at stamps
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    missing_sidecar = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files, mtimes={"research/2026-10-23-ic/industry.md": ancient}),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not missing_sidecar["done"]
    assert any(
        "industry.md.language.json" in m and "缺少语言审查记录" in m
        for m in missing_sidecar["missing"]
    )

    # The reused note: old mtime, records bound to its current hash -> pass.
    files["research/2026-10-23-ic/industry.md.language.json"] = _language_json(
        files["research/2026-10-23-ic/industry.md"]
    )
    reused = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files, mtimes={"research/2026-10-23-ic/industry.md": ancient}),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert reused["done"], reused["missing"]


@pytest.mark.asyncio
async def test_manual_stale_round2_with_no_run_asks_for_round2_not_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual close (no same-name run), declared draft's round-2 hash stale:
    the message names the fix (another round 2), not a run hunt."""
    files = _preview_files()
    files[f"{PREVIEW_DRAFT}.language.json"] = _language_json(
        files[PREVIEW_DRAFT], round2_sha=_draft_hash("被改过的旧终稿")
    )
    _patch_knowledge(monkeypatch, ["drivers", "debate"])
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="earnings")])

    result = await run_check_done(
        workflow="dalton-earnings-preview",
        params=PREVIEW_PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    gap = next(m for m in result["missing"] if "language.json" in m)
    assert "在第 2 轮语言审查后被改过" in gap and "check_language(round=2)" in gap
    assert "找不到" not in gap and "无法核对" not in gap


@pytest.mark.asyncio
async def test_scan_rc_nonzero_is_missing_and_missing_roots_are_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _full_files()
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    # find exits nonzero (unreadable directory): the listing is untrustworthy.
    unreadable = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files, scan_rc=1),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not unreadable["done"]
    assert any("无法扫描工作区的正式产物" in m for m in unreadable["missing"])


@pytest.mark.asyncio
async def test_scan_without_any_root_directories_is_empty_not_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No drafts/ and no research/ at all: the scan is empty, not failed."""
    backend = _make_backend({"model/META-model-v01.xlsx": "x" * 1200})
    assert await check_done_tools._scan_reviewable_since(backend, 0) == []


@pytest.mark.asyncio
async def test_manual_window_falls_back_to_last_24h_for_latest_as_of(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """as_of=latest and no thread start readable (no DB in unit tests): the
    window is the past 24 hours — a note from an hour ago counts, one from 25
    hours ago does not."""
    params = {"ticker": "META", "quarter": "FY26Q3", "as_of": "latest"}
    draft = "drafts/latest-preview-FY26Q3.md"
    files = {
        draft: "# 前瞻\n" + "判断" * 300,
        f"{draft}.audit.md": _sidecar_for("# 前瞻\n" + "判断" * 300),
        f"{draft}.language.json": _language_json("# 前瞻\n" + "判断" * 300),
        "research/latest-preview-FY26Q3/predictions.json": json.dumps(
            {"predictions": [{"id": "P1", "metric": "revenue"}] * 3}
        ),
        "research/notes/fresh.md": "一小时前的笔记" * 50,
        "research/notes/old.md": "25 小时前的笔记" * 50,
    }
    _patch_knowledge(monkeypatch, ["drivers", "debate"], thread_start_epoch=None)
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="earnings")])

    now = int(time.time())
    result = await run_check_done(
        workflow="dalton-earnings-preview",
        params=params,
        backend=_make_backend(
            files,
            mtimes={
                "research/notes/fresh.md": now - 3600,
                "research/notes/old.md": now - 25 * 3600,
            },
        ),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("fresh.md.language.json" in m for m in result["missing"])
    assert not any("old.md" in m for m in result["missing"])


@pytest.mark.asyncio
async def test_language_review_disjunct_ignores_neighbour_unfinished_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 2 recorded the review-pass hash and the draft changed after it:
    the language check reconciles against the same-name run's journal
    directly. A neighbouring (other-workflow, unfinished) run must not turn
    that lookup into 无法核对 — that neighbour may still cost the REVIEW
    check its own missing entry, which is that check's rule."""
    files = _full_files()
    # An unfinished run of ANOTHER workflow on the same thread.
    files[".agents/threads/short-1/workflows/task-crashed/workflow.js"] = "// full-coverage"
    # The draft changed after the review pass: current != review sha.
    review_sha = _draft_hash(files[DRAFT])
    files[DRAFT] = files[DRAFT] + "\n事后改动。\n"
    doc = json.loads(files[f"{DRAFT}.language.json"])
    doc["rounds"][1]["draft_sha256"] = review_sha
    files[f"{DRAFT}.language.json"] = json.dumps(doc, ensure_ascii=False)
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    # The review check's own unfinished-run refusal stands (its rule, not
    # the language check's); the language check itself adds no gap.
    assert any("未结束的 workflow run" in m for m in result["missing"])
    assert not any("语言审查" in m or "language.json" in m for m in result["missing"])


# --- Wake-before-Eastern-midnight: the window binds to the turn's start ----


def _wake_delta_files(*, material: str = "yes", note: str = "research/2026-10-26-delta.md") -> dict[str, str]:
    """A healthy manual dalton-thesis-delta workspace: commit state on disk,
    one verdict row, and the delta note named for the EASTERN business day
    (2026-10-26 ET) while the agent's params will say 2026-10-27."""
    note_text = "# 增量\n" + "变化" * 200
    return {
        "research/tracker/last_run.json": json.dumps(
            {"ticker": "META", "run_id": "20261027-0310", "material": material, "until": "2026-10-26"}
        ),
        "research/tracker/updates.log": "2026-10-27 03:40 | 20261027-0310 | material=yes | 摘要\n",
        note: note_text,
    }


# UTC 2026-10-27T03:10Z == 2026-10-26 23:10 Eastern: the wake fires the
# evening BEFORE the Eastern business day the agent's as_of will name.
_WAKE_EPOCH = int(datetime(2026, 10, 27, 3, 10, tzinfo=timezone.utc).timestamp())


@pytest.mark.asyncio
async def test_wake_before_eastern_midnight_binds_window_to_turn_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual path, woken 2026-10-26 23:10 ET, agent passes as_of=2026-10-27
    and names the note for the Eastern day (2026-10-26): the window starts at
    the turn's start, so the verdict/note written during the turn count and
    the contract reports done."""
    captured = _patch_knowledge_capture(monkeypatch, ["thesis"])
    _patch_verdicts(monkeypatch, [_verdict_row("THESIS_STRENGTHENED", "internal_material")])
    # This turn's start: the wake time, read from conversation_responses.
    monkeypatch.setattr(
        check_done_tools,
        "_run_start_epoch",
        create_autospec(
            check_done_tools._run_start_epoch,
            return_value=_WAKE_EPOCH,
        ),
    )

    result = await run_check_done(
        workflow="dalton-thesis-delta",
        params={"ticker": "META", "as_of": "2026-10-27"},
        backend=_make_backend(
            _wake_delta_files(), mtimes={"research/2026-10-26-delta.md": _WAKE_EPOCH + 1800}
        ),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="resp-wake",
    )
    assert result["done"], result["missing"]

    # The knowledge-version window is the TURN's start (03:10Z), not the
    # as_of day's Eastern midnight (2026-10-27T04:00Z), which would sit after
    # everything the turn wrote.
    assert captured["args"][2] == datetime.fromtimestamp(_WAKE_EPOCH, tz=timezone.utc)


@pytest.mark.asyncio
async def test_delta_note_variant_accepts_the_eastern_day_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """as_of=2026-10-27 renders research/2026-10-27-delta.md, which does not
    exist; the contract accepts the note the run actually wrote
    (research/2026-10-26-delta.md) because its mtime is inside the run's
    window."""
    _patch_knowledge(
        monkeypatch, ["thesis"], start_epochs={"resp-wake": _WAKE_EPOCH}
    )
    _patch_verdicts(monkeypatch, [_verdict_row("THESIS_STRENGTHENED", "internal_material")])

    result = await run_check_done(
        workflow="dalton-thesis-delta",
        params={"ticker": "META", "as_of": "2026-10-27"},
        backend=_make_backend(
            _wake_delta_files(), mtimes={"research/2026-10-26-delta.md": _WAKE_EPOCH + 1800}
        ),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="resp-wake",
    )
    assert result["done"], result["missing"]
    assert not any("不存在" in m for m in result["missing"])


@pytest.mark.asyncio
async def test_delta_note_variant_outside_window_is_still_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-glob note whose mtime predates the run's window does not stand
    in for this run's deliverable: the missing entry names both the expected
    path and the variant glob."""
    _patch_knowledge(
        monkeypatch, ["thesis"], start_epochs={"resp-wake": _WAKE_EPOCH}
    )
    _patch_verdicts(monkeypatch, [_verdict_row("THESIS_STRENGTHENED", "internal_material")])

    result = await run_check_done(
        workflow="dalton-thesis-delta",
        params={"ticker": "META", "as_of": "2026-10-27"},
        backend=_make_backend(
            _wake_delta_files(), mtimes={"research/2026-10-26-delta.md": _WAKE_EPOCH - 86400}
        ),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="resp-wake",
    )
    assert not result["done"]
    assert any(
        "research/2026-10-27-delta.md" in m and "research/*-delta.md" in m
        for m in result["missing"]
    )


@pytest.mark.asyncio
async def test_explicit_delta_note_param_wins_over_dated_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """params["delta_note"] (the path the agent says it wrote) replaces the
    dated template, no variant search needed, as long as its mtime is within
    the current run's window."""
    _patch_knowledge(
        monkeypatch, ["thesis"], start_epochs={"resp-wake": _WAKE_EPOCH}
    )
    _patch_verdicts(monkeypatch, [_verdict_row("THESIS_STRENGTHENED", "internal_material")])

    result = await run_check_done(
        workflow="dalton-thesis-delta",
        params={
            "ticker": "META",
            "as_of": "2026-10-27",
            "delta_note": "research/2026-10-26-delta.md",
        },
        backend=_make_backend(
            _wake_delta_files(), mtimes={"research/2026-10-26-delta.md": _WAKE_EPOCH + 1800}
        ),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="resp-wake",
    )
    assert result["done"], result["missing"]


@pytest.mark.asyncio
async def test_explicit_delta_note_param_stale_mtime_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicitly passed path_param (delta_note) whose mtime predates this
    run's window must fail freshness check and explain the reason."""
    _patch_knowledge(
        monkeypatch, ["thesis"], start_epochs={"resp-wake": _WAKE_EPOCH}
    )
    _patch_verdicts(monkeypatch, [_verdict_row("THESIS_STRENGTHENED", "internal_material")])

    result = await run_check_done(
        workflow="dalton-thesis-delta",
        params={
            "ticker": "META",
            "as_of": "2026-10-27",
            "delta_note": "research/2026-10-20-delta.md",
        },
        backend=_make_backend(
            _wake_delta_files(note="research/2026-10-20-delta.md"),
            mtimes={"research/2026-10-20-delta.md": _WAKE_EPOCH - 3600},
        ),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="resp-wake",
    )
    assert result["done"] is False
    assert any(
        "research/2026-10-20-delta.md" in m
        and "不是本轮写的" in m
        and "期望路径" in m
        for m in result["missing"]
    )


@pytest.mark.asyncio
async def test_variant_glob_rejects_old_or_undated_filename_even_when_mtime_in_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-saved old file (or an undated file) matching variant_glob whose
    mtime falls inside this run's window must NOT be accepted when its
    filename date is neither as_of nor the window start's Eastern business day."""
    _patch_knowledge(
        monkeypatch, ["thesis"], start_epochs={"resp-wake": _WAKE_EPOCH}
    )
    _patch_verdicts(monkeypatch, [_verdict_row("THESIS_STRENGTHENED", "internal_material")])

    for bad_note in ("research/2026-10-20-delta.md", "research/undated-delta.md"):
        result = await run_check_done(
            workflow="dalton-thesis-delta",
            params={"ticker": "META", "as_of": "2026-10-27"},
            backend=_make_backend(
                _wake_delta_files(note=bad_note),
                mtimes={bad_note: _WAKE_EPOCH + 1800},
            ),
            workspace_id=WS,
            thread_id="short-1",
            conversation_run_id="resp-wake",
        )
        assert result["done"] is False
        assert any(
            "research/2026-10-27-delta.md" in m and "research/*-delta.md" in m
            for m in result["missing"]
        )


@pytest.mark.asyncio
async def test_resolved_variant_draft_flows_to_closing_sidecar_and_language_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When dalton-earnings-preview accepts drafts/2026-10-26-preview-FY26Q3.md
    via variant_glob while as_of=2026-10-27, closing's check_draft and
    language_review must inspect the resolved 2026-10-26 draft and its sidecars
    rather than demanding nonexistent 2026-10-27 sidecars."""
    _patch_knowledge(
        monkeypatch, ["drivers", "debate"], start_epochs={"resp-wake": _WAKE_EPOCH}
    )
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="earnings")])

    draft_26 = "drafts/2026-10-26-preview-FY26Q3.md"
    draft_text = "# 前瞻\n" + "判断" * 300
    push_27 = "research/push/2026-10-27-earnings-preview-FY26Q3.md"
    push_text = "## 结论\n维持。\n"
    pred_27 = "research/2026-10-27-preview-FY26Q3/predictions.json"
    files = {
        draft_26: draft_text,
        "drafts/2026-10-26-preview-FY26Q3.audit.md": _sidecar_for(draft_text),
        f"{draft_26}.language.json": _language_json(draft_text),
        push_27: push_text,
        f"{push_27}.language.json": _language_json(push_text),
        pred_27: json.dumps({"predictions": [{"id": "P1", "metric": "revenue"}] * 3}),
    }
    mtimes = {
        draft_26: _WAKE_EPOCH + 600,
        push_27: _WAKE_EPOCH + 600,
    }
    result = await run_check_done(
        workflow="dalton-earnings-preview",
        params={"ticker": "META", "quarter": "FY26Q3", "as_of": "2026-10-27"},
        backend=_make_backend(files, mtimes=mtimes),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="resp-wake",
    )
    assert result["done"] is True, result["missing"]


@pytest.mark.asyncio
async def test_verdict_from_sibling_thread_in_same_workspace_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verdict written in the same workspace and time window by another
    thread must not satisfy this thread's check; only this thread's own verdict
    passes."""
    _patch_knowledge(
        monkeypatch, ["thesis"], start_epochs={"resp-wake": _WAKE_EPOCH}
    )
    _patch_verdicts(
        monkeypatch,
        [
            _verdict_row(
                "THESIS_STRENGTHENED",
                "earnings",
                conversation_thread_id="other-thread-99",
            )
        ],
    )
    rejected = await run_check_done(
        workflow="dalton-thesis-delta",
        params={"ticker": "META", "as_of": "2026-10-27"},
        backend=_make_backend(
            _wake_delta_files(), mtimes={"research/2026-10-26-delta.md": _WAKE_EPOCH + 1800}
        ),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="resp-wake",
    )
    assert rejected["done"] is False
    assert any("没有符合条件的 verdict" in m for m in rejected["missing"])

    _patch_verdicts(
        monkeypatch,
        [
            _verdict_row(
                "THESIS_STRENGTHENED",
                "internal_material",
                conversation_thread_id="short-1",
            )
        ],
    )
    accepted = await run_check_done(
        workflow="dalton-thesis-delta",
        params={"ticker": "META", "as_of": "2026-10-27"},
        backend=_make_backend(
            _wake_delta_files(), mtimes={"research/2026-10-26-delta.md": _WAKE_EPOCH + 1800}
        ),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="resp-wake",
    )
    assert accepted["done"] is True, accepted["missing"]


# --- Same-as_of reruns: every contract artifact must be THIS run's writing ---


# A second delta run later the same as_of day: its window starts here.
_SECOND_RUN_EPOCH = int(datetime(2026, 10, 27, 13, 0, tzinfo=timezone.utc).timestamp())


def _second_run_files() -> dict[str, str]:
    """A healthy thesis-delta workspace whose note keeps the template's own
    dated spelling (no variant needed): as_of names the same day as the run."""
    return {
        "research/tracker/last_run.json": json.dumps(
            {"ticker": "META", "run_id": "20261027-1300", "material": "yes", "until": "2026-10-27"}
        ),
        "research/tracker/updates.log": "2026-10-27 13:05 | 20261027-1300 | material=yes | s\n",
        "research/2026-10-27-delta.md": "# 增量\n" + "变化" * 200,
    }


def _patch_second_run_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        check_done_tools,
        "_run_start_epoch",
        create_autospec(
            check_done_tools._run_start_epoch,
            return_value=_SECOND_RUN_EPOCH,
        ),
    )


@pytest.mark.asyncio
async def test_same_as_of_rerun_rejects_the_morning_runs_template_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second run of the same as_of day: the note left by the morning run
    (template path, exists, big enough) is NOT this run's writing — judged
    missing with the expected path."""
    _patch_second_run_start(monkeypatch)
    _patch_knowledge(monkeypatch, ["thesis"])
    _patch_verdicts(monkeypatch, [_verdict_row("THESIS_STRENGTHENED", "internal_material")])

    result = await run_check_done(
        workflow="dalton-thesis-delta",
        params={"ticker": "META", "as_of": "2026-10-27"},
        backend=_make_backend(
            _second_run_files(),
            mtimes={
                "research/2026-10-27-delta.md": _SECOND_RUN_EPOCH - 3600,
                "research/tracker/last_run.json": _SECOND_RUN_EPOCH + 60,
                "research/tracker/updates.log": _SECOND_RUN_EPOCH + 60,
            },
        ),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="resp-2nd",
    )
    assert result["done"] is False
    assert any(
        "不是本轮写的" in m
        and "research/2026-10-27-delta.md" in m
        and "期望路径" in m
        for m in result["missing"]
    )


@pytest.mark.asyncio
async def test_same_as_of_rerun_passes_when_this_run_rewrites_the_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rewritten inside this run's window (even 2s behind the window start —
    sandbox clock skew): the contract completes."""
    _patch_second_run_start(monkeypatch)
    _patch_knowledge(monkeypatch, ["thesis"])
    _patch_verdicts(monkeypatch, [_verdict_row("THESIS_STRENGTHENED", "internal_material")])

    result = await run_check_done(
        workflow="dalton-thesis-delta",
        params={"ticker": "META", "as_of": "2026-10-27"},
        backend=_make_backend(
            _second_run_files(),
            mtimes={
                # 2s behind the run start: inside the shared 5s tolerance.
                "research/2026-10-27-delta.md": _SECOND_RUN_EPOCH - 2,
                "research/tracker/last_run.json": _SECOND_RUN_EPOCH + 60,
                "research/tracker/updates.log": _SECOND_RUN_EPOCH + 60,
            },
        ),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="resp-2nd",
    )
    assert result["done"], result["missing"]


@pytest.mark.asyncio
async def test_same_as_of_rerun_rejects_a_stale_last_run_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """last_run.json is a contract artifact like the rest: a commit record
    from the morning run does not close the afternoon run."""
    _patch_second_run_start(monkeypatch)
    _patch_knowledge(monkeypatch, ["thesis"])
    _patch_verdicts(monkeypatch, [_verdict_row("THESIS_STRENGTHENED", "internal_material")])

    result = await run_check_done(
        workflow="dalton-thesis-delta",
        params={"ticker": "META", "as_of": "2026-10-27"},
        backend=_make_backend(
            _second_run_files(),
            mtimes={
                "research/2026-10-27-delta.md": _SECOND_RUN_EPOCH + 60,
                "research/tracker/last_run.json": _SECOND_RUN_EPOCH - 3600,
                "research/tracker/updates.log": _SECOND_RUN_EPOCH + 60,
            },
        ),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="resp-2nd",
    )
    assert result["done"] is False
    assert any(
        "不是本轮写的" in m and "research/tracker/last_run.json" in m
        for m in result["missing"]
    )


@pytest.mark.asyncio
async def test_named_run_without_readable_start_reports_retryable_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """conversation_run_id provided but its start cannot be read: the check
    reports the dedicated gap instead of silently widening the window; with
    no run id at all, the legacy fallback windows still apply."""
    # No start epoch for "resp-undated": the DB stub answers None for it.
    _patch_knowledge(monkeypatch, ["thesis"], start_epochs={"short-1": _SECOND_RUN_EPOCH})
    _patch_verdicts(monkeypatch, [_verdict_row("THESIS_STRENGTHENED", "internal_material")])

    result = await run_check_done(
        workflow="dalton-thesis-delta",
        params={"ticker": "META", "as_of": "2026-10-27"},
        backend=_make_backend(
            _second_run_files(),
            mtimes={
                "research/2026-10-27-delta.md": _SECOND_RUN_EPOCH - 3600,
                "research/tracker/last_run.json": _SECOND_RUN_EPOCH + 60,
                "research/tracker/updates.log": _SECOND_RUN_EPOCH + 60,
            },
        ),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="resp-undated",
    )
    assert result["done"] is False
    assert any(
        "无法核对本轮完成情况" in m and "读不到本轮开始时间" in m and "dalton_check_done" in m
        for m in result["missing"]
    )

    # Without a run id, the same workspace falls back to the thread-start
    # window (the morning note is in-window there, so no such gap appears).
    no_id = await run_check_done(
        workflow="dalton-thesis-delta",
        params={"ticker": "META", "as_of": "2026-10-27"},
        backend=_make_backend(
            _second_run_files(),
            mtimes={
                "research/2026-10-27-delta.md": _SECOND_RUN_EPOCH - 3600,
                "research/tracker/last_run.json": _SECOND_RUN_EPOCH + 60,
                "research/tracker/updates.log": _SECOND_RUN_EPOCH + 60,
            },
        ),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not any("无法核对本轮完成情况" in m for m in no_id["missing"])


# --- fresh_this_run: the run-window gate is opt-in per contract artifact ----


@pytest.mark.asyncio
async def test_publish_reuses_last_rounds_approved_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dalton-publish on the draft awaiting_pm'd LAST round (unchanged, hash
    and approval still valid): the draft is not fresh_this_run, so its old
    mtime is no gap — the contract completes on content, not on which round
    last touched the file."""
    from src.dalton.publish.files import prepend_publication_banner

    _patch_pending(monkeypatch, error=RuntimeError("must not be queried"))
    # This round's run is named and dated (started now); the draft is a day
    # old — that gap is exactly what must NOT matter for a non-fresh artifact.
    monkeypatch.setattr(
        check_done_tools,
        "_run_start_epoch",
        create_autospec(
            check_done_tools._run_start_epoch,
            return_value=int(datetime.now(timezone.utc).timestamp()),
        ),
    )
    files = _publish_files()
    files["published/2026-10-23-call-META.md"] = prepend_publication_banner(
        files[PUB_DRAFT],
        title="Conviction Call",
        kind="conviction_call",
        published_date="2026-10-23",
        approver_user_id="user-1",
        approved_at="2026-10-23T00:00:00Z",
        reviewer_model="gpt-6.1-sol-oauth",
        reviewer_family="openai",
        publication_id="pr-1",
    )
    stale = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp())
    result = await run_check_done(
        workflow="dalton-publish",
        params=PUB_PARAMS,
        backend=_make_backend(files, mtimes={PUB_DRAFT: stale}),
        workspace_id=WS,
        user_id="user-1",
        thread_id="short-1",
        conversation_run_id="resp-publish",
    )
    assert result["done"], result["missing"]


@pytest.mark.asyncio
async def test_wake_skill_old_note_is_still_missing_under_fresh_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fresh gate stays on the wake skills' own deliverables: thesis-delta's
    note from an earlier run does not close this run (contract marks it
    fresh_this_run)."""
    _patch_second_run_start(monkeypatch)
    _patch_knowledge(monkeypatch, ["thesis"])
    _patch_verdicts(monkeypatch, [_verdict_row("THESIS_STRENGTHENED", "internal_material")])

    result = await run_check_done(
        workflow="dalton-thesis-delta",
        params={"ticker": "META", "as_of": "2026-10-27"},
        backend=_make_backend(
            _second_run_files(),
            mtimes={
                "research/2026-10-27-delta.md": _SECOND_RUN_EPOCH - 3600,
                "research/tracker/last_run.json": _SECOND_RUN_EPOCH + 60,
                "research/tracker/updates.log": _SECOND_RUN_EPOCH + 60,
            },
        ),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="resp-2nd",
    )
    assert result["done"] is False
    assert any("不是本轮写的" in m and "research/2026-10-27-delta.md" in m for m in result["missing"])


@pytest.mark.asyncio
async def test_today_anchor_is_the_window_start_across_utc_midnight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run starts 23:50 UTC, last_run.json committed 23:59, the check runs
    after the UTC midnight crossing: "today" anchors to the run's window
    start (not max with UTC midnight), so the commit passes."""
    day_d = datetime(2026, 10, 27, 23, 50, tzinfo=timezone.utc)
    _freeze_execution_day(monkeypatch, "2026-10-28")  # the check runs the NEXT day
    monkeypatch.setattr(
        check_done_tools,
        "_run_start_epoch",
        create_autospec(check_done_tools._run_start_epoch, return_value=int(day_d.timestamp())),
    )
    _patch_knowledge(monkeypatch, ["thesis"])
    _patch_verdicts(monkeypatch, [_verdict_row("THESIS_STRENGTHENED", "internal_material")])

    files = _second_run_files()
    # The note names the run's as_of day (10-27), written this run.
    note = "research/2026-10-27-delta.md"
    result = await run_check_done(
        workflow="dalton-thesis-delta",
        params={"ticker": "META", "as_of": "2026-10-27"},
        backend=_make_backend(
            files,
            mtimes={
                note: int(datetime(2026, 10, 27, 23, 55, tzinfo=timezone.utc).timestamp()),
                "research/tracker/last_run.json": int(datetime(2026, 10, 27, 23, 59, tzinfo=timezone.utc).timestamp()),
                "research/tracker/updates.log": int(datetime(2026, 10, 27, 23, 59, tzinfo=timezone.utc).timestamp()),
            },
        ),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="resp-night",
    )
    assert result["done"], result["missing"]


@pytest.mark.asyncio
async def test_stale_template_falls_through_to_a_qualifying_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The as_of-spelled note exists but is last round's; this run wrote the
    Eastern-day spelling: the variant search runs for the STALE template file
    too, and the qualifying variant completes the contract."""
    _patch_knowledge(
        monkeypatch, ["thesis"], start_epochs={"resp-wake": _WAKE_EPOCH}
    )
    _patch_verdicts(monkeypatch, [_verdict_row("THESIS_STRENGTHENED", "internal_material")])

    old_note = "# 旧\n" + "变化" * 200
    new_note = "# 新\n" + "变化" * 200
    files = {
        **_wake_delta_files(),
        "research/2026-10-27-delta.md": old_note,  # as_of spelling, STALE
        "research/2026-10-26-delta.md": new_note,  # Eastern-day spelling, this run
    }
    result = await run_check_done(
        workflow="dalton-thesis-delta",
        params={"ticker": "META", "as_of": "2026-10-27"},
        backend=_make_backend(
            files,
            mtimes={
                "research/2026-10-27-delta.md": _WAKE_EPOCH - 86400,
                # 2s before the window start: inside the shared tolerance.
                "research/2026-10-26-delta.md": _WAKE_EPOCH - 2,
            },
        ),
        workspace_id=WS,
        thread_id="short-1",
        conversation_run_id="resp-wake",
    )
    assert result["done"], result["missing"]
    # Not a pass on the stale template: no freshness gap was reported at all.
    assert not any("不是本轮写的" in m for m in result["missing"])
